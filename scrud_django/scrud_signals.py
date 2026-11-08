import logging
from collections.abc import Mapping

from django.db import router, transaction
from django.dispatch import Signal

from .conf import workflow_actions

# Workflow actions that fail, and SCRUD_WORKFLOW_ACTIONS entries skipped at
# write time (`ScrudSignalProcessor`).
logger = logging.getLogger("scrud_django.workflow_actions")

# Sent by ``ResourceMixin.save()`` / ``delete()`` inside the write, before it
# commits (and before the API's post-save check, which may still roll it
# back): a receiver may read or change the database in the write's
# transaction. Work that leaves the database belongs in
# ``transaction.on_commit``, as `ScrudSignalProcessor` runs the configured
# workflow actions. ``using`` is the database the write went to (`write_alias`),
# which ``on_commit`` must name: not the instance's ``_state.db``, the one it
# was read from (a read replica under a primary/replica router).
# ``scrud_post_delete`` also sends ``pk``, the deleted row's primary key:
# ``instance`` is Django's deleted instance, whose pk is None by then.
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
    entry of the setting (`determine_action`)."""

    workflow_actions = {}
    post_save_literal = 'on_save'
    post_delete_literal = 'on_delete'
    resource_type_uri_literal = 'resource_type_uri'

    def __init__(self) -> None:
        scrud_post_save.connect(self.post_save_trigger)
        scrud_post_delete.connect(self.post_delete_trigger)
        self.workflow_actions = workflow_actions()

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

    def run_on_commit(self, action, sender, kwargs, kind=None):
        """Call ``action(sender, **kwargs)`` when the write's transaction
        commits, through a `WorkflowActionCall` that logs its failure as
        `kind` of action. ``robust`` is a backstop: the call does not
        raise."""
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
        or ``None`` is no action."""
        actions = self.workflow_actions.get(type_uri)
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
