"""`registration.TypeSpec`, the framework-neutral description of a resource
type that every front end registers from: ``spec.register()`` stores the
type's row, its hooks and its service catalog entry (or leaves them pending
on a database error) without building any routes; the DRF registrations
(`json_resource_type`, `computed_resource_type`,
`built_in_resource_type_urls`) register their specs with their own route
builders."""

import dataclasses
import inspect
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
        spec._register_row(force=True)
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
def test_spec_registration_arguments_are_private_and_frozen():
    spec = registration.json_resource_type_spec(TYPE_URI, SLUG, revision="3")
    assert [f.name for f in dataclasses.fields(spec) if not f.name.startswith("_")] == [
        "kind",
        "resource_type_uri",
        "slug",
        "options",
        "document_media_type",
    ]
    assert spec._storage["revision"] == "3"
    with pytest.raises(TypeError):
        spec._storage["revision"] = "4"
    # Neither the IRI nor the slug is stored twice.
    assert "resource_type_uri" not in spec._storage
    assert "slug" not in spec._storage
    assert set(inspect.signature(registration.TypeSpec).parameters) == {
        "kind",
        "resource_type_uri",
        "slug",
        "options",
        "document_media_type",
    }


def deny(request, resource_type, obj):
    return False


OTHER_URI = "tests://SpecThingMoved"
OTHER_SLUG = "spec-things-moved"


@pytest.fixture
def forgotten_moved_things():
    yield
    with core_types._OPTIONS_LOCK:
        core_types._OPTIONS.pop(OTHER_URI, None)
        core_types._IRI_SLUGS.pop(OTHER_URI, None)
        core_types._SLUG_IRIS.pop(OTHER_SLUG, None)
    services.services.pop(OTHER_URI, None)
    registration._PENDING_REGISTRATIONS.pop(OTHER_SLUG, None)


@pytest.mark.django_db
@test_context
def test_replacing_the_iri_moves_the_row_with_the_hooks(forgotten_moved_things):
    """`dataclasses.replace` of the IRI stores the row under the new IRI,
    where the hooks are recorded: the deny-all object check is the stored
    type's (`types.md` I1)."""
    spec = registration.json_resource_type_spec(
        TYPE_URI, SLUG, object_checks=[deny], schema_func=lambda: SUMMARY_SCHEMA
    )
    moved = dataclasses.replace(spec, resource_type_uri=OTHER_URI)
    moved.register()
    stored = ResourceType.objects.get(slug=SLUG)
    assert stored.type_uri == OTHER_URI
    assert core_types.type_options(stored.type_uri).object_checks == (deny,)
    assert any(
        isinstance(leaf, core_types.HookLeaf)
        for leaf in core_types.requirement_for(stored).entries
    )
    # The other registration arguments come along.
    assert stored.schema is not None
    assert stored.schema.content == SUMMARY_SCHEMA


@pytest.mark.django_db
@test_context
def test_replacing_the_slug_stores_and_routes_it(forgotten_moved_things):
    spec = registration.json_resource_type_spec(TYPE_URI, SLUG)
    moved = dataclasses.replace(spec, slug=OTHER_SLUG)
    assert moved.register(urls_func=lambda slug: [slug]) == [OTHER_SLUG]
    assert ResourceType.objects.filter(slug=OTHER_SLUG).count() == 1
    assert not ResourceType.objects.filter(slug=SLUG).exists()
    assert core_types.type_options_for_slug(OTHER_SLUG) == spec.options


def test_spec_is_keyword_only():
    with pytest.raises(TypeError):
        registration.TypeSpec("json", TYPE_URI, SLUG, core_types.TypeOptions(), None)
    spec = registration.TypeSpec(
        kind="json",
        resource_type_uri=TYPE_URI,
        slug=SLUG,
        options=core_types.TypeOptions(),
        document_media_type=None,
    )
    assert spec.slug == SLUG


def test_a_kind_nobody_routes_is_refused():
    spec = registration.json_resource_type_spec(TYPE_URI, SLUG)
    with pytest.raises(ImproperlyConfigured):
        dataclasses.replace(spec, kind="model")


def test_kind_and_compute_must_agree():
    json_spec = registration.json_resource_type_spec(TYPE_URI, SLUG)
    with pytest.raises(ImproperlyConfigured):
        dataclasses.replace(json_spec, options=core_types.TypeOptions(compute=dict))
    computed = registration.computed_resource_type_spec(
        TYPE_URI, SLUG, None, None, compute=lambda request, key: {}
    )
    with pytest.raises(ImproperlyConfigured):
        dataclasses.replace(computed, options=core_types.TypeOptions())
    with pytest.raises(ImproperlyConfigured):
        registration.TypeSpec(
            kind="computed",
            resource_type_uri=TYPE_URI,
            slug=SLUG,
            options=core_types.TypeOptions(),
            document_media_type=None,
        )


def test_document_media_type_must_be_a_documents():
    assert registration.DOCUMENT_MEDIA_TYPES == frozenset(
        {"application/schema+json", "application/ld+json"}
    )
    for media_type in ("text/csv", "application/schema+json "):
        with pytest.raises(ImproperlyConfigured):
            registration.json_resource_type_spec(
                TYPE_URI, SLUG, document_media_type=media_type
            )
    for media_type in registration.DOCUMENT_MEDIA_TYPES:
        spec = registration.json_resource_type_spec(
            TYPE_URI, SLUG, document_media_type=media_type
        )
        assert spec.document_media_type == media_type


def test_ninja_document_renderers_are_keyed_by_the_document_media_types():
    from scrud_django.ninja import negotiation

    assert set(negotiation.DOCUMENT_RENDERERS) == registration.DOCUMENT_MEDIA_TYPES


def test_a_built_in_spec_takes_no_options():
    built_in = next(
        spec
        for spec in registration.built_in_type_specs()
        if spec.slug == "indexing-policies"
    )
    with pytest.raises(ImproperlyConfigured):
        dataclasses.replace(
            built_in, options=core_types.TypeOptions(object_checks=(deny,))
        )
    # Nor another IRI or slug: it registers through its own cache.
    with pytest.raises(ImproperlyConfigured):
        dataclasses.replace(built_in, resource_type_uri=OTHER_URI)
    with pytest.raises(ImproperlyConfigured):
        dataclasses.replace(built_in, slug=OTHER_SLUG)
    assert dataclasses.replace(built_in) == built_in


def test_options_that_are_not_type_options_are_refused():
    spec = registration.json_resource_type_spec(TYPE_URI, SLUG)
    for options in (None, {}, "options"):
        with pytest.raises(ImproperlyConfigured):
            dataclasses.replace(spec, options=options)


def test_specs_are_hashable():
    specs = [
        *registration.built_in_type_specs(),
        *ninja_type_specs(),
        registration.json_resource_type_spec(TYPE_URI, SLUG),
    ]
    assert len(set(specs)) == len(specs)
    assert hash(registration.json_resource_type_spec(TYPE_URI, SLUG)) == hash(
        registration.json_resource_type_spec(TYPE_URI, SLUG)
    )


def test_compute_must_be_callable():
    with pytest.raises(TypeError):
        registration.computed_resource_type_spec(
            TYPE_URI, SLUG, None, None, compute="nope"
        )


def test_json_spec_configuration_is_keyword_only():
    parameters = inspect.signature(
        registration.json_resource_type_spec
    ).parameters.values()
    assert "indexing_policy_resource" not in {p.name for p in parameters}
    assert [
        p.name for p in parameters if p.kind is not inspect.Parameter.KEYWORD_ONLY
    ] == ["resource_type_uri", "slug"]
    with pytest.raises(TypeError):
        registration.json_resource_type_spec(TYPE_URI, SLUG, "2")


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


def test_a_registration_without_routes_takes_no_viewset_class():
    """`build_urls=False` builds no routes, so a viewset class would be
    ignored: it is refused before anything is stored (`types.md` S5)."""
    with pytest.raises(TypeError):
        registration.ResourceTypeRegistration(
            TYPE_URI, slug=SLUG, build_urls=False, viewset_class=object
        )


def test_role_list_routes_are_read_only():
    with pytest.raises(TypeError):
        registration.ROLE_LIST_ROUTES["Role"] = ("x",)
