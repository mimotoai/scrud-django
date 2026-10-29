import json
from datetime import datetime

from django.db.models import Model
from jsonpath_ng import parse
from rdflib import Graph, URIRef
from safetydance import context_data, initializer, step, step_namespace
from safetydance_django.steps import json_values_match
from safetydance_django.test import Http

from scrud_django.models import Resource, ResourceType

__all__ = [
    'Scrud',
    'an_instance_named',
    'is_valid_resource',
    'is_valid_resource_type',
    'an_registration_data',
    'check_registration_results',
    'json_path_matches',
    'ld_context_states_sub_class_of',
    'resource_envelope_json_is',
    'resource_json_is',
    'response_json_matches',
]

http_step = step_namespace("http")


@context_data
class Scrud:
    named_instances: dict = initializer(lambda ctx: {})
    named_registration_data: dict = initializer(lambda ctx: {})
    json_path_expressions: dict = initializer(lambda ctx: {})
    json_path_match: list


sub_class_of = URIRef("http://www.w3.org/2000/01/rdf-schema#subClassOf")


@step
def an_instance_named(name: str, instance: Model):
    Scrud.named_instances[name] = instance


@step
def is_valid_resource(name: str):
    instance = Scrud.named_instances[name]
    assert isinstance(instance, Resource)
    assert isinstance(instance.content, dict)
    assert isinstance(instance.modified_at, datetime)
    assert isinstance(instance.etag, str)
    assert len(instance.etag) == 32


@step
def is_valid_resource_type(name: str):
    instance = Scrud.named_instances[name]
    assert isinstance(instance, ResourceType)
    assert isinstance(instance.type_uri, str)
    assert len(instance.type_uri) > 0


@step
def ld_context_states_sub_class_of(subclass_uri, superclass_uri):
    data = Http.response.json()
    g = Graph().parse(data=json.dumps(data), format='json-ld')
    superclasses = list(g.transitive_objects(subclass_uri, sub_class_of))
    assert len(superclasses) > 0, superclasses
    assert superclass_uri in superclasses, superclasses


# register


@step
def an_registration_data(name: str, registration_data: dict):
    Scrud.named_registration_data[name] = registration_data


@step
def check_registration_results(name: str, result):
    expected = Scrud.named_registration_data[name]
    # result = register_resource_type(**expected)
    resource_type_name = list(expected.keys())[0]
    expected = expected[resource_type_name]

    assert result.type_uri == expected['rdf_type_uri']
    assert result.slug == resource_type_name
    assert result.context_uri == expected['json_ld_context_url']
    assert result.schema_uri == expected['json_schema_url']


def get_compiled_expression(expr, compiled_expressions):
    compiled_expr = compiled_expressions.get(expr, None)
    if compiled_expr is None:
        compiled_expr = parse(expr)
        compiled_expressions[expr] = compiled_expr
    return compiled_expr


@step
def json_path_matches(expr, condition):
    data = Http.response.json()
    compiled_expr = get_compiled_expression(expr, Scrud.json_path_expressions)
    Scrud.json_path_match = compiled_expr.find(data)
    assert len(Scrud.json_path_match) > 0, data
    match = Scrud.json_path_match[0].value
    assert condition(Scrud.json_path_match), (
        f"expr: {expr} \ncondition: {condition}, \n match: {match}"
    )


@http_step
def resource_envelope_json_is(resource_data: dict):
    assert 'href' in resource_data
    assert isinstance(resource_data['href'], str)

    assert 'last_modified' in resource_data
    assert isinstance(resource_data['last_modified'], str)

    assert 'etag' in resource_data
    assert isinstance(resource_data['etag'], str)

    assert 'content' in resource_data
    assert isinstance(resource_data['content'], dict)

    response = Http.response.json()
    assert response["content"] == resource_data["content"]


@http_step
def resource_json_is(resource_data):
    response = Http.response.json()
    assert response == resource_data, response


@http_step
def response_json_matches(expected):
    observed = Http.response.json()
    assert json_values_match(expected, observed), Http.response
