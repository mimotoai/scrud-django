import fastjsonschema

from scrud_django.exceptions import JsonValidationError
from scrud_django.models import Resource, ResourceType
from scrud_django.registration import (
    WORKFLOW_POLICY_RESOURCE_TYPE_ as WorkflowPolicyResourceType,
)
from scrud_django.workflow import WorkflowPolicy


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

            def validate(content, handlers={}, formats={}):
                try:
                    return validation_func(content)
                except fastjsonschema.JsonSchemaException as e:
                    raise JsonValidationError(e)

            validator = ValidatorEntry(resource_type, validate)
            self.validators[resource_type.type_uri] = validator
        return validator.validate


class WorkflowValidator:
    requires_context = True

    def __init__(self):
        self.workflow_policies = {}

    def __call__(self, data, context):
        resource_type = context.resource_type
        policies = Resource.objects.filter(data__resource_type=resource_type.type_uri)
        # TODO policy should be based on security context!!
        for policy in policies:
            workflow_policy = self.workflow_policies.get(
                (policy.id, policy.version), None
            )
            if workflow_policy is None:
                workflow_policy = WorkflowPolicy(policy.content)
                self.workflow_policies[(policy.id, policy.version)] = workflow_policy
            instance = context.instance
            if instance is None:
                workflow_policy.validate_initial_state(data)
            else:
                workflow_policy.validate_transition(data, instance.content)
