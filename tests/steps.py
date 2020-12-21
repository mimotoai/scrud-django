from datetime import datetime

from django.db.models import Model
from safetydance import step_data
from safetydance_django.steps import http_client, http_response  # noqa: F401
from safetydance_django.test import Http  # noqa: F401
from safetydance_test.step_extension import step_extension

from scrud_django.models import Resource, ResourceType

named_instances = step_data(dict, initializer=dict)


__all__ = [
    'an_instance_named',
    'is_valid_resource',
    'is_valid_resource_type',
    'an_registration_data',
    'check_registration_results',
    'resource_json_is',
]


@step_extension
def an_instance_named(name: str, instance: Model):
    named_instances[name] = instance


@step_extension
def is_valid_resource(name: str):
    instance = named_instances[name]
    assert isinstance(instance, Resource)
    assert isinstance(instance.content, dict)
    assert isinstance(instance.modified_at, datetime)
    assert isinstance(instance.etag, str)
    assert len(instance.etag) == 32


@step_extension
def is_valid_resource_type(name: str):
    instance = named_instances[name]
    assert isinstance(instance, ResourceType)
    assert isinstance(instance.type_uri, str)
    assert len(instance.type_uri) > 0


# register

named_registration_data = step_data(dict, initializer=dict)


@step_extension
def an_registration_data(name: str, registration_data: dict):
    named_registration_data[name] = registration_data


@step_extension
def check_registration_results(name: str, result):
    expected = named_registration_data[name]
    # result = register_resource_type(**expected)
    resource_type_name = list(expected.keys())[0]
    expected = expected[resource_type_name]

    assert result.type_uri == expected['rdf_type_uri']
    assert result.slug == resource_type_name
    assert result.context_uri == expected['json_ld_context_url']
    assert result.schema_uri == expected['json_schema_url']


def resource_envelope_json_is(resource_data: dict):
    assert 'href' in resource_data
    assert isinstance(resource_data['href'], str)

    assert 'last_modified' in resource_data
    assert isinstance(resource_data['last_modified'], str)

    assert 'etag' in resource_data
    assert isinstance(resource_data['etag'], str)

    assert 'content' in resource_data
    assert isinstance(resource_data['content'], dict)

    response = http_response.json()
    assert response["content"] == resource_data["content"]


def resource_json_is(resource_data):
    response = http_response.json()
    assert response == resource_data, response


resource_envelope_json_is = step_extension(
    f=resource_envelope_json_is, target_type=Http
)
resource_json_is = step_extension(f=resource_json_is, target_type=Http)
