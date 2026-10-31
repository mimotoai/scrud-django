"""Registering a resource type again (each process start registers every
type) keeps its schema, context and indexing policy resources: under
``DEBUG``, which rewrites the type, it updates them in place instead of
storing new ones, and leaves one whose content is unchanged untouched."""

import json

import pytest
from django.test import override_settings

from scrud_django.models import Resource, ResourceType
from scrud_django.registration import (
    ResourceTypeRegistration,
    register_indexing_policy_resource_type,
    register_json_ld_resource_type,
    register_json_schema_resource_type,
)

TYPE_URI = "tests://DebugRegistered"
SCHEMA = {
    "$schema": "http://json-schema.org/draft-04/schema",
    "properties": {"name": {"type": "string"}},
}
CONTEXT = {"@context": {"name": "http://schema.org/name"}}
INDEXING_POLICY = {"fields": []}


def register(schema=SCHEMA):
    """One process start's registration of the type."""
    return ResourceTypeRegistration(
        TYPE_URI,
        slug="debug-registered",
        schema_func=lambda: json.dumps(schema),
        context_func=lambda: json.dumps(CONTEXT),
        indexing_policy_func=lambda: json.dumps(INDEXING_POLICY),
        register_service=False,
    ).resource_type


def stored():
    """(schema, context, indexing policy) resource counts."""
    return tuple(
        Resource.objects.filter(resource_type=t).count()
        for t in (
            register_json_schema_resource_type(),
            register_json_ld_resource_type(),
            register_indexing_policy_resource_type(),
        )
    )


@pytest.mark.django_db
@pytest.mark.parametrize("debug", [True, False])
def test_registering_a_type_again_stores_no_new_resources(debug):
    with override_settings(DEBUG=debug):
        first = register()
        before = stored()
        schema = Resource.objects.get(pk=first.schema_id)
        for _ in range(3):
            again = register()
    assert stored() == before
    assert ResourceType.objects.filter(type_uri=TYPE_URI).count() == 1
    assert (again.schema_id, again.context_id, again.indexing_policy_id) == (
        first.schema_id,
        first.context_id,
        first.indexing_policy_id,
    )
    stored_type = ResourceType.objects.get(type_uri=TYPE_URI)
    assert stored_type.schema_id == first.schema_id
    assert stored_type.context_id == first.context_id
    assert stored_type.indexing_policy_id == first.indexing_policy_id
    # Unchanged content is not written again: its validators stay.
    unchanged = Resource.objects.get(pk=first.schema_id)
    assert (unchanged.etag, unchanged.modified_at) == (
        schema.etag,
        schema.modified_at,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("debug", [True, False])
def test_a_changed_schema_updates_the_stored_one(debug):
    with override_settings(DEBUG=debug):
        first = register()
        before = stored()
        changed = dict(SCHEMA, required=["name"])
        again = register(changed)
    assert stored() == before
    assert again.schema_id == first.schema_id
    assert Resource.objects.get(pk=first.schema_id).content == changed
