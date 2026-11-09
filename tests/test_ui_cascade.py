"""The stylesheet cascade and component resolution."""

import pytest
from django.urls import reverse

from scrud_django.registration import ResourceRegistration
from scrud_django.ui.cascade import (
    builtin_component,
    effective_rules,
    load_components,
    resolve_component,
    rules_memo,
)
from scrud_django.ui.selectors import TypeTerm

PREFIXES = {"ex": "http://ex/"}
LIST = TypeTerm(iri="http://ex/L")
OBJECT = TypeTerm(schema_type="object")
TERMS = [LIST, OBJECT]


def make_component(name, mode="view", **overrides):
    reverse("ui-components-list")  # the URLconf registers the types
    return ResourceRegistration.register(
        {
            "name": name,
            "engine": "django",
            "mode": mode,
            "template": "<p>" + name + "</p>",
            "rbac_context": "",
            **overrides,
        },
        "ui-components",
    )


def make_stylesheet(name, rules, context="", **overrides):
    reverse("ui-stylesheets-list")
    return ResourceRegistration.register(
        {
            "name": name,
            "prefixes": PREFIXES,
            "rules": [{"select": s, "component": c} for s, c in rules],
            "rbac_context": context,
            **overrides,
        },
        "ui-stylesheets",
    )


def resolve(contexts=(), chain=(), terms=TERMS, mode="view", linked=()):
    with rules_memo():
        rules = effective_rules(list(contexts), tuple(linked))
        return resolve_component(rules, tuple(chain), terms, mode).name


@pytest.mark.django_db
def test_more_scopes_wins():
    make_component("a")
    make_component("b")
    make_stylesheet("s", [("ex:L", "a"), ("sidebar ex:L", "b")])
    assert resolve(chain=("sidebar",)) == "b"
    assert resolve(chain=()) == "a"


@pytest.mark.django_db
def test_type_iri_beats_schema_type():
    make_component("a")
    make_component("b")
    make_stylesheet("s", [("object", "a"), ("ex:L", "b")])
    assert resolve() == "b"


@pytest.mark.django_db
def test_type_iri_beats_later_schema_rule():
    make_component("a")
    make_component("b")
    make_stylesheet("s", [("ex:L", "a"), ("object", "b")])
    assert resolve() == "a"


@pytest.mark.django_db
def test_context_beats_default_and_list_order():
    for name in "abc":
        make_component(name)
    make_stylesheet("s", [("ex:L", "a")])
    make_stylesheet("s", [("ex:L", "b")], context="project-a")
    make_stylesheet("s", [("ex:L", "c")], context="project-b")
    assert resolve(["project-a", "project-b"]) == "c"
    assert resolve(["project-b", "project-a"]) == "b"
    assert resolve(["other"]) == "a"


@pytest.mark.django_db
def test_wrong_mode_falls_through():
    make_component("view-only", mode="view")
    make_stylesheet("s", [("ex:L", "view-only")])
    assert resolve(mode="view") == "view-only"
    assert resolve(mode="edit") == "scrud:object-edit"
    make_component("editor", mode="edit")
    make_stylesheet("t", [("object", "editor")], context="p")
    # the best rule (ex:L) has no edit component; the lower object rule does
    assert resolve(["p"], mode="edit") == "editor"


@pytest.mark.django_db
def test_builtin_named_and_missing_components():
    make_stylesheet("s", [("ex:L", "scrud:string"), ("object", "no-such")])
    assert resolve() == "scrud:string"
    make_stylesheet("t", [("ex:L", "no-such")], context="p")
    assert resolve(["p"]) == "scrud:string"


@pytest.mark.django_db
def test_rules_memo_sees_edits_in_a_new_scope():
    # A memo scope (one render, after any write) reads the stylesheets once:
    # an edit applies in the next scope, not within one.
    make_component("a")
    make_component("b")
    sheet = make_stylesheet("s", [("ex:L", "a")])
    with rules_memo():
        first = effective_rules([])
        assert effective_rules([]) is first
        ResourceRegistration.update(
            {**sheet.content, "rules": [{"select": "ex:L", "component": "b"}]},
            "ui-stylesheets",
            str(sheet.pk),
        )
        assert effective_rules([]) is first
    with rules_memo():
        second = effective_rules([])
        assert resolve_component(second, (), TERMS, "view").name == "b"
    assert resolve_component(first, (), TERMS, "view").name == "a"


@pytest.mark.django_db
def test_stylesheets_read_once_per_memo():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    make_stylesheet("s", [("ex:L", "scrud:json")], context="p1")
    make_stylesheet("t", [("ex:L", "scrud:json")], context="p2")
    with rules_memo():
        with CaptureQueriesContext(connection) as queries:
            effective_rules(["p1"])
            effective_rules(["p2"])
            effective_rules(["p1", "p2"], ("s",))
        assert len(queries) == 1
    # outside the memo each call reads them again
    with CaptureQueriesContext(connection) as queries:
        effective_rules(["p1"])
        effective_rules(["p1"])
    assert len(queries) == 2


@pytest.mark.django_db
def test_no_memo_outside_scope():
    make_stylesheet("s", [("ex:L", "scrud:json")])
    assert effective_rules([]) is not effective_rules([])


@pytest.mark.django_db
def test_linked_stylesheet_ranks_highest():
    make_component("a")
    make_component("b")
    make_stylesheet("extra", [("ex:L", "a")])
    make_stylesheet("pa", [("ex:L", "b")], context="project-a")
    assert resolve(["project-a"]) == "b"
    assert resolve(["project-a"], linked=("extra",)) == "a"


@pytest.mark.django_db
def test_linked_stylesheet_found_in_a_context():
    make_component("a")
    make_stylesheet("extra", [("ex:L", "a")], context="project-a")
    assert resolve([], linked=("extra",)) == "scrud:object"
    assert resolve(["project-a"], linked=("extra",)) == "a"


@pytest.mark.django_db
def test_order_breaks_ties_within_a_context():
    make_component("a")
    make_component("b")
    make_stylesheet("s1", [("ex:L", "a")], order=5)
    make_stylesheet("s2", [("ex:L", "b")], order=1)
    assert resolve() == "a"


@pytest.mark.django_db
def test_duplicate_stylesheet_names_lowest_pk_wins():
    make_component("a")
    make_component("b")
    make_stylesheet("dup", [("ex:L", "a")], context="p")
    make_stylesheet("dup", [("ex:L", "b")], context="p")
    assert resolve(["p"]) == "a"


@pytest.mark.django_db
def test_duplicate_component_names_lowest_pk_wins():
    first = make_component("dup", template="first")
    make_component("dup", template="second")
    component = load_components({"dup"})["dup"]
    assert component.template == "first"
    assert component.cache_key == (first.pk, first.etag)


@pytest.mark.django_db
def test_load_components():
    make_component("a", scopes={"*": "row"}, stylesheets=["x"])
    loaded = load_components({"a", "scrud:json", "missing", "scrud:nope"})
    assert set(loaded) == {"a", "scrud:json"}
    assert loaded["a"].scopes == {"*": "row"}
    assert loaded["a"].stylesheets == ("x",)
    assert loaded["scrud:json"].cache_key == ("builtin", "scrud:json")


def test_builtin_modes():
    assert builtin_component("scrud:object").mode == "view"
    assert builtin_component("scrud:string-edit").mode == "edit"
    for name in ("null", "uri", "json", "page", "collection", "notice"):
        assert builtin_component(f"scrud:{name}").mode == "any"
    assert builtin_component("scrud:json").engine == "django"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "terms, mode, expected",
    [
        ([TypeTerm(schema_type="null")], "view", "scrud:null"),
        ([TypeTerm(schema_type="null")], "edit", "scrud:null"),
        ([TypeTerm(schema_type="integer")], "view", "scrud:number"),
        ([TypeTerm(schema_type="integer")], "edit", "scrud:number-edit"),
        ([TypeTerm(schema_type="string", format="uri")], "edit", "scrud:uri"),
        ([TypeTerm(schema_type="string", format="date")], "view", "scrud:string"),
        ([TypeTerm(schema_type="string", format="date")], "edit", "scrud:date-edit"),
        ([TypeTerm(schema_type="string", enum=True)], "edit", "scrud:enum-edit"),
        ([TypeTerm(schema_type="string", enum=True)], "view", "scrud:string"),
        ([TypeTerm(iri="http://ex/Unknown")], "view", "scrud:json"),
        ([TypeTerm(iri="http://ex/Unknown")], "edit", "scrud:json"),
        ([], "view", "scrud:json"),
    ],
)
def test_default_stylesheet_and_fallbacks(terms, mode, expected):
    assert resolve(terms=terms, mode=mode) == expected


@pytest.mark.django_db
def test_fallback_without_matching_rules():
    # an empty chain of rules still falls back on the first schema term
    assert (
        resolve_component((), (), [LIST, TypeTerm(schema_type="integer")], "edit").name
        == "scrud:number-edit"
    )
    assert (
        resolve_component((), (), [LIST, TypeTerm(schema_type="null")], "edit").name
        == "scrud:null"
    )
    assert resolve_component((), (), [LIST], "view").name == "scrud:json"


@pytest.mark.django_db
def test_default_page_rule():
    assert resolve(terms=[TypeTerm(iri="scrud:page")]) == "scrud:page"
    assert resolve(terms=[TypeTerm(iri="scrud:page")], mode="edit") == "scrud:page"


@pytest.mark.django_db
def test_components_loaded_once_per_memo():
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    make_component("a")
    make_component("b")
    with rules_memo():
        with CaptureQueriesContext(connection) as queries:
            assert set(load_components({"a"})) == {"a"}
            assert set(load_components({"b", "missing"})) == {"b"}
            assert set(load_components({"a", "b"})) == {"a", "b"}
        assert len(queries) == 1
    # outside the memo each call reads them again
    with CaptureQueriesContext(connection) as queries:
        load_components({"a"})
        load_components({"a"})
    assert len(queries) == 2
