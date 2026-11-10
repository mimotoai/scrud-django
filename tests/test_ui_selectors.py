"""Stylesheet selectors, type terms and JSON-LD term helpers."""

import json
from importlib import resources

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.ui import ld
from scrud_django.ui.selectors import (
    Selector,
    SelectorError,
    TypeTerm,
    parse_selector,
    selector_matches,
)

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401


def test_parse_scoped_iri():
    assert parse_selector("sidebar ex:TodoItem", {"ex": "http://ex/"}) == Selector(
        ("sidebar",), TypeTerm(iri="http://ex/TodoItem")
    )


def test_parse_schema_format():
    assert parse_selector("string[format=date]", {}) == Selector(
        (), TypeTerm(schema_type="string", format="date")
    )


def test_parse_enum():
    assert parse_selector("string[enum]", {}).term == TypeTerm(
        schema_type="string", enum=True
    )


@pytest.mark.parametrize(
    "text",
    ["", "sidebar", "Sidebar! ex:X", "strng", "string[format=]", "string[enum"],
)
def test_parse_errors(text):
    with pytest.raises(SelectorError):
        parse_selector(text, {})


def test_descendant_scope_match():
    term = [TypeTerm(schema_type="string")]
    chain = ("page", "sidebar", "row")
    assert selector_matches(parse_selector("sidebar string", {}), chain, term) == 0
    assert (
        selector_matches(parse_selector("row sidebar string", {}), chain, term) is None
    )


def test_term_index():
    iri = TypeTerm(iri="http://ex/X")
    terms = [
        iri,
        TypeTerm(schema_type="string", format="date"),
        TypeTerm(schema_type="string"),
    ]
    prefixes = {"ex": "http://ex/"}
    assert selector_matches(parse_selector("string", prefixes), (), terms) == 1
    assert (
        selector_matches(parse_selector("string[format=date]", prefixes), (), terms)
        == 1
    )
    assert selector_matches(parse_selector("ex:X", prefixes), (), terms) == 0
    assert selector_matches(parse_selector("string[enum]", prefixes), (), terms) is None
    assert selector_matches(parse_selector("number", prefixes), (), terms) is None


def test_enum_matching():
    terms = [TypeTerm(schema_type="string", enum=True), TypeTerm(schema_type="string")]
    assert selector_matches(parse_selector("string[enum]", {}), (), terms) == 0
    assert selector_matches(parse_selector("string", {}), (), terms) == 0


def test_expand():
    ctx = {"ex": "http://ex/", "obj": {"@id": "x"}}
    assert ld.expand("ex:A", ctx) == "http://ex/A"
    assert ld.expand("http://other/A", ctx) == "http://other/A"
    assert ld.expand("nope:A", ctx) == "nope:A"
    assert ld.expand("obj:A", ctx) == "obj:A"


def _workflow_context():
    path = resources.files("scrud_django") / "static" / "WorkflowPolicy-ld.json"
    return json.loads(path.read_text())


def test_term_definition_class_and_nested():
    ctx = _workflow_context()
    prop, cls, nested = ld.term_definition(ctx, "initial_states")
    assert prop == "http://api.openteams.com/json-ld/InitialStates"
    assert cls == "http://api.openteams.com/json-ld/States"
    prop, cls, nested = ld.term_definition(ctx, "transitions")
    assert cls is None
    assert "from" in nested and "openteams" in nested
    assert ld.term_definition(ctx, "missing") == (None, None, ctx)


@pytest.mark.django_db
@test_context
def test_stylesheet_bad_selector_is_400(admin_login):
    Given.http.force_authenticate(admin_login)
    body = {"name": "bad", "rules": [{"select": "strng", "component": "x"}]}
    When.http.post(reverse("ui-stylesheets-list"), data=body, format="json")
    Then.http.status_code_is(400)
    assert Http.response.json()["type"].endswith("ui-selector-invalid")


@pytest.mark.django_db
@test_context
def test_stylesheet_selector_uses_own_prefixes(admin_login):
    Given.http.force_authenticate(admin_login)
    body = {
        "name": "ok",
        "prefixes": {"ex": "http://ex/"},
        "rules": [{"select": "sidebar ex:Item", "component": "x"}],
    }
    When.http.post(reverse("ui-stylesheets-list"), data=body, format="json")
    Then.http.status_code_is(201)
