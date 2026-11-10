"""The studio's sample instance: a deterministic value built from a JSON Schema."""

import copy
from types import SimpleNamespace

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource, ResourceType
from scrud_django.registration import get_resource_type_for
from scrud_django.ui.render import render_resource
from scrud_django.ui.studio.preview import sample_of
from scrud_django.ui.studio.sample import sample_for
from scrud_django.validators import compile_json_schema

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_render import request_for


def test_sample_prefers_default_then_examples_then_enum():
    assert sample_for({"type": "string", "default": "d", "examples": ["e"]}) == "d"
    assert sample_for({"type": "string", "examples": ["e", "f"], "enum": ["x"]}) == "e"
    assert sample_for({"type": "integer", "enum": [7, 8]}) == 7
    assert sample_for({"default": None, "type": "string"}) is None


def test_sample_never_shares_the_schemas_values():
    """A ``default`` or ``examples`` value comes back as a copy: changing the
    sample, as `sample_of` does with its ``rbac_context``, leaves the stored
    schema alone."""
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "default": [{"x": 1}]},
            "meta": {"type": "object", "examples": [{"k": "v"}]},
        },
        "default": {"title": "t", "nested": {"a": [1]}},
    }
    before = copy.deepcopy(schema)
    sample = sample_for(schema)
    assert sample == schema["default"] and sample is not schema["default"]
    sample["nested"]["a"].append(2)
    assert schema == before
    parts = sample_for(schema["properties"]["tags"])
    parts[0]["x"] = 9
    meta = sample_for(schema["properties"]["meta"])
    meta["k"] = "changed"
    assert schema == before
    resource_type = SimpleNamespace(schema=SimpleNamespace(content=schema))
    assert sample_of(resource_type, "acme")["rbac_context"] == "acme"
    assert sample_of(resource_type)["rbac_context"] == ""
    assert schema == before


def test_sample_by_type_and_format():
    assert sample_for({"type": "string"}) == "Lorem ipsum"
    for fmt, value in {
        "date": "2026-01-01",
        "date-time": "2026-01-01T00:00:00Z",
        "uri": "https://example.com/",
        "email": "name@example.com",
    }.items():
        assert sample_for({"type": "string", "format": fmt}) == value
    assert sample_for({"type": "string", "format": "other"}) == "Lorem ipsum"
    assert sample_for({"type": "integer"}) == 0
    assert sample_for({"type": "number", "minimum": 2.5}) == 2.5
    assert sample_for({"type": "integer", "minimum": 3}) == 3
    assert sample_for({"type": "boolean"}) is False
    assert sample_for({"type": "frobnicate"}) is None
    assert sample_for({}) is None


def test_sample_has_one_array_item_and_every_property():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "done": {"type": "boolean"},
        },
    }
    assert sample_for(schema) == {
        "name": "Lorem ipsum",
        "tags": ["Lorem ipsum"],
        "done": False,
    }


def test_sample_resolves_refs_and_stops_at_cycles():
    schema = {
        "type": "object",
        "properties": {
            "title": {"$ref": "#/$defs/text"},
            "child": {"$ref": "#/$defs/node"},
            "bad": {"$ref": "http://example.com/x"},
            "missing": {"$ref": "#/$defs/nope"},
        },
        "$defs": {
            "text": {"type": "string"},
            "node": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "next": {"$ref": "#/$defs/node"},
                },
            },
        },
    }
    assert sample_for(schema) == {
        "title": "Lorem ipsum",
        "child": {"name": "Lorem ipsum", "next": None},
        "bad": None,
        "missing": None,
    }
    assert sample_for({"$ref": "#"}) is None
    loop = {
        "$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}},
        "$ref": "#/$defs/a",
    }
    assert sample_for(loop) is None
    # a root passed separately resolves refs in a subschema
    assert sample_for({"$ref": "#/$defs/text"}, schema) == "Lorem ipsum"


def test_sample_limits_depth():
    schema = {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}
    assert sample_for(schema, max_depth=5) == [["Lorem ipsum"]]
    assert sample_for(schema, max_depth=2) == [["Lorem ipsum"]]
    assert sample_for(schema, max_depth=1) == [[None]]
    assert sample_for(schema, max_depth=0) == [None]


def test_sample_handles_type_lists_oneof_and_bare_objects():
    assert sample_for({"type": ["null", "string"]}) == "Lorem ipsum"
    assert sample_for({"type": ["integer", "null"]}) == 0
    assert sample_for({"type": ["null"]}) is None
    assert sample_for({"type": "object"}) == {}
    assert sample_for({"type": "object", "properties": {}}) == {}
    assert sample_for({"oneOf": [{"type": "boolean"}, {"type": "string"}]}) is False
    assert sample_for({"anyOf": [{"foo": 1}, {"type": "string"}]}) == "Lorem ipsum"
    assert sample_for({"oneOf": [{"$ref": "#/nope"}, {"type": "integer"}]}) == 0
    assert sample_for({"oneOf": []}) is None


def test_sample_is_deterministic():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "array", "items": {"type": "number"}},
            "b": {"enum": [1]},
        },
    }
    assert sample_for(schema) == sample_for(schema)


@pytest.mark.django_db
@test_context
def test_todo_list_sample_validates():
    reverse("todo-lists-list")
    schema = get_resource_type_for("todo-lists").schema.content
    sample = sample_for(schema)
    assert isinstance(sample, dict) and sample
    compile_json_schema(schema)(sample)


def test_sample_infers_the_type_of_a_typeless_schema():
    assert sample_for({"properties": {"a": {"type": "string"}}}) == {"a": "Lorem ipsum"}
    assert sample_for({"items": {"type": "integer"}}) == [0]
    assert sample_for({"properties": "x"}) is None
    assert sample_for({}) is None
    merged = sample_for(
        {
            "allOf": [
                {"properties": {"a": {"type": "string"}}},
                {"type": "object", "properties": {"b": {"type": "boolean"}}},
            ]
        }
    )
    assert merged == {"a": "Lorem ipsum", "b": False}
    assert (
        sample_for({"allOf": [{"foo": 1}, {"type": "string"}, {"type": "integer"}]})
        == "Lorem ipsum"
    )
    assert sample_for({"allOf": []}) is None


def test_integer_minimum_is_rounded_up():
    assert sample_for({"type": "integer", "minimum": 2.5}) == 3
    assert sample_for({"type": "integer", "minimum": -2.5}) == -2
    assert sample_for({"type": "number", "minimum": 2.5}) == 2.5


@pytest.mark.django_db
def test_typeless_sample_renders_like_the_typed_one(admin_login):
    typed = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "n": {"type": "integer", "minimum": 2.5},
        },
    }
    typeless = {
        "properties": {
            "name": {"type": "string"},
            "tags": {"items": {"type": "string"}},
            "n": {"type": "integer", "minimum": 2.5},
        }
    }

    def html(schema):
        return str(
            render_resource(
                sample_for(schema),
                resource_type=ResourceType(
                    type_uri="tests://Node",
                    slug="nodes",
                    schema=Resource(content=schema),
                    context=Resource(content={}),
                ),
                href="/nodes/1/",
                etag="e",
                request=request_for(admin_login),
            )
        )

    assert sample_for(typeless) == {
        "name": "Lorem ipsum",
        "tags": ["Lorem ipsum"],
        "n": 3,
    }
    assert html(typeless) == html(typed)
