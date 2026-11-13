"""`registration.TypeSpec`, the framework-neutral description of a resource
type that every front end registers from: ``spec.register()`` stores the
type's row, its hooks and its service catalog entry (or leaves them pending
on a database error) without building any routes; the DRF registrations
(`json_resource_type`, `computed_resource_type`,
`built_in_resource_type_urls`) register their specs with their own route
builders."""

import logging
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError, connections
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django import registration, services
from scrud_django.core import types as core_types
from scrud_django.models import ResourceType

from .resource_types import (
    SUMMARY_CONTEXT,
    SUMMARY_SCHEMA,
    hooked_resource_types,
    json_resource_types,
    ninja_type_specs,
)
from .steps import *  # noqa: F403, F401

TYPE_URI = "tests://SpecThing"
SLUG = "spec-things"
# The built-in types `registration` caches at module level.
BUILT_IN_CACHES = [
    "JSON_SCHEMA_REGISTRATION_",
    "JSON_SCHEMA_RESOURCE_TYPE_",
    "JSON_LD_RESOURCE_TYPE_",
    "JSON_LD_REGISTRATION_",
    "INDEXING_POLICY_RESOURCE_TYPE_",
    "INDEXING_POLICY_REGISTRATION_",
    "WORKFLOW_POLICY_RESOURCE_TYPE_",
    "WORKFLOW_POLICY_REGISTRATION_",
]


@pytest.fixture(autouse=True)
def forgotten_spec_things():
    """The test type's hooks, service catalog entry and pending registration
    are forgotten after the test."""
    yield
    with core_types._OPTIONS_LOCK:
        core_types._OPTIONS.pop(TYPE_URI, None)
        core_types._IRI_SLUGS.pop(TYPE_URI, None)
        core_types._SLUG_IRIS.pop(SLUG, None)
    services.services.pop(TYPE_URI, None)
    registration._PENDING_REGISTRATIONS.pop(SLUG, None)
    registration._PROBED.clear()


def stored_rows():
    return list(ResourceType.objects.filter(type_uri=TYPE_URI).values())


def unreachable(execute, sql, params, many, context):
    """A connection's ``execute_wrapper``: the database is unreachable."""
    raise OperationalError("could not connect to server: Connection refused")


@pytest.mark.django_db
@test_context
def test_json_resource_type_returns_todays_paths():
    urls = registration.json_resource_type(TYPE_URI, SLUG)
    assert [route.name for route in urls] == [f"{SLUG}-list", f"{SLUG}-detail"]
    assert [str(route.pattern) for route in urls] == [
        f"{SLUG}/",
        f"{SLUG}/<str:slug>/",
    ]
    by_drf = stored_rows()
    assert len(by_drf) == 1
    assert registration.json_resource_type_spec(TYPE_URI, SLUG).register() == []
    assert stored_rows() == by_drf


@pytest.mark.django_db
@test_context
def test_spec_register_twice_is_idempotent(caplog):
    spec = registration.json_resource_type_spec(TYPE_URI, SLUG)
    with caplog.at_level(logging.WARNING):
        assert spec.register() == []
        assert spec.register(urls_func=lambda slug: [slug]) == [SLUG]
    assert len(stored_rows()) == 1
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert core_types.type_options_for_slug(SLUG) == spec.options
    assert TYPE_URI in services.services


@pytest.mark.django_db
@test_context
def test_spec_register_builds_no_drf_views(monkeypatch):
    # The DRF views cannot be imported: registering a spec, and the built-in
    # types its documents are stored as, needs none.
    import scrud_django

    monkeypatch.delattr(scrud_django, "views")
    monkeypatch.setitem(sys.modules, "scrud_django.views", None)
    for name in BUILT_IN_CACHES:
        monkeypatch.setattr(registration, name, getattr(registration, name))
    for spec in registration.built_in_type_specs():
        spec._registrar(force=True)
    spec = registration.json_resource_type_spec(
        TYPE_URI, SLUG, schema_func=lambda: SUMMARY_SCHEMA
    )
    assert spec.register() == []
    assert stored_rows()[0]["slug"] == SLUG


@pytest.mark.django_db
@test_context
def test_spec_register_defers_on_a_database_error(django_capture_on_commit_callbacks):
    spec = registration.json_resource_type_spec(TYPE_URI, SLUG)
    with connections[registration.type_db()].execute_wrapper(unreachable):
        assert spec.register(urls_func=lambda slug: [slug]) == [SLUG]
    assert SLUG in registration._PENDING_REGISTRATIONS
    assert TYPE_URI in services.services
    assert not stored_rows()
    with django_capture_on_commit_callbacks(execute=True):
        registered = registration.get_resource_type_for(SLUG)
    assert registered.type_uri == TYPE_URI
    assert SLUG not in registration._PENDING_REGISTRATIONS


@pytest.mark.django_db
@test_context
def test_computed_spec_requires_compute():
    with pytest.raises(ImproperlyConfigured):
        registration.computed_resource_type_spec(
            TYPE_URI,
            SLUG,
            lambda: SUMMARY_SCHEMA,
            lambda: SUMMARY_CONTEXT,
            compute=None,
        )


@pytest.mark.django_db
@test_context
def test_built_in_specs_name_document_media_types():
    media_types = {
        spec.slug: spec.document_media_type
        for spec in registration.built_in_type_specs()
    }
    assert media_types == {
        "json-schema": "application/schema+json",
        "json-ld": "application/ld+json",
        "indexing-policies": None,
        "workflow-policies": None,
    }


@pytest.mark.django_db
@test_context
def test_spec_registration_kwargs_are_frozen():
    spec = registration.json_resource_type_spec(TYPE_URI, SLUG, revision="3")
    assert spec.registration_kwargs["revision"] == "3"
    with pytest.raises(TypeError):
        spec.registration_kwargs["revision"] = "4"


@pytest.mark.django_db
@test_context
def test_ninja_type_specs_share_the_drf_registrations_hooks(caplog):
    reverse("hooked-notes-list")  # the URLconf registers the test types
    specs = ninja_type_specs()
    assert [spec.slug for spec in specs] == [
        "partner-profiles",
        "todos",
        "todo-lists",
        "checked-notes",
        "hooked-notes",
        "summaries",
        "encoded-summaries",
        "refusing-summaries",
        "workflow-notes",
    ]
    kinds = {spec.slug: spec.kind for spec in specs}
    assert kinds["summaries"] == kinds["encoded-summaries"] == "computed"
    # Both front ends registering the same types in one process replace
    # nothing: their hooks are the same.
    with caplog.at_level(logging.WARNING):
        for spec in specs:
            spec.register()
        json_resource_types()
        hooked_resource_types()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    for spec in specs:
        assert core_types.type_options_for_slug(spec.slug) == spec.options
