"""Registering a resource type again (each process start registers every
type) keeps its schema, context and indexing policy resources: under
``DEBUG``, which rewrites the type, it updates them in place instead of
storing new ones, and leaves one whose content is unchanged untouched. A
type whose row and documents are unchanged is not written at all."""

import json

import pytest
from django.db import connections
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from scrud_django.models import Resource, ResourceType
from scrud_django.registration import (
    ResourceTypeRegistration,
    register_indexing_policy_resource_type,
    register_json_ld_resource_type,
    register_json_schema_resource_type,
    type_db,
)

TYPE_URI = "tests://DebugRegistered"
SCHEMA = {
    "$schema": "http://json-schema.org/draft-04/schema",
    "properties": {"name": {"type": "string"}},
}
CONTEXT = {"@context": {"name": "http://schema.org/name"}}
INDEXING_POLICY = {"fields": []}


def register(schema=SCHEMA, revision="1"):
    """One process start's registration of the type."""
    return ResourceTypeRegistration(
        TYPE_URI,
        revision,
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


def writes_during(func):
    """The SQL statements other than reads (and savepoints) that `func` runs
    on the types' database."""
    reads = ("SELECT", "SAVEPOINT", "RELEASE SAVEPOINT")
    with CaptureQueriesContext(connections[type_db()]) as queries:
        func()
    sql = [query["sql"] for query in queries.captured_queries]
    return [s for s in sql if not s.lstrip().upper().startswith(reads)]


@pytest.mark.django_db
@pytest.mark.parametrize("debug", [True, False])
def test_registering_an_unchanged_type_again_writes_nothing(debug):
    with override_settings(DEBUG=debug):
        first = register()
        before = ResourceType.objects.get(pk=first.pk)
        assert writes_during(register) == []
    after = ResourceType.objects.get(pk=first.pk)
    assert (after.etag, after.modified_at, after.revision) == (
        before.etag,
        before.modified_at,
        before.revision,
    )


@pytest.mark.django_db
def test_under_debug_a_changed_type_is_rewritten():
    with override_settings(DEBUG=True):
        first = register()
        before = ResourceType.objects.get(pk=first.pk)
        assert writes_during(lambda: register(revision="2")) != []
    after = ResourceType.objects.get(pk=first.pk)
    assert after.revision == "2"
    assert after.etag != before.etag
    assert after.modified_at >= before.modified_at
    assert ResourceType.objects.filter(type_uri=TYPE_URI).count() == 1
