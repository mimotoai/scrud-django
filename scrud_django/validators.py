import fastjsonschema
from django.core.exceptions import ImproperlyConfigured
from rest_framework.exceptions import ValidationError
from scoped_rbac import access
from scoped_rbac.permissions import http_action_iri_for, policy_for
from scoped_rbac.policy import Permission

from scrud_django.exceptions import JsonValidationError, WorkflowTransitionError
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


class JsonValidator:
    requires_context = True

    def __init__(self):
        self.validators = {}

    def __call__(self, data, context):
        validate = self.validator_for(context.resource_type)
        data = validate(data)
        return data

    def is_json_schema(self, schema):
        return schema is not None and schema.resource_type.type_uri in [
            "http://json-schema.org/draft-04/schema",
            "http://json-schema.org/draft-06/schema",
            "http://json-schema.org/draft-07/schema",
        ]

    def validator_for(self, resource_type):
        schema = resource_type.schema
        if not self.is_json_schema(schema):
            return lambda r: r
        validator = self.validators.get(resource_type.type_uri, None)
        if validator is None or not validator.matches(resource_type):
            try:
                content = schema.content
                if "$id" in content:
                    del content["$id"]
                validation_func = fastjsonschema.compile(content)
            except Exception as e:
                import logging

                logging.error(e)
                return lambda r: r

            def validate(content, handlers=None, formats=None):
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
        transitions = [
            (c, policy, self.transition_for(policy, data, instance))
            for c, policy in policies
            if policy is not None
        ]
        for c, policy, transition_data in transitions:
            self.authorize(policy, transition_data, c, context)
        return data

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
