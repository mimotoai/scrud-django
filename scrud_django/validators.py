import fastjsonschema
from rest_framework import exceptions


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
        if validator is None:
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
                    raise exceptions.ValidationError(
                        {
                            e.name: {
                                "message": e.message,
                                "definition": e.definition,
                                "rule": e.rule,
                                "rule_definition": e.rule_definition,
                            }
                        }
                    )

            self.validators[resource_type.type_uri] = validate
            validator = validate
        return validator
