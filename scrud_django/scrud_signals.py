import logging
from collections.abc import Mapping
from types import MappingProxyType

from django.core.exceptions import ImproperlyConfigured
from django.db import router, transaction
from django.dispatch import Signal

from .conf import configured_workflow_actions

# Workflow actions that fail, and SCRUD_WORKFLOW_ACTIONS entries skipped at
# write time (`ScrudSignalProcessor`).
logger = logging.getLogger("scrud_django.workflow_actions")

# Sent inside the write, before it commits: README "Workflow actions" says
# when (and when not) and what each argument holds; keep it the one full
# description. Keyword arguments: ``signal``, ``sender``, ``instance``,
# ``using`` (the database written to, `write_alias`), ``resource_type_uri``
# and ``action``; ``scrud_post_save`` adds ``update_fields``,
# ``transition_data``, ``transition_error`` and ``transitions``
# (``ResourceMixin.get_transitions``), ``scrud_post_delete`` adds ``pk``.
scrud_post_save = Signal()
scrud_post_delete = Signal()


def write_alias(sender, kwargs):
    """The database a ``scrud_post_save`` / ``scrud_post_delete`` write went
    to: the signal's ``using`` (``ResourceMixin`` always sends it), else the
    router's ``db_for_write`` for the instance, as Django picks a write's
    database. Not the instance's ``_state.db`` first: that is the database it
    was read from, a read replica's alias under a primary/replica router,
    whose connection would run the action at once, before the write commits
    or if it rolls back. (With no router's opinion, ``db_for_write`` gives
    ``_state.db`` anyway.)"""
    return kwargs.get("using") or router.db_for_write(
        sender, instance=kwargs.get("instance")
    )


def action_name(action):
    """`action`'s qualified name (``module.qualname``), or its ``repr`` for
    a callable without one (a ``functools.partial``)."""
    qualname = getattr(action, "__qualname__", None)
    if qualname is None:
        return repr(action)
    module = getattr(action, "__module__", None)
    return f"{module}.{qualname}" if module else qualname


class WorkflowActionCall:
    """A workflow action's call, ``action(sender, **kwargs)``, as
    ``transaction.on_commit`` runs it. An exception it raises is logged on
    the ``scrud_django.workflow_actions`` logger, with the action's qualified
    name, the type URI, the kind of action (`kind`, ``on_save`` or
    ``on_delete``), the resource's pk (``scrud_post_delete``'s ``pk``, as the
    deleted instance has none) and the traceback, and not raised again:
    ``on_commit``'s ``robust`` would log it a second time, anonymously, on
    ``django.db.backends.base``. Its ``__qualname__`` (which that log names)
    is the action's, should anything else go wrong."""

    def __init__(self, action, kind, sender, kwargs):
        self.action = action
        self.kind = kind
        self.sender = sender
        self.kwargs = kwargs
        self.name = action_name(action)
        self.__qualname__ = f"workflow action {self.name}"

    def pk(self):
        if "pk" in self.kwargs:
            return self.kwargs["pk"]
        return getattr(self.kwargs.get("instance"), "pk", None)

    def __call__(self):
        try:
            self.action(self.sender, **self.kwargs)
        except Exception as error:
            logger.error(
                "Workflow action %s (%s for %s pk %s, %s) failed: %s",
                self.name,
                self.kind,
                self.kwargs.get("resource_type_uri"),
                self.pk(),
                self.kwargs.get("action"),
                error,
                exc_info=True,
            )


class ScrudSignalProcessor:
    """Runs the ``SCRUD_WORKFLOW_ACTIONS`` of a resource's type on
    ``scrud_post_save`` (``on_save``) and ``scrud_post_delete``
    (``on_delete``), once the write commits (``transaction.on_commit`` on its
    database): a write rolled back (a post-save 403, any error) runs none of
    them, and outside a transaction they run at once. An action that raises
    never fails the write, in a transaction or not: `WorkflowActionCall`
    logs it on the ``scrud_django.workflow_actions`` logger. Nor does a bad
    entry of the setting (`determine_action`).

    One processor receives the signals at a time: building one connects it
    (`connect`) in place of the one connected before, under a fixed
    ``dispatch_uid`` per signal, and the signals hold it (it need not be kept
    referenced). ``ScrudConfig.ready()`` builds the default one
    (``apps.get_app_config("scrud_django").processor``); an instance of a
    subclass built later (in the ``ready()`` of an app listed after
    ``scrud_django``) replaces it rather than running every action a second
    time."""

    post_save_literal = 'on_save'
    post_delete_literal = 'on_delete'
    resource_type_uri_literal = 'resource_type_uri'
    post_save_dispatch_uid = 'scrud_django.ScrudSignalProcessor.post_save'
    post_delete_dispatch_uid = 'scrud_django.ScrudSignalProcessor.post_delete'
    # Assigned to the processor (`workflow_actions`), else None: the setting's.
    _workflow_actions = None

    def __init__(self) -> None:
        # Loaded (and checked) now, so that a setting that cannot be loaded
        # stops the process at startup; read again after it changes.
        configured_workflow_actions()
        self.connect()

    @property
    def workflow_actions(self):
        """Type URI -> ``{"on_save": ..., "on_delete": ...}``: the mapping
        assigned to this processor, as assigned, else a read-only view
        (``MappingProxyType``) of ``SCRUD_WORKFLOW_ACTIONS``'s
        (`configured_workflow_actions`: loaded once, and again after the
        setting changes, as under ``override_settings``), which every
        processor shares: to change it, change the setting or assign this
        processor a mapping of its own."""
        if self._workflow_actions is not None:
            return self._workflow_actions
        return MappingProxyType(configured_workflow_actions())

    @workflow_actions.setter
    def workflow_actions(self, actions):
        self._workflow_actions = actions

    def connect(self):
        """Connect this processor to ``scrud_post_save`` and
        ``scrud_post_delete`` in place of the processor connected before
        (the same ``dispatch_uid``), strongly."""
        for signal, receiver, dispatch_uid in (
            (scrud_post_save, self.post_save_trigger, self.post_save_dispatch_uid),
            (
                scrud_post_delete,
                self.post_delete_trigger,
                self.post_delete_dispatch_uid,
            ),
        ):
            signal.disconnect(dispatch_uid=dispatch_uid)
            signal.connect(receiver, weak=False, dispatch_uid=dispatch_uid)

    def post_save_trigger(self, sender, **kwargs):
        action = self.determine_action(
            self.post_save_literal, kwargs.get(self.resource_type_uri_literal)
        )
        if action is not None:
            self.run_on_commit(action, sender, kwargs, self.post_save_literal)

    def post_delete_trigger(self, sender, **kwargs):
        action = self.determine_action(
            self.post_delete_literal, kwargs.get(self.resource_type_uri_literal)
        )
        if action is not None:
            self.run_on_commit(action, sender, kwargs, self.post_delete_literal)

    def run_on_commit(self, action, sender, kwargs, kind):
        """Call ``action(sender, **kwargs)`` when the write's transaction
        commits, through a `WorkflowActionCall` that logs its failure as
        `kind` of action (`post_save_literal` or `post_delete_literal`).
        ``robust`` is a backstop: the call does not raise."""
        transaction.on_commit(
            WorkflowActionCall(action, kind, sender, kwargs),
            using=write_alias(sender, kwargs),
            robust=True,
        )

    def determine_action(self, post_literal, type_uri):
        """The `post_literal` (``on_save`` or ``on_delete``) action of
        `type_uri`, or None. Never raises, so never fails the write: an entry
        that is not a mapping, or an action that is not callable, is logged
        on the ``scrud_django.workflow_actions`` logger and runs nothing (the
        ``scrud_django.E007`` and ``E009`` checks report them). A missing key
        or ``None`` is no action, and so is every action of a setting changed
        (``override_settings``) to one that cannot be loaded, which is
        logged."""
        try:
            all_actions = self.workflow_actions
        except (ImproperlyConfigured, ImportError) as error:
            logger.error(
                "SCRUD_WORKFLOW_ACTIONS cannot be loaded (%s); no %s action runs "
                "(scrud_django.E007).",
                error,
                post_literal,
            )
            return None
        actions = all_actions.get(type_uri)
        if actions is None:
            return None
        if not isinstance(actions, Mapping):
            logger.error(
                "SCRUD_WORKFLOW_ACTIONS: the entry for %r is not a mapping (%r); "
                "no %s action runs (scrud_django.E007).",
                type_uri,
                actions,
                post_literal,
            )
            return None
        action = actions.get(post_literal)
        if action is not None and not callable(action):
            logger.error(
                "SCRUD_WORKFLOW_ACTIONS: the %s action for %r is not callable "
                "(%r); it does not run (scrud_django.E009).",
                post_literal,
                type_uri,
                action,
            )
            return None
        return action
