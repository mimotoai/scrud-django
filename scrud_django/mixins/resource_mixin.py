import logging
from collections.abc import Mapping
from typing import NamedTuple

from django.db import models, router
from django.db.models import JSONField
from django.utils.translation import gettext_lazy as _
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.models.resource_type import ResourceType
from scrud_django.workflow import (
    LEAVE,
    WORKFLOW_POLICY_TYPE_URI,
    TransitionData,
    WorkflowAmbiguousStateError,
    WorkflowInvalidInitialStateError,
    WorkflowInvalidStateError,
    WorkflowTransitionError,
    default_workflow_policy_for,
    workflow_policies_for,
)

from ..scrud_signals import scrud_post_delete, scrud_post_save

workflow_policy_type_uri = WORKFLOW_POLICY_TYPE_URI

# The workflow errors of saves that skipped the ``WorkflowValidator``
# (`ResourceMixin.save`).
logger = logging.getLogger("scrud_django.workflow")

# The workflow errors a report of a transition (`ResourceMixin.get_transitions`)
# catches: a write that skipped the ``WorkflowValidator`` is still saved.
WORKFLOW_ERRORS = (
    WorkflowAmbiguousStateError,
    WorkflowInvalidInitialStateError,
    WorkflowInvalidStateError,
    WorkflowTransitionError,
)


class Transitions(NamedTuple):
    """A save's workflow transitions (`ResourceMixin.get_transitions`):
    ``scrud_post_save`` sends the first three as its keyword arguments of
    the same names; `ResourceMixin.save` logs `errors`."""

    # Under the policy of the first context the content names that has one
    # (`ResourceMixin.workflow_context_and_policy`); ``None`` when no policy
    # applies or the transition is invalid there.
    transition_data: TransitionData | None
    # The workflow error that made `transition_data` ``None``, else ``None``.
    transition_error: WorkflowTransitionError | None
    # Context -> `TransitionData` (``None``: invalid there), for every
    # context the stored resource or the content is in under a policy.
    transitions: dict[str | None, TransitionData | None]
    # Context -> the workflow error that made its `transitions` entry
    # ``None``, for each such context (not sent with the signal: `save`
    # logs them).
    errors: dict[str | None, WorkflowTransitionError]


def rbac_contexts_of(content):
    """The contexts a resource's `content` is in: its ``rbac_context`` when it is
    a string or a list or tuple of strings, else ``DEFAULT_CONTEXT``, the
    context scoped-rbac authorizes a POST that names none in. Without it a
    resource that names none would have no context, which no role grants.
    Absent, ``None`` and ``""`` are all the default context. The one reading of
    a resource's contexts: object checks (``Resource.rbac_contexts``, and
    ``scrud_django.permissions.resource_contexts``, scoped-rbac's default
    ``contexts_for`` on the scrud views, which give checks the content), and the workflow checks (``WorkflowValidator``) all
    use it. Each context is the exact ``str`` of its characters, as
    scoped-rbac reads a context (``access.context_name``): a ``str`` subclass
    (a ``StrEnum`` member, from Python code) is not kept as itself. A value
    is told by its type, as scoped-rbac does: an object only passing as a
    ``str`` (a ``SimpleLazyObject``) is not a string, and is not
    evaluated."""
    context = content.get("rbac_context") if isinstance(content, Mapping) else None
    if issubclass(type(context), str):
        return [str.__str__(context)]
    if issubclass(type(context), (list, tuple)) and all(
        issubclass(type(item), str) for item in context
    ):
        return [str.__str__(item) for item in context]
    return [DEFAULT_CONTEXT]


class ScrudfulMixin(models.Model):
    class Meta:
        abstract = True

    resource_type = models.ForeignKey(
        ResourceType,
        on_delete=models.PROTECT,
        verbose_name=_('resource type'),
    )
    modified_at = models.DateTimeField()
    etag = models.CharField(max_length=40)

    @property
    def rbac_context(self):
        raise NotImplementedError()


class ResourceMixin(ScrudfulMixin):
    # The actual JSON content for this resource
    content = JSONField()

    @property
    def rbac_context(self):
        return self.content.get("rbac_context", None)

    # A resource whose content names no ``rbac_context`` belongs to the default
    # context (""), where scoped-rbac also authorizes its creation. Only a string
    # (or a list or tuple of strings) names a context: any other value is content
    # that merely uses the key, such as a JSON-LD context document's term
    # definition ``"rbac_context": {"@id": "openteams:RBACContext"}``. Taken as a
    # context, such a value is one no role grants, which hid those documents
    # from everyone but superusers.
    @property
    def rbac_contexts(self):
        """The contexts scoped-rbac checks this resource in (its object checks
        read ``rbac_contexts`` before ``rbac_context``): `rbac_contexts_of` its
        content."""
        return rbac_contexts_of(self.content)

    def to_cel(self):
        """What a grant's CEL condition reads for this resource: its content
        when it is a mapping, else ``{}``. The scrud views' object checks and
        list filter give conditions (and operators) the content itself
        (`scrud_django.permissions.content_of`), the same fields."""
        return self.content if isinstance(self.content, Mapping) else {}

    def _write_db(self, using=None):
        """The database a ``save()`` or ``delete()`` of this instance writes
        to, as Django picks it: `using`, else the router's
        ``db_for_write`` for this instance (not ``_state.db``, the database it
        was read from, which may be a read replica)."""
        return using or router.db_for_write(type(self), instance=self)

    def save(
        self, force_insert=False, force_update=False, using=None, update_fields=None
    ) -> None:
        """``Model.save()``, then ``scrud_post_save``, sent with ``using`` the
        database the save wrote to (`_write_db`) and the save's transitions
        (`get_transitions`), read before it writes.

        A save is not checked against the workflow (the ``WorkflowValidator``
        checks API writes; the admin, ``ResourceRegistration`` and server
        code skip it): an invalid one is still saved, and each context where
        it is invalid is logged as a WARNING on the ``scrud_django.workflow``
        logger, naming the resource's pk, its type URI, the context and the
        workflow error (`log_workflow_errors`)."""
        db = self._write_db(using)
        transitions = self.get_transitions(db)
        action = 'CREATE' if self._state.adding else 'UPDATE'
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )
        scrud_post_save.send(
            sender=self.__class__,
            instance=self,
            update_fields=update_fields,
            using=db,
            resource_type_uri=self.resource_type.type_uri,
            transition_data=transitions.transition_data,
            transition_error=transitions.transition_error,
            transitions=transitions.transitions,
            action=action,
        )
        self.log_workflow_errors(transitions.errors)

    def log_workflow_errors(self, errors):
        """Log each workflow error of `errors` (context -> error, from
        `get_transitions`) as a WARNING on the ``scrud_django.workflow``
        logger. An API write passed the ``WorkflowValidator`` and has none;
        a save that skipped it (the admin, ``ResourceRegistration``, server
        code) would otherwise be invalid silently in every context but the
        one ``transition_error`` reports."""
        for context, error in errors.items():
            logger.warning(
                "Resource pk %s of %s was saved past its workflow in context "
                "%r (policy %r): %s (prior state %r, proposed state %r%s).",
                self.pk,
                self.resource_type.type_uri,
                context,
                error.policy,
                error.message,
                error.prior_state,
                error.proposed_state,
                (
                    f", matching states {error.matching_states!r}"
                    if isinstance(error, WorkflowAmbiguousStateError)
                    else ""
                ),
            )

    def delete(self, using=None, keep_parents=False) -> tuple[int, dict[str, int]]:
        """``Model.delete()``, then ``scrud_post_delete``, sent with ``using``
        the database the delete wrote to (`_write_db`): an instance read from
        a replica still has the replica's alias in ``_state.db``. Django's
        delete sets the instance's pk to None, so the signal's ``pk`` is the
        one it had, naming the deleted row."""
        db = self._write_db(using)
        pk = self.pk
        res = super().delete(using=using, keep_parents=keep_parents)
        scrud_post_delete.send(
            sender=self.__class__,
            instance=self,
            pk=pk,
            using=db,
            resource_type_uri=self.resource_type.type_uri,
            action='DELETE',
        )
        return res

    def get_transition_data(self, using=None):
        """The `TransitionData` ``scrud_post_save`` reports for this save as
        ``transition_data`` (`get_transitions`): under the policy
        `get_workflow_policy` picks, from the stored row (read on the
        database the save writes to, `using` or the router's for this
        instance, not a read replica) to the content. A create, a save whose
        row is not on that database, and an update whose picked context is
        one the stored resource was not in (a move into it), are reported as
        ``__INITIALIZE__`` there (no prior state, the initial state the
        content is in), as the ``WorkflowValidator`` checks them. ``None``
        when no policy applies or the transition is invalid, content in no
        initial state included (``transition_error`` tells them apart)."""
        return self.get_transitions(using).transition_data

    def stored_content(self, using=None):
        """The content of this resource's stored row on the database a save
        writes to (`using`, else the router's for this instance), or ``None``
        for a create or a row that is not there (an instance copied to
        another database with ``save(using=...)``)."""
        if self._state.adding:
            return None
        db = self._write_db(using)
        prior_version = self.__class__.objects.using(db).filter(pk=self.pk).first()
        return prior_version.content if prior_version is not None else None

    def get_transitions(self, using=None):
        """The `Transitions` ``scrud_post_save`` reports for this save, from
        the stored content (`stored_content`) to the content:

        - ``transition_data`` (`get_transition_data`), and
          ``transition_error``, the workflow error that made it ``None``
          (an invalid transition, or content that meets the conditions of
          more than one state, which a write that skipped the
          ``WorkflowValidator`` still saves), else ``None``;
        - ``transitions``: for each context the stored resource or the
          content is in (stored ones first, as the ``WorkflowValidator``
          checks them) where a policy applies (`workflow_policies_for`), the
          write there. (`rbac_contexts_of` always names one context at
          least; a resource whose `rbac_contexts` named none, as an
          override might, would be reported under ``None`` with the
          settings default, as the ``WorkflowValidator`` checks it:
          defensive.) A context it stays in: the transition under its
          policy (``None`` if invalid); one it enters (or a create's): its
          ``__INITIALIZE__`` (``None`` if the content is in no initial state
          there); one it leaves: ``TransitionData(None, <the state it was
          in there>, "__LEAVE__")`` (`LEAVE`); ``None`` in any of them where
          the content's (or stored content's) state is ambiguous. A context
          without a policy is left out.

        Each context's policy is read once."""
        prior_data = self.stored_content(using)
        stored_contexts = rbac_contexts_of(prior_data) if prior_data is not None else []
        # Defensive: `rbac_contexts_of` never gives no context.
        contexts = list(self.rbac_contexts) or [None]
        concerned = list(dict.fromkeys(stored_contexts + contexts))
        named = [context for context in concerned if context is not None]
        # The stored policies of every named context, read in one query.
        found = dict(workflow_policies_for(self.resource_type, named)) if named else {}
        policies = {
            context: (
                default_workflow_policy_for(self.resource_type)
                if context is None
                else found[context]
            )
            for context in concerned
        }
        reported = {
            context: self.transition_in(
                policy,
                prior_data,
                entering=context is not None and context not in stored_contexts,
                leaving=context not in contexts,
            )
            for context, policy in policies.items()
            if policy is not None
        }
        # transition_data's context: the first the content names with a
        # policy (`workflow_context_and_policy`).
        picked = next((c for c in contexts if policies[c] is not None), None)
        transition_data, transition_error = reported.get(picked, (None, None))
        return Transitions(
            transition_data,
            transition_error,
            {context: data for context, (data, _) in reported.items()},
            {
                context: error
                for context, (_, error) in reported.items()
                if error is not None
            },
        )

    def transition_in(self, workflow, prior_data, entering=False, leaving=False):
        """``(TransitionData or None, workflow error or None)``: this save
        under `workflow` in a context the resource `entering` moves into,
        `leaving` moves out of, or else stays in, from `prior_data` (``None``:
        a create). An invalid transition, a create (or move in) whose
        content is in no initial state (``WorkflowInvalidInitialStateError``)
        and content that meets the conditions of more than one state
        (``WorkflowAmbiguousStateError``) are reported as ``None`` with the
        error, never raised."""
        try:
            if leaving:
                prior_state = workflow.state(prior_data)
                return TransitionData(None, prior_state, LEAVE), None
            if prior_data is None or entering:
                # A create has no prior state; its proposed state is the
                # initial state the content is in. Content in none is an
                # invalid create (``WorkflowInvalidInitialStateError``), as
                # the `WorkflowValidator` checks it.
                return workflow.initial_transition(self.content), None
            return workflow.determine_transition(self.content, prior_data), None
        except WORKFLOW_ERRORS as error:
            # Workflow validation is the WorkflowValidator's: the error is
            # reported, not raised.
            return None, error

    def get_workflow_policy(self):
        """The policy whose transition the ``scrud_post_save`` signal reports.

        For a resource in several contexts it is the policy in effect
        (`workflow_policy_for`: the stored policy there, else the settings
        default) in the **first** context its content names, in the order of
        its ``rbac_context`` list; for one in a single context, that context's
        (`rbac_contexts_of` gives the default context ``""`` to content that
        names none; `workflow_context_and_policy` gives the settings default
        to a resource in none, defensively). Its content decides, so
        it is deterministic. A context with neither is skipped; ``None`` when
        none has one. Validation and authorization are the
        `WorkflowValidator`'s, which checks every context."""
        return self.workflow_context_and_policy()[1]

    def workflow_context_and_policy(self):
        """``(context, policy)`` of `get_workflow_policy`: the context whose
        policy it is, or ``(None, None)``."""
        contexts = self.rbac_contexts
        if not contexts:
            # Defensive: `rbac_contexts_of` never gives no context; an
            # `rbac_contexts` override that did would be checked under the
            # settings default, as the ``WorkflowValidator`` checks it.
            return None, default_workflow_policy_for(self.resource_type)
        for context, policy in workflow_policies_for(self.resource_type, contexts):
            if policy is not None:
                return context, policy
        return None, None

    class Meta:
        abstract = True
