import fastjsonschema

from scrud_django.exceptions import JsonValidationError
from scrud_django.models import ResourceType


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
