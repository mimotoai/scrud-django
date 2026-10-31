from collections.abc import Mapping

from django.db import models, router
from django.db.models import JSONField
from django.utils.translation import gettext_lazy as _
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.models.resource_type import ResourceType
from scrud_django.workflow import (
    WORKFLOW_POLICY_TYPE_URI,
    TransitionData,
    WorkflowInvalidInitialStateError,
    WorkflowInvalidStateError,
    WorkflowTransitionError,
    default_workflow_policy_for,
    workflow_policy_for,
)

from ..scrud_signals import scrud_post_delete, scrud_post_save

workflow_policy_type_uri = WORKFLOW_POLICY_TYPE_URI


def rbac_contexts_of(content):
    """The contexts a resource's `content` is in: its ``rbac_context`` when it is
    a string or a list or tuple of strings, else ``DEFAULT_CONTEXT``, the
    context scoped-rbac authorizes a POST that names none in. Without it a
    resource that names none would have no context, which no role grants.
    Absent, ``None`` and ``""`` are all the default context. The one reading of
    a resource's contexts: object checks (``Resource.rbac_contexts``, and
    scoped-rbac's default ``contexts_for`` on the scrud views, which give
    checks the content), and the workflow checks (``WorkflowValidator``) all
    use it."""
    context = content.get("rbac_context") if isinstance(content, Mapping) else None
    if isinstance(context, str):
        return [context]
    if isinstance(context, (list, tuple)) and all(
        isinstance(item, str) for item in context
    ):
        return list(context)
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

    def save(
        self, force_insert=False, force_update=False, using=None, update_fields=None
    ) -> None:
        transition_data = self.get_transition_data(using)
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
            using=using,
            resource_type_uri=self.resource_type.type_uri,
            transition_data=transition_data,
            action=action,
        )

    def delete(self, using=None, keep_parents=False) -> tuple[int, dict[str, int]]:
        res = super().delete(using=using, keep_parents=keep_parents)
        scrud_post_delete.send(
            sender=self.__class__,
            instance=self,
            using=using,
            resource_type_uri=self.resource_type.type_uri,
            action='DELETE',
        )
        return res

    def get_transition_data(self, using=None):
        """The `TransitionData` ``scrud_post_save`` reports for this save, from
        the stored row (read on the database the save writes to, `using` or
        the router's for this instance, not a read replica) to the content,
        under the policy `get_workflow_policy` picks. A create, and an update
        whose picked context is one the stored resource was not in (a move
        into it), are reported as ``__INITIALIZE__`` there (no prior state, the
        initial state the content is in), as the ``WorkflowValidator`` checks
        them."""
        if self._state.adding:
            prior_data = None
        else:
            db = using or router.db_for_write(type(self), instance=self)
            prior_version = self.__class__.objects.using(db).filter(pk=self.pk)
            prior_data = prior_version.first().content
        context, workflow = self.workflow_context_and_policy()
        if workflow is None:
            return None
        entering = (
            prior_data is not None
            and context is not None
            and context not in rbac_contexts_of(prior_data)
        )
        if prior_data is None or entering:
            # A create has no prior state; its proposed state is the initial
            # state the content is in (``None`` if none: this is a report, the
            # `WorkflowValidator` validates).
            initial_state = workflow.determine_state(
                self.content, workflow.initial_state_expressions
            )
            return TransitionData(initial_state, None, "__INITIALIZE__")
        try:
            return workflow.determine_transition(self.content, prior_data)
        except (
            WorkflowInvalidInitialStateError,
            WorkflowInvalidStateError,
            WorkflowTransitionError,
        ):
            """ Workflow validation shouldn't happen in here.
                hence we ignore the exceptions. """
            return None

    def get_workflow_policy(self):
        """The policy whose transition the ``scrud_post_save`` signal reports.

        For a resource in several contexts it is the policy in effect
        (`workflow_policy_for`: the stored policy there, else the settings
        default) in the **first** context its content names, in the order of
        its ``rbac_context`` list; for one in a single context, that context's;
        for one in no context, the settings default. Its content decides, so
        it is deterministic. A context with neither is skipped; ``None`` when
        none has one. Validation and authorization are the
        `WorkflowValidator`'s, which checks every context."""
        return self.workflow_context_and_policy()[1]

    def workflow_context_and_policy(self):
        """``(context, policy)`` of `get_workflow_policy`: the context whose
        policy it is (``None`` for a resource in no context), or
        ``(None, None)``."""
        contexts = self.rbac_contexts
        if not contexts:
            return None, default_workflow_policy_for(self.resource_type)
        for context in contexts:
            policy = workflow_policy_for(self.resource_type, context)
            if policy is not None:
                return context, policy
        return None, None

    class Meta:
        abstract = True
