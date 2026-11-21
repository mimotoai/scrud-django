import logging
from collections.abc import Mapping

from django.db import models, router
from django.db.models import JSONField
from django.utils.translation import gettext_lazy as _
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.models.resource_type import ResourceType
from scrud_django.workflow import (
    WORKFLOW_POLICY_TYPE_URI,
    default_workflow_policy_for,
    workflow_policies_for,
)

from ..core.decisions import NULL_CONTENT, take_pending
from ..core.signals import scrud_post_delete, scrud_post_save
from ..core.transitions import (  # noqa: F401  (re-exported)
    WORKFLOW_ERRORS,
    Transitions,
    log_skipped_check,
    transition_in,
    transitions_between,
)

workflow_policy_type_uri = WORKFLOW_POLICY_TYPE_URI

# The workflow errors of saves that skipped the ``WorkflowValidator``
# (`ResourceMixin.save`).
logger = logging.getLogger("scrud_django.workflow")


def rbac_contexts_of(content):
    """The contexts a resource's `content` is in: its ``rbac_context`` when it is
    a string or a list or tuple of strings, else ``DEFAULT_CONTEXT``, the
    context scoped-rbac authorizes a POST that names none in. Without it a
    resource that names none would have no context, which no role grants.
    Absent, ``None`` and ``""`` are all the default context. The one reading of
    a resource's contexts: object checks (``Resource.rbac_contexts``, and
    ``scrud_django.drf.permissions.resource_contexts``, scoped-rbac's default
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
        (`scrud_django.drf.permissions.content_of`), the same fields."""
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
        database the save wrote to (`_write_db`) and the save's
        `WorkflowDecision` as ``decision`` (with its ``transition_data``,
        ``transition_error`` and ``transitions``), taken before it writes:
        the workflow check's, when one is pending for this save
        (`scrud_django.core.decisions.take_pending`: an API or UI write's,
        of this type, this pk, ``None`` for a create, and this content),
        else computed (`get_transitions`, ``checked=False``).

        Otherwise a save is not checked against the workflow (the
        ``WorkflowValidator`` checks API writes; the admin,
        ``ResourceRegistration`` and server code skip it): an invalid one is
        still saved, and each context where it is invalid is logged as a
        WARNING on the ``scrud_django.workflow`` logger, naming the resource's pk, its type URI, the context, the
        policy and the workflow error (`log_workflow_errors`), with the
        states the content meets when it meets several. It is logged once
        the row is written and before ``scrud_post_save`` is sent, so that a
        receiver that raises cannot keep it from being logged (the save may
        still roll back)."""
        db = self._write_db(using)
        decision = take_pending(
            type_iri=self.resource_type.type_uri,
            pk=None if self._state.adding else self.pk,
            content=self.content,
        )
        if decision is None:
            decision = self.get_transitions(db)
        action = 'CREATE' if self._state.adding else 'UPDATE'
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )
        self.log_workflow_errors(decision.errors)
        scrud_post_save.send(
            sender=self.__class__,
            instance=self,
            update_fields=update_fields,
            using=db,
            resource_type_uri=self.resource_type.type_uri,
            transition_data=decision.transition_data,
            transition_error=decision.transition_error,
            transitions=decision.transitions,
            action=action,
            decision=decision,
        )

    def log_workflow_errors(self, errors):
        """Log each workflow error of `errors` (context -> error, from
        `get_transitions`) as a WARNING on the ``scrud_django.workflow``
        logger. An API write passed the ``WorkflowValidator`` and has none;
        a save that skipped it (the admin, ``ResourceRegistration``, server
        code) would otherwise be invalid silently in every context but the
        one ``transition_error`` reports."""
        log_skipped_check(
            errors,
            subject=f"resource pk {self.pk}",
            type_iri=self.resource_type.type_uri,
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
        prior_version = self._stored_row(using)
        return prior_version.content if prior_version is not None else None

    def _stored_row(self, using=None):
        """This resource's stored row on the database a save writes to
        (`stored_content`), or ``None``."""
        if self._state.adding:
            return None
        db = self._write_db(using)
        return self.__class__.objects.using(db).filter(pk=self.pk).first()

    def get_transitions(self, using=None):
        """The unchecked `WorkflowDecision` (``checked=False``,
        `transitions_between`) ``scrud_post_save`` reports for this save,
        from the stored content (`stored_content`) to the content. A
        create, and a save whose row is not on the database written to, is
        a create; a stored row whose content is not an object (JSON null,
        stored before 0.12.0) is an update from `NULL_CONTENT`, in no state
        in the default context, as the workflow check decides it
        (`scrud_django.core.workflow.prior_of`):

        - ``transition_data`` (`get_transition_data`), and
          ``transition_error``, the workflow error that made it ``None``
          (an invalid transition, or content that meets the conditions of
          more than one state, which a write that skipped the
          ``WorkflowValidator`` still saves), else ``None``;
        - ``transitions``, a read-only mapping (``MappingProxyType``): for
          each context the stored resource or the content is in (stored ones first, as the ``WorkflowValidator``
          checks them) where a policy applies (`workflow_policies_for`), the
          write there. (`rbac_contexts_of` always names one context at
          least; a resource whose `rbac_contexts` named none, as an
          override might, would be reported under ``None`` with the
          settings default, as the ``WorkflowValidator`` checks it:
          defensive.) A context it stays in: the transition under its
          policy (``None`` if invalid); one it enters (or a create's): its
          ``__INITIALIZE__`` (``None`` if the content is in no initial state
          there); one it leaves: the transition there from the stored
          content, flagged ``leaving`` (``None`` if invalid); ``None`` in
          any of them where the content's (or stored content's) state is
          ambiguous. A context without a policy is left out.

        Each context's policy is read once."""
        stored = self._stored_row(using)
        if stored is None:
            prior_data = None
        elif isinstance(stored.content, Mapping):
            prior_data = stored.content
        else:
            prior_data = NULL_CONTENT
        return transitions_between(
            self.resource_type.type_uri,
            prior=prior_data,
            proposed=self.content,
            contexts_before=(
                rbac_contexts_of(prior_data) if prior_data is not None else []
            ),
            # `rbac_contexts_of` never gives no context: None (the settings
            # default) is defensive, for a write in none before or after.
            contexts_after=list(self.rbac_contexts),
            stored_pk=None if stored is None else stored.pk,
        )

    def transition_in(self, workflow, prior_data, entering=False, leaving=False):
        """`scrud_django.core.transitions.transition_in` of this content."""
        return transition_in(
            workflow, prior_data, self.content, entering=entering, leaving=leaving
        )

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
