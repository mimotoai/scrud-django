import json
from collections.abc import Mapping
from datetime import datetime
from importlib.util import find_spec

import safetydance_django.steps as http_steps
from django.db.models import Model
from django.test import Client
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from jsonpath_ng import parse
from rdflib import Graph, URIRef
from safetydance import context_data, initializer, step, step_namespace
from safetydance_django.steps import json_values_match
from safetydance_django.test import Http

from scrud_django.models import Resource, ResourceType

__all__ = [
    'PlainClient',
    'Scrud',
    'drf_importable',
    'use_plain_client',
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


def drf_importable() -> bool:
    """Whether DRF can be imported (the suite's check for it: tests patch it).
    The no-DRF run's blocker (`tests.no_drf.Blocker`) refuses it rather than
    not finding it."""
    try:
        return find_spec("rest_framework") is not None
    except ModuleNotFoundError:
        return False


#: DRF's multipart content type (its renderer adds the charset).
MULTIPART = f"{MULTIPART_CONTENT}; charset=utf-8"


class PlainClient(Client):
    """The ``Http`` steps' client when DRF is not used: ``django.test.Client``
    (CSRF checks off, as the test client has them) that takes DRF's
    ``format="json"`` or ``"multipart"`` as a body (`_body`), so a step reads the same for either client.
    ``force_login`` and ``login`` are the test client's; there is no
    ``force_authenticate``."""

    FORMATS = ("json", "multipart")

    def _body(self, kwargs):
        """The request's ``data`` and ``content_type`` as ``APIClient`` makes
        them: a ``format`` (``"json"``, or ``"multipart"``, which is also the
        default for a ``Mapping`` body without ``content_type``, as in DRF's
        test settings) is encoded; an explicit ``content_type`` is kept, a
        ``Mapping`` body of a JSON one serialized. Any other ``format`` is a
        ``ValueError``. The multipart content type has DRF's ``charset``."""
        format = kwargs.pop("format", None)
        if format is not None and format not in self.FORMATS:
            raise ValueError(f"format {format!r} is not one of {self.FORMATS}")
        data = kwargs.get("data")
        if kwargs.get("content_type") is None and format is None:
            format = "multipart" if isinstance(data, Mapping) else None
        if kwargs.get("content_type") is None and format == "json":
            kwargs["content_type"] = "application/json"
        elif kwargs.get("content_type") is None and format == "multipart":
            kwargs["content_type"] = MULTIPART
        if data is not None and not isinstance(data, str | bytes):
            if kwargs.get("content_type") == "application/json":
                kwargs["data"] = json.dumps(
                    data, separators=(",", ":"), ensure_ascii=False
                ).encode()
            elif kwargs.get("content_type") == MULTIPART:
                kwargs["data"] = encode_multipart(BOUNDARY, data)
        return kwargs

    def post(self, path, data=None, **kwargs):
        return super().post(path, **self._body({"data": data, **kwargs}))

    def put(self, path, data="", **kwargs):
        return super().put(path, **self._body({"data": data, **kwargs}))

    def patch(self, path, data="", **kwargs):
        return super().patch(path, **self._body({"data": data, **kwargs}))

    def delete(self, path, data="", **kwargs):
        return super().delete(path, **self._body({"data": data, **kwargs}))


def use_plain_client(monkeypatch=None):
    """Make the ``Http`` steps create a `PlainClient` instead of DRF's
    ``APIClient``. safetydance-django's ``Http.api_client`` initializer calls
    its module's ``_new_api_client`` when a test first needs the client, so
    replacing that, and registering no step twice, is the whole override.
    Without ``monkeypatch`` it holds for the session (DRF absent); with it
    (the ``plain_client`` fixture) for one test."""
    if monkeypatch is None:
        http_steps._new_api_client = PlainClient
    else:
        monkeypatch.setattr(http_steps, "_new_api_client", PlainClient)


if not drf_importable():
    use_plain_client()


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
