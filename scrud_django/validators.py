import copy
import logging
from contextlib import contextmanager

import fastjsonschema
from django.core.exceptions import ImproperlyConfigured
from rest_framework.exceptions import ValidationError
from scoped_rbac import access
from scoped_rbac.permissions import http_action_iri_for, policy_for
from scoped_rbac.policy import Permission

from scrud_django.exceptions import (
    HiddenWorkflowTransitionError,
    JsonSchemaInvalidError,
    JsonValidationError,
    WorkflowTransitionError,
)
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import ResourceType
from scrud_django.permissions import TransitionCheck
from scrud_django.workflow import (
    WORKFLOW_POLICY_TYPE_URI,
    TransitionData,
    WorkflowPolicy,
    default_workflow_policy_for,
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
    content is not changed."""
    content = copy.deepcopy(content)
    if isinstance(content, dict):
        content.pop("$id", None)
    try:
        return fastjsonschema.compile(content, handlers=RefuseEveryScheme())
    except Exception as e:
        raise JsonSchemaInvalidError(f"The JSON Schema does not compile: {e}") from e


class JsonValidator:
    """Validates a body against its type's JSON Schema. A body written to a
    JSON Schema type is itself a schema, and must compile
    (`compile_json_schema`). A stored schema that does not compile fails
    closed: every write of its type is refused with ``JsonSchemaInvalidError``
    (400)."""

    requires_context = True

    def __init__(self):
        self.validators = {}

    def __call__(self, data, context):
        if context.resource_type.type_uri in JSON_SCHEMA_TYPE_URIS:
            compile_json_schema(data)
        validate = self.validator_for(context.resource_type)
        data = validate(data)
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
    context. All of them must pass. A resource in no context is checked under
    the settings default, and only a superuser may make its transition, as
    scoped-rbac's object checks allow.

    A ``WorkflowPolicy`` resource is itself checked when it is written (see
    `WorkflowPolicy`): a policy that fails those checks is a 400 and is not
    stored. Policies stored before those checks existed are not rechecked.
    """

    requires_context = True

    def __call__(self, data, context):
        resource_type = context.resource_type
        if resource_type.type_uri == WORKFLOW_POLICY_TYPE_URI:
            self.validate_policy(data)
        instance = context.instance
        contexts = rbac_contexts_of(data)
        if instance is not None:
            contexts = list(
                dict.fromkeys(rbac_contexts_of(instance.content) + contexts)
            )
        if contexts:
            policies = [(c, workflow_policy_for(resource_type, c)) for c in contexts]
        else:
            policies = [(None, default_workflow_policy_for(resource_type))]
        request = context.context["request"]
        transitions = []
        for c, policy in policies:
            if policy is None:
                continue
            with self.errors_hidden_unless_readable(policy, request):
                transitions.append(
                    (c, policy, self.transition_for(policy, data, instance))
                )
        for c, policy, transition_data in transitions:
            with self.errors_hidden_unless_readable(policy, request):
                self.authorize(policy, transition_data, c, context)
        return data

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
                access.body_mapping(stored.content),
                stored.rbac_contexts,
            ):
                raise HiddenWorkflowTransitionError() from e
            raise

    def validate_policy(self, data):
        try:
            WorkflowPolicy(data)
        except ImproperlyConfigured as e:
            raise ValidationError({"workflow_policy": [str(e)]}) from e

    def transition_for(self, workflow_policy, data, instance):
        if instance is None:
            workflow_policy.validate_initial_state(data)
            return TransitionData(None, data, "__INITIALIZE__")
        return workflow_policy.determine_transition(data, instance.content)

    def authorize(self, workflow_policy, transition_data, rbac_context, context):
        """Raise unless the user may make `transition_data`'s transition in
        `rbac_context` (``None``: the resource is in no context)."""
        request = context.context["request"]
        permission = Permission(
            http_action_iri_for(request), context.resource_type.type_uri
        )
        transition = TransitionCheck(transition_data.transition)
        if rbac_context is None:
            # No context: only a superuser (as scoped-rbac's object checks).
            allowed = access.may_act_on(request, permission, transition, [])
        else:
            allowed = policy_for(request).should_allow(
                permission, rbac_context, resource=transition
            )
        if not allowed:
            raise WorkflowTransitionError(
                workflow_policy.reference,
                transition_data.prior_state,
                transition_data.proposed_state,
                message="The user isn't authorized to perform this state transition on this resource.",
            )
