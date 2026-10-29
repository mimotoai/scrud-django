"""A resource's RBAC contexts are also stored as rows (``ResourceContext``),
kept in step with its content by ``Resource.save()``, so that lists can be
filtered by context in SQL."""

import importlib
import json
from copy import deepcopy

import pytest
from django.apps import apps as global_apps
from django.db import connection
from django.urls import reverse
from rest_framework import status
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource, ResourceContext
from scrud_django.registration import ResourceRegistration

from .fixtures import *  # noqa: F401, F403
from .fixtures import RESOURCE_ENDPOINT_DETAIL_NAME, RESOURCE_ENDPOINT_LIST_NAME
from .steps import *  # noqa: F403, F401

CONTEXT_ROWS = [
    ({}, {""}),
    ({"rbac_context": None}, {""}),
    ({"rbac_context": ""}, {""}),
    ({"rbac_context": "a"}, {"a"}),
    ({"rbac_context": ["a", "b", "a"]}, {"a", "b"}),
    ({"rbac_context": []}, set()),
    ({"rbac_context": {"@id": "openteams:RBACContext"}}, {""}),
    ({"rbac_context": ["a", 1]}, {""}),
    ({"rbac_context": "__x"}, {"__x"}),
]


def rows_of(resource):
    return set(
        ResourceContext.objects.filter(resource=resource).values_list(
            "rbac_context", flat=True
        )
    )


def register(content):
    reverse(RESOURCE_ENDPOINT_LIST_NAME)
    return ResourceRegistration.register(
        dict({"name": "n", "slug": "n"}, **content), "partner-profiles"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("content, expected", CONTEXT_ROWS)
def test_save_stores_one_row_per_context(content, expected):
    resource = register(content)
    assert rows_of(resource) == expected


@pytest.mark.django_db
def test_save_keeps_rows_in_step_with_the_content():
    resource = register({"rbac_context": ["a", "b"]})
    ResourceRegistration.update(
        {"name": "n", "rbac_context": ["b", "c"]}, "partner-profiles", resource.pk
    )
    assert rows_of(resource) == {"b", "c"}
    # A plain save (server code, the admin) keeps them in step too.
    resource = Resource.objects.get(pk=resource.pk)
    resource.content = {"name": "n"}
    resource.save()
    assert rows_of(resource) == {""}
    resource.delete()
    assert not ResourceContext.objects.filter(resource_id=resource.pk).exists()


@pytest.mark.django_db
def test_save_with_update_fields_without_content_leaves_rows():
    resource = register({"rbac_context": "a"})
    resource.etag = "x" * 32
    resource.save(update_fields=["etag"])
    assert rows_of(resource) == {"a"}


def migration_0008():
    return importlib.import_module("scrud_django.migrations.0008_resourcecontext")


class FakeSchemaEditor:
    connection = connection


@pytest.mark.django_db
def test_migration_backfills_rows_for_resources_stored_before_it():
    resources = [register(deepcopy(content)) for content, _ in CONTEXT_ROWS]
    # As stored before the table existed: no rows.
    ResourceContext.objects.all().delete()
    migration_0008().backfill_resource_contexts(global_apps, FakeSchemaEditor())
    for resource, (_, expected) in zip(resources, CONTEXT_ROWS, strict=True):
        assert rows_of(resource) == expected, resource.content
    # Every other resource (schemas, contexts, policies) has its rows too.
    without_rows = Resource.objects.exclude(
        pk__in=ResourceContext.objects.values("resource_id")
    ).exclude(content__rbac_context=[])
    assert not without_rows.exists()


@pytest.mark.django_db
def test_migration_contexts_agree_with_the_model():
    from scrud_django.mixins.resource_mixin import rbac_contexts_of

    contexts_of = migration_0008().contexts_of
    for content, _ in CONTEXT_ROWS:
        assert set(contexts_of(content)) == set(rbac_contexts_of(content))
    for content in (None, "text", 3, ["a"]):
        assert set(contexts_of(content)) == set(rbac_contexts_of(content))


@pytest.mark.django_db
@test_context
def test_empty_context_list_post_is_400(admin_login, partner_profile_post_data):
    before = Resource.objects.count()
    Given.http.force_authenticate(admin_login)
    When.http.post(
        reverse(RESOURCE_ENDPOINT_LIST_NAME),
        dict(partner_profile_post_data, rbac_context=[]),
        format="json",
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    And.http.content_type_is("application/problem+json")
    And.http.response_json_matches(
        {
            "type": "https://api.openteams.com/http-problem/rbac-context-invalid",
            "title": "Invalid rbac_context",
            "status": 400,
        }
    )
    assert "at least one context" in Http.response.json()["detail"]
    assert Resource.objects.count() == before


@pytest.mark.django_db
@test_context
def test_empty_context_list_put_is_400(admin_login, partner_profiles):
    url = reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id})
    stored = deepcopy(partner_profiles.content)
    Given.http.force_authenticate(admin_login)
    When.http.put(
        url,
        dict(stored, rbac_context=[]),
        format="json",
        HTTP_IF_MATCH="*",
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    And.http.response_json_matches(
        {"type": "https://api.openteams.com/http-problem/rbac-context-invalid"}
    )
    partner_profiles.refresh_from_db()
    assert partner_profiles.content == stored
    assert rows_of(partner_profiles) == {""}


@pytest.mark.django_db
@test_context
def test_overlong_context_is_400(admin_login, partner_profile_post_data):
    before = Resource.objects.count()
    Given.http.force_authenticate(admin_login)
    When.http.post(
        reverse(RESOURCE_ENDPOINT_LIST_NAME),
        dict(partner_profile_post_data, rbac_context="x" * 2049),
        format="json",
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    And.http.response_json_matches(
        {"type": "https://api.openteams.com/http-problem/rbac-context-invalid"}
    )
    assert Resource.objects.count() == before


@pytest.mark.django_db
def test_overlong_context_is_refused_server_side():
    before = Resource.objects.count()
    with pytest.raises(ValueError, match="2048"):
        register({"rbac_context": "x" * 2049})
    assert Resource.objects.count() == before


@pytest.mark.django_db
@test_context
def test_server_side_empty_context_list_is_stored_superuser_only(
    admin_login, regular_login
):
    resource = register({"rbac_context": []})
    assert rows_of(resource) == set()
    url = reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": resource.id})
    Given.http.force_authenticate(regular_login)
    When.http.get(url)
    Then.http.status_code_is(status.HTTP_404_NOT_FOUND)
    Given.http.force_authenticate(admin_login)
    When.http.get(url)
    Then.http.status_code_is(status.HTTP_200_OK)


def test_resource_content_reads_as_cel_fields():
    resource = Resource(content={"name": "x"})
    assert resource.to_cel() == {"name": "x"}
    assert Resource(content=json.loads('"text"')).to_cel() == {}


@pytest.mark.django_db
def test_raw_saves_keep_rows_in_step():
    # reversion's revert and loaddata save with Model.save_base(raw=True),
    # past Resource.save_base: the post_save receiver keeps the rows.
    from django.core import serializers
    from reversion.models import Version

    resource = register({"rbac_context": "a"})
    ResourceRegistration.update(
        {"name": "n", "rbac_context": "b"}, "partner-profiles", resource.pk
    )
    assert rows_of(resource) == {"b"}
    oldest = Version.objects.get_for_object(resource).order_by("pk").first()
    assert oldest.field_dict["content"]["rbac_context"] == "a"
    oldest.revert()
    assert rows_of(resource) == {"a"}

    data = json.loads(
        serializers.serialize("json", [Resource.objects.get(pk=resource.pk)])
    )
    data[0]["fields"]["content"]["rbac_context"] = ["c", "d"]
    (loaded,) = serializers.deserialize("json", json.dumps(data))
    assert loaded.object.content["rbac_context"] == ["c", "d"]
    loaded.save()
    assert rows_of(resource) == {"c", "d"}
