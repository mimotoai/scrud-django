"""Every JSON Schema the library serves compiles: each resource type's
collection schema (``collections-json-schema/<slug>/``), which a list's
``Link`` names as its ``describedby``, and each JSON Schema resource
(``json-schema/<id>/``), which an item's ``Link`` names. A list's body
validates against its collection schema."""

import fastjsonschema
import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from scrud_django.models import Resource, ResourceType
from scrud_django.registration import (
    ResourceRegistration,
    register_indexing_policy_resource_type,
    workflow_policy_resource_type,
)
from scrud_django.validators import compile_json_schema

from .factories import UserFactory

# A $ref to a document outside the collection schema (a type registered with
# a schema_uri) names what a client fetches; the test does not fetch it.
REMOTE = {scheme: (lambda uri: {}) for scheme in ("http", "https", "tests", "")}

TODO = {
    "title": "plan",
    "status": "not started",
    "priority": "high",
    "assignee": "http://somewhere.example/someone",
    "blockers": [{"explanation": "waiting"}],
}
POLICY = {
    "resource_type": "tests://ToDo",
    "initial_states": {"not started": 'status == "not started"'},
    "states": {"in progress": 'status == "in progress"'},
    "transitions": {"start": {"from": "not started", "to": "in progress"}},
}


def superuser():
    client = APIClient()
    client.force_authenticate(UserFactory(is_superuser=True, is_staff=True))
    return client


def registered_types():
    reverse("partner-profiles-list")  # importing tests.urls registers them
    workflow_policy_resource_type()
    register_indexing_policy_resource_type()
    return list(ResourceType.objects.order_by("slug"))


def compile_served(schema):
    schema = dict(schema)
    schema.pop("$id", None)
    return fastjsonschema.compile(schema, handlers=REMOTE)


@pytest.mark.django_db
def test_every_collection_schema_compiles():
    client = superuser()
    types = registered_types()
    assert {"json-schema", "json-ld", "partner-profiles", "todos"} <= {
        t.slug for t in types
    }
    for resource_type in types:
        response = client.get(
            reverse("collections-json-schema", args=[resource_type.slug])
        )
        assert response.status_code == 200, (resource_type.slug, response.content)
        try:
            compile_served(response.json())
        except Exception as e:  # pragma: no cover - the assertion's message
            pytest.fail(f"{resource_type.slug}: {type(e).__name__}: {e}")


@pytest.mark.django_db
def test_every_json_schema_resource_compiles():
    client = superuser()
    registered_types()
    schema_type = ResourceType.objects.get(slug="json-schema")
    schemas = Resource.objects.filter(resource_type=schema_type)
    assert schemas.count() >= 4  # the library's two, the tests' two
    for schema in schemas:
        response = client.get(reverse("json-schema-detail", args=[schema.pk]))
        assert response.status_code == 200, response.content
        compile_json_schema(response.json())


@pytest.mark.django_db
@pytest.mark.parametrize(
    "slug, content",
    [
        ("partner-profiles", {"name": "x"}),
        ("todos", TODO),
        ("workflow-policies", POLICY),
        ("json-ld", {"@context": {"name": "http://schema.org/name"}}),
        ("json-schema", {"type": "object"}),
    ],
)
def test_a_list_validates_against_its_collection_schema(slug, content):
    client = superuser()
    registered_types()
    ResourceRegistration.register(dict(content), slug)
    validate = compile_served(
        client.get(reverse("collections-json-schema", args=[slug])).json()
    )
    list_url = reverse(ResourceType.objects.get(slug=slug).route_name_list())
    body = client.get(list_url, {"page_size": 1}).json()
    assert body["content"], body
    assert body["previous"] is None, body
    validate(body)
    if body["next"] is not None:
        # A page past the first has a previous link (and maybe no next).
        second = client.get(body["next"]).json()
        assert second["previous"] is not None, second
        validate(second)


@pytest.mark.django_db
def test_a_collection_schema_refuses_a_list_whose_content_is_not_of_the_type():
    client = superuser()
    registered_types()
    validate = compile_served(
        client.get(
            reverse("collections-json-schema", args=["workflow-policies"])
        ).json()
    )
    body = {
        "count": 1,
        "page_count": 1,
        "next": None,
        "previous": None,
        "content": [
            {
                "href": "http://testserver/workflow-policies/1/",
                "etag": '"x"',
                "last_modified": "Thu, 01 Jan 1970 00:00:00 GMT",
                "link": "",
                "content": dict(POLICY, initial_states={"not started": 5}),
            }
        ],
    }
    with pytest.raises(fastjsonschema.JsonSchemaValueException):
        validate(body)
