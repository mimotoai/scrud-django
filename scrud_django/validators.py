import copy
import logging
from collections.abc import Mapping
from contextlib import contextmanager

import fastjsonschema
from django.core.exceptions import ImproperlyConfigured
from scoped_rbac import access
from scoped_rbac.permissions import drf_error, http_action_iri_for, policy_for
from scoped_rbac.policy import Permission

from scrud_django.exceptions import (
    HiddenWorkflowTransitionError,
    JsonSchemaInvalidError,
    JsonValidationError,
    WorkflowPolicyInvalidError,
    WorkflowTransitionError,
)
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import ResourceType
from scrud_django.permissions import BodyCheck, TransitionCheck, resource_check
from scrud_django.workflow import (
    WORKFLOW_POLICY_TYPE_URI,
    WorkflowPolicy,
    default_workflow_policy_for,
    stored_policies_for,
    workflow_policy_for,
)

logger = logging.getLogger(__name__)


class ValidatorEntry:
    def __init__(self, resource_type: ResourceType, validate: callable):
        self.type_uri = resource_type.type_uri
        self.revision = resource_type.revision
        self.etag = resource_type.schema.etag
        self.modified_at = resource_type.schema.modified_at
        self.validate = validate

    def matches(self, resource_type: ResourceType):
        return (
            self.type_uri == resource_type.type_uri
            and self.revision == resource_type.revision
            and self.etag == resource_type.schema.etag
            and self.modified_at == resource_type.schema.modified_at
        )


JSON_SCHEMA_TYPE_URIS = (
    "http://json-schema.org/draft-04/schema",
    "http://json-schema.org/draft-06/schema",
    "http://json-schema.org/draft-07/schema",
)


class RefusedRemoteRef(fastjsonschema.JsonSchemaDefinitionException):
    pass


def refuse_remote_ref(uri):
    raise RefusedRemoteRef(
        f"$ref {uri!r} is not allowed: a $ref must point into the schema itself "
        "(start with '#')."
    )


class RefuseEveryScheme(dict):
    """fastjsonschema's ``handlers``: it fetches a ``$ref`` outside the schema
    with the handler for its URI scheme, else with ``urlopen``. This mapping has
    a handler for every scheme (``http``, ``file``, none...), which refuses."""

    def __contains__(self, scheme):
        return True

    def __getitem__(self, scheme):
        return refuse_remote_ref

    def get(self, scheme, default=None):
        return refuse_remote_ref


def compile_json_schema(content):
    """Compile JSON Schema `content` with fastjsonschema, resolving ``$ref`` only
    within it. Raises ``JsonSchemaInvalidError`` if it does not compile, a
    ``$ref`` to anything else (a URL, a file, another document) included. The
    content is not changed. The validator applies no ``default``
    (``use_default=False``): it validates the data as it is, and leaves it as
    it is."""
    content = copy.deepcopy(content)
    if isinstance(content, dict):
        content.pop("$id", None)
    try:
        return fastjsonschema.compile(
            content, handlers=RefuseEveryScheme(), use_default=False
        )
    except Exception as e:
        raise JsonSchemaInvalidError(f"The JSON Schema does not compile: {e}") from e


class JsonValidator:
    """Validates a body against its type's JSON Schema. A body written to a
    JSON Schema type is itself a schema, and must compile
    (`compile_json_schema`). A stored schema that does not compile fails
    closed: every write of its type is refused with ``JsonSchemaInvalidError``
    (400).

    JSON Schema ``default`` values are not applied: the validator is compiled
    with ``use_default=False`` (`compile_json_schema`), so it validates `data`,
    the body that is stored, exactly as it was sent (a default can neither
    make it valid, for example through ``if``/``then``, nor be stored), and
    leaves it unchanged."""

    requires_context = True

    def __init__(self):
        self.validators = {}

    def __call__(self, data, context):
        if context.resource_type.type_uri in JSON_SCHEMA_TYPE_URIS:
            compile_json_schema(data)
        validate = self.validator_for(context.resource_type)
        validate(data)
        return data

    def is_json_schema(self, schema):
        return (
            schema is not None
            and schema.resource_type.type_uri in JSON_SCHEMA_TYPE_URIS
        )

    def validator_for(self, resource_type):
        schema = resource_type.schema
        if not self.is_json_schema(schema):
            return lambda r: r
        validator = self.validators.get(resource_type.type_uri, None)
        if validator is None or not validator.matches(resource_type):
            try:
                validation_func = compile_json_schema(schema.content)
            except JsonSchemaInvalidError as error:
                # The writer of a resource may not be the schema's author: the
                # reason is logged, not sent.
                logger.error(
                    "The JSON Schema of %s does not compile, so its writes are "
                    "refused: %s",
                    resource_type.type_uri,
                    error.detail,
                )

                def validate(content):
                    raise JsonSchemaInvalidError(
                        "The JSON Schema of this resource type does not compile, "
                        "so it cannot be validated."
                    )

            else:

                def validate(content):
                    try:
                        return validation_func(content)
                    except fastjsonschema.JsonSchemaException as e:
                        raise JsonValidationError(e) from e

            validator = ValidatorEntry(resource_type, validate)
            self.validators[resource_type.type_uri] = validator
        return validator.validate


def check_policy_is_unique(content, pk=None):
    """Refuse (`WorkflowPolicyInvalidError`, 400) the ``WorkflowPolicy``
    `content` (of the resource `pk`, ``None`` for a create) when another
    stored policy for its type applies in one of its contexts. Run by
    `WorkflowValidator.validate_policy`, and again after the save under
    `lock_workflow_policy_type` (``ResourceViewMixin.save_checked``), so that
    of two concurrent writes the second is refused."""
    resource_type = content.get("resource_type")
    for rbac_context in rbac_contexts_of(content):
        others = [
            policy
            for policy in stored_policies_for(resource_type, rbac_context)
            if policy.pk != pk
        ]
        if others:
            raise WorkflowPolicyInvalidError(
                f"A WorkflowPolicy for {resource_type!r} already exists in "
                f"context {rbac_context!r}; update or delete it instead."
            )


def rbac_permissions_of(view):
    """The ``IsAuthorized`` (or subclass) permissions of `view` (nested ones
    included: ``ResourceViewMixin.get_rbac_permissions``), or ``[]`` for no
    view or one without them."""
    get_rbac_permissions = getattr(view, "get_rbac_permissions", None)
    return list(get_rbac_permissions()) if callable(get_rbac_permissions) else []


class WorkflowValidator:
    """Checks a create or update of a resource against the workflow policy of
    every context concerned (`workflow_policy_for`: the context's stored policy,
    else the settings default).

    The contexts are those the new content names (`rbac_contexts_of`) and, for
    an update, those the stored resource is in as well, so that moving a
    resource out of a context is checked under that context's policy too. In
    each of them that has a policy, a create must be a valid initial state and
    an update a valid transition, and the user must be authorized for the
    transition (a `TransitionCheck` for the ``scrud_workflow`` operator) in that
    context. A context an update moves the resource into (one the body names
    and the stored resource is not in) is checked as a create there: the body
    must be a valid initial state and the user authorized for
    ``__INITIALIZE__`` by the request's method (``http.PUT``, not
    ``http.POST``). All of them must pass. A resource in no context is checked under
    the settings default, and only a superuser may make its transition, as
    scoped-rbac's object checks allow. In a context where no policy applies, a
    grant conditioned on ``scrud_workflow`` allows nothing
    (`authorize_without_policy`: 403). The operator reads the
    `TransitionCheck`; a CEL condition reads the body.

    An error under a stored policy the requester may not read names neither
    the policy nor its states (`errors_hidden_unless_readable`).

    A ``WorkflowPolicy`` resource is itself checked when it is written
    (`validate_policy`): a policy that fails those checks, names a context that
    is not a string, or would be a second policy for its type in its context
    is a 400 and is not stored. Policies stored before those checks existed
    (or server-side) are not rechecked on write; the database system check
    reports those that fail them (``scrud_django.E005``).
    """

    requires_context = True

    def __call__(self, data, context):
        resource_type = context.resource_type
        instance = context.instance
        data = self.body_of(data, context)
        if resource_type.type_uri == WORKFLOW_POLICY_TYPE_URI:
            self.validate_policy(data, instance)
        stored_contexts = (
            rbac_contexts_of(instance.content) if instance is not None else []
        )
        contexts = list(dict.fromkeys(stored_contexts + rbac_contexts_of(data)))
        if contexts:
            policies = [(c, workflow_policy_for(resource_type, c)) for c in contexts]
        else:
            policies = [(None, default_workflow_policy_for(resource_type))]
        # The request is needed only where a policy applies; without one (a
        # serializer used outside a request) there is no user whose grants a
        # context without a policy could condition.
        request = context.context.get("request")
        if request is None and any(p is not None for _, p in policies):
            self.request_of(context)
        transitions = []
        for c, policy in policies:
            if policy is None:
                continue
            # A context the resource enters (not one it is stored in) has
            # no prior state there: the write creates it there.
            entering = instance is not None and c is not None
            entering = entering and c not in stored_contexts
            with self.errors_hidden_unless_readable(policy, request):
                transitions.append(
                    (
                        c,
                        policy,
                        self.transition_for(policy, data, instance, entering),
                    )
                )
        for c, policy, transition_data in transitions:
            with self.errors_hidden_unless_readable(policy, request):
                self.authorize(policy, transition_data, c, context, data)
        if request is not None:
            for c, policy in policies:
                if policy is None:
                    self.authorize_without_policy(c, context, data)
        return data

    @staticmethod
    def body_of(data, context):
        """What the check reads: the body as sent (the serializer's
        ``initial_data``, which is what is stored), not `data`, the copy DRF
        passes to validators, which another validator may have changed (a
        JSON Schema default filled in, for example). `data` when the
        serializer has no mapping ``initial_data``."""
        initial_data = getattr(context, "initial_data", None)
        return initial_data if isinstance(initial_data, Mapping) else data

    def request_of(self, context):
        """The request of the serializer `context`; the workflow check
        authorizes it. Raises ``ImproperlyConfigured`` without one."""
        request = getattr(context, "context", {}).get("request")
        if request is None:
            raise ImproperlyConfigured(
                "WorkflowValidator needs the request in the serializer context "
                "(context={'request': request}) to authorize workflow transitions."
            )
        return request

    @contextmanager
    def errors_hidden_unless_readable(self, workflow_policy, request):
        """A workflow error under a stored policy the requester may not read
        (``http.GET`` on ``WorkflowPolicy`` in none of its contexts) becomes a
        `HiddenWorkflowTransitionError`, which names neither the policy nor
        its states: the resource may be in a context of someone else's.
        Errors under readable policies and settings defaults are unchanged."""
        try:
            yield
        except WorkflowTransitionError as e:
            stored = workflow_policy.resource
            if stored is not None and not access.may_read(
                request,
                WORKFLOW_POLICY_TYPE_URI,
                resource_check(stored),
                stored.rbac_contexts,
            ):
                raise HiddenWorkflowTransitionError() from e
            raise

    def validate_policy(self, data, instance):
        """Refuse (`WorkflowPolicyInvalidError`, 400, with the reason as its
        ``detail``) a ``WorkflowPolicy`` write `data` (replacing `instance`,
        ``None`` for a create) that fails `WorkflowPolicy`'s checks, whose
        ``rbac_context`` is present but not a string (a policy is in exactly
        one context), or that would be a second stored policy for its type in
        its context."""
        if "rbac_context" in data and not isinstance(data["rbac_context"], str):
            raise WorkflowPolicyInvalidError(
                "A WorkflowPolicy's rbac_context must be a string (one context), "
                f"not {data['rbac_context']!r}."
            )
        try:
            WorkflowPolicy(data)
        except ImproperlyConfigured as e:
            raise WorkflowPolicyInvalidError(str(e)) from e
        check_policy_is_unique(data, instance.pk if instance is not None else None)

    def transition_for(self, workflow_policy, data, instance, entering=False):
        """The `TransitionData` of writing `data` under `workflow_policy`: a
        create's ``__INITIALIZE__`` (`data` must be an initial state) when
        there is no stored `instance`, or when the write moves the resource
        into this context (`entering`); else the transition from the stored
        content."""
        if instance is None or entering:
            return workflow_policy.initial_transition(data)
        return workflow_policy.determine_transition(data, instance.content)

    def authorize(
        self, workflow_policy, transition_data, rbac_context, context, data=None
    ):
        """Raise unless the user may make `transition_data`'s transition in
        `rbac_context` (``None``: the resource is in no context)."""
        if not self.allowed(context, rbac_context, transition_data.transition, data):
            raise WorkflowTransitionError(
                workflow_policy.reference,
                transition_data.prior_state,
                transition_data.proposed_state,
                message="The user isn't authorized to perform this state transition on this resource.",
            )

    def authorize_without_policy(self, rbac_context, context, data):
        """Where no workflow policy applies there is no transition: a grant
        conditioned on ``scrud_workflow`` allows nothing there (its operator
        gets a `TransitionCheck` whose transition is ``None``), and the write
        is refused with 403 (``PermissionDenied``) when such a grant is the
        reason it would otherwise be allowed. Where the user holds no grant at
        all, or an unconditional or CEL one, the object and post-save checks
        decide, as before."""
        if self.allowed(context, rbac_context, None, data):
            return
        if not self.allowed(context, rbac_context, data=data, transition_check=False):
            return
        raise drf_error(access.AccessDenied(), self.request_of(context))

    def allowed(
        self, context, rbac_context, transition=None, data=None, transition_check=True
    ):
        """Whether the request's action on the type is allowed in `rbac_context`
        (``None``: no context, superusers only, as scoped-rbac's object checks)
        on a `TransitionCheck` of `transition`: the ``scrud_workflow`` operator
        reads its transition, any other operator reads it as the body `data` (it
        is a mapping over it), and a CEL condition reads the body, as in
        scoped-rbac's request check. Without `transition_check`, on the body
        alone.

        The request's policy must allow it, and, with a view in the
        serializer context (the scrud views'), so must each of the view's
        ``IsAuthorized`` permissions (`rbac_permissions_of`):
        ``has_context_permission(request, view, check, rbac_context)``. An
        ``IsAuthorized`` subclass can therefore refuse a transition, never
        grant one the policy refuses: its ``obj`` here is the
        `TransitionCheck`, whose mapping is the request body as the writer
        sent it (a check written for stored fields, such as an owner, would
        read the writer's claim). Without a view (a serializer used outside
        the views), the policy alone decides."""
        request = self.request_of(context)
        permission = Permission(
            http_action_iri_for(request), context.resource_type.type_uri
        )
        body = access.body_mapping(data)
        # Read-only at every level: an operator cannot change what is stored.
        resource = (
            TransitionCheck(transition, body) if transition_check else BodyCheck(body)
        )
        if rbac_context is None:
            return access.may_act_on(request, permission, resource, [])
        if not policy_for(request).should_allow(
            permission, rbac_context, resource=resource
        ):
            return False
        # The view's IsAuthorized permissions may refuse what the policy
        # allows, never allow what it refuses: here `resource` reads the
        # request body, which the writer chose.
        view = getattr(context, "context", {}).get("view")
        return all(
            leaf.has_context_permission(request, view, resource, rbac_context)
            for leaf in rbac_permissions_of(view)
        )
