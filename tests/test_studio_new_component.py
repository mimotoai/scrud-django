"""New component for a node (spec Goal 5): the options the studio offers
for a node (its matches, most specific first, "inside this type", a scope of
its chain, a data context) and the action, which writes a Jinja component
and the rule that selects it (and, inside this type, the root component's
scope: ruling S2) on a working branch in one transaction, through the
resource views, and reports whether the rule wins. These tests serve the API
at ``/api/`` and the studio at ``/studio/`` (``tests.mounted_urls``)."""

import json
import re
from unittest import mock

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django import collection_type_uri_for
from scrud_django.models import Resource
from scrud_django.registration import ResourceTypeRegistration
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI
from scrud_django.ui.cascade import builtin_component
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import assign_role
from .steps import *  # noqa: F403, F401
from .test_ui_branch_push import create, make_wip, own_rows, stored_state
from .test_ui_branch_types import component, make_branch, problem
from .test_ui_http import grant, html, make_component, make_stylesheet
from .test_ui_render import make_component as scoped_component

SLUG = "todo-lists"
EX = "http://example.org/todo#"
CONTEXT_PREFIXES = {"ex": EX, "openteams": "http://api.openteams.com/json-ld/"}
READ_ONLY = "https://api.openteams.com/http-problem/ui-studio-branch-read-only"
UNAVAILABLE = "https://api.openteams.com/http-problem/ui-branch-unavailable"
TEMPLATE_INVALID = "https://api.openteams.com/http-problem/ui-template-invalid"
SAVED = "scrud-studio-saved"
EM = "<em>{{ value.text }}</em>"
MARK = "<mark>{{ value }}</mark>"
STRING = {"select": "string", "label": "every string on this branch", "broad": True}
# A to-do's blocker explanation: a string with no IRI term.
EXPLANATION = "/blockers/0/explanation"

# The preview's nodes that hold no other node: {pointer: markup}.
LEAF = re.compile(
    r'<scrud-node data-pointer="([^"]*)">((?:(?!<scrud-node).)*?)</scrud-node>', re.S
)


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def options(status=200, slug=SLUG, **params):
    reverse("todo-lists-list")  # the URLconf registers the types
    When.http.get(
        reverse("scrud-studio:new-component-options", args=[slug]),
        {"branch": "wip", **params},
    )
    Then.http.status_code_is(status)
    return Http.response.json()


def new_component(status=201, **body):
    """POST the action: by default, ``EM`` for a to-do list's first item on
    ``wip`` in view mode, matching its most specific term."""
    reverse("todo-lists-list")
    When.http.post(
        reverse("scrud-studio:new-component"),
        data={
            "branch": "wip",
            "slug": SLUG,
            "pointer": "/items/0",
            "mode": "view",
            "applies_to": "",
            "template": EM,
            **body,
        },
        format="json",
    )
    Then.http.status_code_is(status)
    return Http.response.json()


def preview(slug=SLUG, **params):
    reverse("todo-lists-list")
    When.http.get(reverse("scrud-studio:preview", args=[slug]), params)
    Then.http.status_code_is(200)
    return html()


def explain(slug=SLUG, **params):
    When.http.get(reverse("scrud-studio:explain", args=[slug]), params)
    Then.http.status_code_is(200)
    return Http.response.json()


def jinja_copy(builtin):
    """The template of a root copied from `builtin`: Jinja drops a last
    newline, so the copy has one more to render the same bytes."""
    template = builtin_component(builtin).template
    assert template.endswith("\n")
    return template + "\n"


def leaves(page):
    return dict(LEAF.findall(page))


def outside(page, prefix="/blockers"):
    """`page` with the markup of each leaf node under `prefix` blanked."""
    return LEAF.sub(
        lambda m: (
            f'<scrud-node data-pointer="{m[1]}">…</scrud-node>'
            if m[1].startswith(prefix)
            else m[0]
        ),
        page,
    )


def odd_type():
    """Type ``studio-odd``: its root's class is ``urn:x:Item`` (its
    context's ``@type``), and its properties ``a.b`` and ``a-b`` have no
    IRI."""
    ResourceTypeRegistration(
        "tests://StudioOdd",
        slug="studio-odd",
        schema_func=lambda: json.dumps(
            {
                "type": "object",
                "properties": {"a.b": {"type": "string"}, "a-b": {"type": "string"}},
            }
        ),
        context_func=lambda: json.dumps({"@type": "urn:x:Item"}),
        register_service=False,
    )


def ours(explained, name):
    """The one rule of `explained` that selects component `name`."""
    (found,) = [r for r in explained["rules"] if r.get("component") == name]
    return found


def alice_on_wip(admin_login, ui_alice_login):
    """``wip`` (in "alice", over main), and alice signed in."""
    make_wip(admin_login)
    Given.http.force_authenticate(ui_alice_login)
    return ui_alice_login


def may_preview_context(user, context):
    """Let `user` preview `context`'s presentation (ruling S7)."""
    grant(user, {"http.GET": [UI_STYLESHEET_TYPE_URI]}, context)


@pytest.mark.django_db
@test_context
def test_options_most_specific_first_and_broad_labelled(admin_login, ui_alice_login):
    user = alice_on_wip(admin_login, ui_alice_login)
    # A string with no IRI term: its only match is broad, and labelled with
    # what it affects; "inside this type" scopes it at the type's root.
    assert options(slug="todos", pointer=EXPLANATION) == {
        "matches": [STRING],
        "inside_type": {
            "scope": "todos-blockers",
            "root_component": "scrud:object",
            "property": "blockers",
            "creates_root": True,
        },
        "where": [],
        "applies_to": [""],
        "name": "todos-blockers-explanation-view",
    }
    # Most specific first: the property IRI (in the context's prefixes),
    # then the bare type. A node with an IRI term is not offered "inside
    # this type". Where: a scope of its chain; applies to: "" or the data
    # context.
    may_preview_context(user, "acme")
    got = options(pointer="/items/0/text", scope="page compact", context="acme")
    assert got["matches"] == [
        {"select": EX + "text", "label": "ex:text", "broad": False},
        STRING,
    ]
    assert got["inside_type"] is None
    assert got["where"] == ["page", "compact"]
    assert got["applies_to"] == ["", "acme"]
    assert got["name"] == "todo-lists-items-text-view"
    # A class IRI first; the array index is not part of the name.
    got = options(pointer="/items/0", mode="edit")
    assert got["matches"] == [
        {"select": EX + "TodoItem", "label": "ex:TodoItem", "broad": False},
        {"select": "object", "label": "every object on this branch", "broad": True},
    ]
    assert got["name"] == "todo-lists-items-edit"
    # Format and enum terms, labelled.
    assert options(slug="todos", pointer="/blockers/0/blocked_by")["matches"] == [
        {
            "select": "string[format=uri]",
            "label": "every string format=uri on this branch",
            "broad": True,
        },
        STRING,
    ]
    assert options(slug="todos", pointer="/status")["matches"] == [
        {
            "select": "http://schema.org/status",
            "label": "schema:status",
            "broad": False,
        },
        {
            "select": "string[enum]",
            "label": "every string enum on this branch",
            "broad": True,
        },
        STRING,
    ]
    # The root: its type URI (no context prefix shortens it), never "inside
    # this type".
    got = options(slug="todos", pointer="")
    assert got["matches"][0] == {
        "select": "tests://ToDo",
        "label": "tests://ToDo",
        "broad": False,
    }
    assert got["inside_type"] is None
    assert got["name"] == "todos-view"
    # Refusals, as the preview's.
    options(400, pointer="/nope")
    options(400, branch="no-such-branch")
    assert problem()["type"] == UNAVAILABLE
    Given.http.force_authenticate(None)
    options(401)


@pytest.mark.django_db
@test_context
def test_new_component_on_class_term_wins_and_renders(admin_login, ui_alice_login):
    alice_on_wip(admin_login, ui_alice_login)
    assert "<em>" not in preview(branch="wip")
    before_main = own_rows(UI_STYLESHEET_TYPE_URI, "main")
    got = new_component()
    assert Http.response["HX-Trigger"] == SAVED
    name = "todo-lists-items-view"
    assert got["written"] == [
        {"kind": "component", "name": name},
        {"kind": "stylesheet", "name": "studio", "applies_to": ""},
    ]
    assert got["wins"] is True
    explained = got["explanation"]
    assert explained["rules"][explained["winner"]] == {
        **ours(explained, name),
        "selector": EX + "TodoItem",
        "stylesheet": "studio",
        "branch": "wip",
        "applies_to": "",
        "outcome": "wins",
    }
    # It agrees with the inspector.
    assert explain(pointer="/items/0", branch="wip") == explained
    # Both on the author's branch, through the API (server-set fields).
    (stored,) = own_rows(UI_COMPONENT_TYPE_URI, "wip")
    assert stored == {
        "name": name,
        "engine": "jinja2",
        "mode": "view",
        "template": EM,
        "branch": "wip",
        "rbac_context": "alice",
        "base": None,
    }
    (sheet,) = own_rows(UI_STYLESHEET_TYPE_URI, "wip")
    assert sheet == {
        "name": "studio",
        "applies_to": "",
        "prefixes": CONTEXT_PREFIXES,
        "rules": [{"select": "ex:TodoItem", "component": name}],
        "branch": "wip",
        "rbac_context": "alice",
        "base": None,
    }
    assert own_rows(UI_STYLESHEET_TYPE_URI, "main") == before_main
    assert own_rows(UI_COMPONENT_TYPE_URI, "main") == []
    # The preview uses them, on the branch only.
    assert "<em>Lorem ipsum</em>" in preview(branch="wip")
    assert "<em>" not in preview()
    # A second one is added to the branch's studio stylesheet, after the
    # first rule, its prefixes kept.
    got = new_component(pointer="/title", match="string", template="<q>{{ value }}</q>")
    assert got["wins"] is True
    assert got["written"][0] == {"kind": "component", "name": "todo-lists-title-view"}
    (sheet,) = own_rows(UI_STYLESHEET_TYPE_URI, "wip")
    assert sheet["prefixes"] == CONTEXT_PREFIXES
    assert sheet["rules"] == [
        {"select": "ex:TodoItem", "component": name},
        {"select": "string", "component": "todo-lists-title-view"},
    ]
    assert "<q>Lorem ipsum</q>" in preview(branch="wip")


@pytest.mark.django_db
@test_context
def test_inside_type_adds_parent_scope_and_wins(admin_login, ui_alice_login):
    alice_on_wip(admin_login, ui_alice_login)
    before = preview(slug="todos", branch="wip")
    got = new_component(
        slug="todos", pointer=EXPLANATION, inside_type=True, template=MARK
    )
    name = "todos-blockers-explanation-view"
    assert got["written"] == [
        {"kind": "component", "name": "todos-view"},
        {"kind": "component", "name": name},
        {"kind": "stylesheet", "name": "studio", "applies_to": ""},
    ]
    assert got["wins"] is True
    explained = got["explanation"]
    assert explained["scope"] == ["todos-blockers"]
    assert ours(explained, name)["selector"] == "todos-blockers string"
    assert explain(slug="todos", pointer=EXPLANATION, branch="wip") == explained
    # The root component: scrud:object's markup in Jinja, with the scope,
    # selected by the type URI, in the same stylesheet.
    root, node = own_rows(UI_COMPONENT_TYPE_URI, "wip")
    assert root == {
        "name": "todos-view",
        "engine": "jinja2",
        "mode": "view",
        "template": jinja_copy("scrud:object"),
        "scopes": {"blockers": "todos-blockers"},
        "branch": "wip",
        "rbac_context": "alice",
        "base": None,
    }
    assert node["name"] == name and node["template"] == MARK
    (sheet,) = own_rows(UI_STYLESHEET_TYPE_URI, "wip")
    assert sheet["prefixes"] == {
        "schema": "http://schema.org/",
        "openteams": "http://api.openteams.com/json-ld/",
    }
    assert sheet["rules"] == [
        {"select": "tests://ToDo", "component": "todos-view"},
        {"select": "todos-blockers string", "component": name},
    ]
    # The node renders it, and the page is byte-identical outside the
    # property's leaves: the root's markup and every other node.
    after = preview(slug="todos", branch="wip")
    assert leaves(after)[EXPLANATION] == "<mark>Lorem ipsum</mark>"
    assert "/title" in leaves(after)
    assert outside(after) == outside(before)
    # Per mode: the view-mode root does not scope edit mode.
    before_edit = preview(slug="todos", branch="wip", mode="edit")
    assert "<mark>" not in before_edit
    assert options(slug="todos", pointer=EXPLANATION)["inside_type"] == {
        "scope": "todos-blockers",
        "root_component": "todos-view",
        "property": "blockers",
        "creates_root": False,
    }
    assert options(slug="todos", pointer=EXPLANATION, mode="edit")["inside_type"] == {
        "scope": "todos-blockers",
        "root_component": "scrud:object-edit",
        "property": "blockers",
        "creates_root": True,
    }
    # The root has the scope now: another node of the property needs no
    # root write.
    got = new_component(
        slug="todos",
        pointer="/blockers/0/blocked_by",
        inside_type=True,
        match="string[format=uri]",
        template="<s>{{ value }}</s>",
    )
    assert got["written"] == [
        {"kind": "component", "name": "todos-blockers-blocked_by-view"},
        {"kind": "stylesheet", "name": "studio", "applies_to": ""},
    ]
    assert got["wins"] is True
    assert own_rows(UI_STYLESHEET_TYPE_URI, "wip")[0]["rules"][-1] == {
        "select": "todos-blockers string[format=uri]",
        "component": "todos-blockers-blocked_by-view",
    }
    # Edit mode: a root copied from scrud:object-edit, byte-identical
    # outside the property's leaves.
    got = new_component(
        slug="todos", pointer=EXPLANATION, mode="edit", inside_type=True, template=MARK
    )
    assert got["written"][0] == {"kind": "component", "name": "todos-edit"}
    assert got["wins"] is True
    (root,) = [
        c for c in own_rows(UI_COMPONENT_TYPE_URI, "wip") if c["name"] == "todos-edit"
    ]
    assert root["mode"] == "edit"
    assert root["template"] == jinja_copy("scrud:object-edit")
    after_edit = preview(slug="todos", branch="wip", mode="edit")
    assert leaves(after_edit)[EXPLANATION] == "<mark>Lorem ipsum</mark>"
    assert outside(after_edit) == outside(before_edit)


@pytest.mark.django_db
@test_context
def test_view_specific_where_scope(admin_login, ui_alice_login):
    alice_on_wip(admin_login, ui_alice_login)
    # A scope of the node's chain only.
    before = stored_state()
    new_component(400, where="sidebar", scope="page compact")
    assert stored_state() == before
    got = new_component(where="compact", scope="page compact")
    assert got["wins"] is True
    assert own_rows(UI_STYLESHEET_TYPE_URI, "wip")[0]["rules"] == [
        {"select": "compact ex:TodoItem", "component": "todo-lists-items-view"}
    ]
    assert "<em>Lorem ipsum</em>" in preview(branch="wip", scope="page compact")
    assert "<em>" not in preview(branch="wip", scope="page")
    # With "inside this type": the scopes in the order of the node's chain.
    got = new_component(
        slug="todos",
        pointer=EXPLANATION,
        inside_type=True,
        where="page",
        scope="page",
        template=MARK,
    )
    assert got["wins"] is True
    assert own_rows(UI_STYLESHEET_TYPE_URI, "wip")[0]["rules"][-1] == {
        "select": "page todos-blockers string",
        "component": "todos-blockers-explanation-view",
    }
    assert "<mark>Lorem ipsum</mark>" in preview(
        slug="todos", branch="wip", scope="page"
    )
    assert "<mark>" not in preview(slug="todos", branch="wip")


@pytest.mark.django_db
@test_context
def test_domain_specific_applies_to(admin_login, ui_alice_login):
    user = alice_on_wip(admin_login, ui_alice_login)
    may_preview_context(user, "acme")
    # Only "" or the preview's data context.
    before = stored_state()
    new_component(400, applies_to="acme")
    new_component(400, applies_to="other", context="acme")
    assert stored_state() == before
    got = new_component(applies_to="acme", context="acme")
    assert got["wins"] is True
    assert got["written"][-1] == {
        "kind": "stylesheet",
        "name": "studio",
        "applies_to": "acme",
    }
    explained = got["explanation"]
    assert explained["rules"][explained["winner"]]["applies_to"] == "acme"
    (sheet,) = own_rows(UI_STYLESHEET_TYPE_URI, "wip")
    assert sheet["applies_to"] == "acme" and sheet["rbac_context"] == "alice"
    # It applies only with that data context.
    assert "<em>Lorem ipsum</em>" in preview(branch="wip", context="acme")
    assert "<em>" not in preview(branch="wip")
    # The stylesheet for "" is another one.
    new_component(pointer="/title", match="string", template="<q>{{ value }}</q>")
    sheets = own_rows(UI_STYLESHEET_TYPE_URI, "wip")
    assert [(s["name"], s["applies_to"]) for s in sheets] == [
        ("studio", "acme"),
        ("studio", ""),
    ]


@pytest.mark.django_db
@test_context
def test_reports_not_winning_and_why(admin_login, ui_alice_login):
    make_component("row", "<b>{{ value.text }}</b>")
    sheet = make_stylesheet("s", [("page compact ex:TodoItem", "row")])
    alice_on_wip(admin_login, ui_alice_login)
    got = new_component(scope="page compact")
    assert got["wins"] is False
    explained = got["explanation"]
    assert ours(explained, "todo-lists-items-view")["outcome"] == "fewer scopes"
    # The two-scope rule outranks it; alice may not read its stylesheet.
    winner = explained["rules"][explained["winner"]]
    assert winner == {"readable": False, "rank": winner["rank"], "outcome": "wins"}
    assert winner["rank"][0] == 2
    assert explain(pointer="/items/0", branch="wip", scope="page compact") == explained
    page = preview(branch="wip", scope="page compact")
    assert "<b>Lorem ipsum</b>" in page and "<em>" not in page
    # The studio edits no other rule to force a win.
    assert Resource.objects.get(pk=sheet.pk).etag == sheet.etag


@pytest.mark.django_db
@test_context
def test_new_component_is_atomic(admin_login, ui_alice_login):
    # main's root component of to-dos, inherited by wip.
    main_root = make_component(
        "todo-root",
        "<div>{% for name, child in child_items %}{{ child }}{% endfor %}</div>",
    )
    make_stylesheet("s", [("tests://ToDo", "todo-root")])
    user = alice_on_wip(admin_login, ui_alice_login)
    grant(user, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "")
    assert options(slug="todos", pointer=EXPLANATION)["inside_type"] == {
        "scope": "todos-blockers",
        "root_component": "todo-root",
        "property": "blockers",
        "creates_root": False,
    }
    # The node's template does not compile: the view's 400, and the root's
    # override (written first) is rolled back with everything else.
    before = stored_state()
    new_component(
        400, slug="todos", pointer=EXPLANATION, inside_type=True, template="{% for %}"
    )
    assert problem()["type"] == TEMPLATE_INVALID
    assert "HX-Trigger" not in Http.response
    assert stored_state() == before
    # Compiling: the root is overridden on the branch, with the scope, and
    # based on main's.
    got = new_component(
        slug="todos", pointer=EXPLANATION, inside_type=True, template=MARK
    )
    assert got["written"][0] == {"kind": "component", "name": "todo-root"}
    assert got["wins"] is True
    override = own_rows(UI_COMPONENT_TYPE_URI, "wip")[0]
    assert override == {
        **main_root.content,
        "scopes": {"blockers": "todos-blockers"},
        "branch": "wip",
        "rbac_context": "alice",
        "base": main_root.etag,
    }
    assert Resource.objects.get(pk=main_root.pk).etag == main_root.etag
    # Only the node's rule: the root is selected by main's stylesheet.
    assert own_rows(UI_STYLESHEET_TYPE_URI, "wip")[0]["rules"] == [
        {
            "select": "todos-blockers string",
            "component": "todos-blockers-explanation-view",
        }
    ]
    assert "<mark>Lorem ipsum</mark>" in preview(slug="todos", branch="wip")


@pytest.mark.django_db
@test_context
def test_inside_type_keeps_a_scope_the_root_gives(admin_login, ui_alice_login):
    # The root's component already scopes the property: that scope is used
    # as it is, so that the rules naming it keep matching, and the root is
    # not written.
    scoped_component(
        "todo-root",
        "<div>{% for name, child in child_items %}{{ child }}{% endfor %}</div>",
        scopes={"blockers": "notes"},
    )
    make_stylesheet("s", [("tests://ToDo", "todo-root")])
    user = alice_on_wip(admin_login, ui_alice_login)
    grant(user, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "")
    assert options(slug="todos", pointer=EXPLANATION)["inside_type"] == {
        "scope": "notes",
        "root_component": "todo-root",
        "property": "blockers",
        "creates_root": False,
    }
    got = new_component(
        slug="todos", pointer=EXPLANATION, inside_type=True, template=MARK
    )
    assert got["wins"] is True
    assert [w["kind"] for w in got["written"]] == ["component", "stylesheet"]
    assert [c["name"] for c in own_rows(UI_COMPONENT_TYPE_URI, "wip")] == [
        "todos-blockers-explanation-view"
    ]
    assert own_rows(UI_STYLESHEET_TYPE_URI, "wip")[0]["rules"] == [
        {"select": "notes string", "component": "todos-blockers-explanation-view"}
    ]


@pytest.mark.django_db
@test_context
def test_new_root_is_selected_by_the_most_specific_class_term(
    admin_login, ui_alice_login
):
    # A partner profile's root has its context's @type (schema:Organization)
    # before its type URI; a rule on that class gives it a built-in.
    make_stylesheet("s", [("http://schema.org/Organization", "scrud:object")])
    alice_on_wip(admin_login, ui_alice_login)
    slug, pointer = "partner-profiles", "/rbac_context"
    assert options(slug=slug, pointer=pointer)["inside_type"] == {
        "scope": "partner-profiles-rbac_context",
        "root_component": "scrud:object",
        "property": "rbac_context",
        "creates_root": True,
    }
    got = new_component(
        slug=slug, pointer=pointer, inside_type=True, template="<mark>ctx</mark>"
    )
    assert got["written"][0] == {"kind": "component", "name": "partner-profiles-view"}
    assert got["wins"] is True
    # The created root is selected by the class, so it outranks the rule on
    # it (a later stylesheet), and its scope applies (ruling S12).
    assert own_rows(UI_STYLESHEET_TYPE_URI, "wip")[0]["rules"] == [
        {"select": "schema:Organization", "component": "partner-profiles-view"},
        {
            "select": "partner-profiles-rbac_context string",
            "component": "partner-profiles-rbac_context-view",
        },
    ]
    root = explain(slug=slug, pointer="", branch="wip")
    assert root["rules"][root["winner"]]["component"] == "partner-profiles-view"
    assert leaves(preview(slug=slug, branch="wip"))[pointer] == "<mark>ctx</mark>"


@pytest.mark.django_db
@test_context
def test_generated_scope_names_do_not_collide(admin_login, ui_alice_login):
    odd_type()
    alice_on_wip(admin_login, ui_alice_login)
    got = new_component(
        slug="studio-odd", pointer="/a.b", inside_type=True, template="<i>one</i>"
    )
    assert got["wins"] is True
    # "a-b" would get the scope "a.b" has: it gets a suffix (ruling S14).
    assert options(slug="studio-odd", pointer="/a-b")["inside_type"] == {
        "scope": "studio-odd-a-b-2",
        "root_component": "studio-odd-view",
        "property": "a-b",
        "creates_root": False,
    }
    got = new_component(
        slug="studio-odd", pointer="/a-b", inside_type=True, template="<i>two</i>"
    )
    assert got["wins"] is True
    (root,) = [
        c
        for c in own_rows(UI_COMPONENT_TYPE_URI, "wip")
        if c["name"] == "studio-odd-view"
    ]
    assert root["scopes"] == {"a.b": "studio-odd-a-b", "a-b": "studio-odd-a-b-2"}
    page = leaves(preview(slug="studio-odd", branch="wip"))
    assert (page["/a.b"], page["/a-b"]) == ("<i>one</i>", "<i>two</i>")


@pytest.mark.django_db
@test_context
def test_an_iri_selector_must_round_trip(admin_login, ui_alice_login):
    odd_type()
    make_wip(admin_login)
    # wip's studio stylesheet has a prefix named like the root class's
    # scheme: "urn:x:Item" would expand to another IRI.
    create(
        "ui-stylesheets",
        {
            "name": "studio",
            "applies_to": "",
            "prefixes": {"urn": "http://elsewhere.example/"},
            "rules": [],
            "branch": "wip",
            "rbac_context": "alice",
        },
    )
    Given.http.force_authenticate(ui_alice_login)
    assert options(slug="studio-odd", pointer="")["matches"][0]["select"] == (
        "urn:x:Item"
    )
    before = stored_state()
    new_component(400, slug="studio-odd", pointer="", template="<i/>")
    assert "match" in problem()["errors"]
    # The component was written first: rolled back.
    assert stored_state() == before


@pytest.mark.django_db
@test_context
def test_refused_last_step_rolls_everything_back(admin_login, ui_alice_login):
    alice_on_wip(admin_login, ui_alice_login)
    # A built-in root: the new root is written first, then the node's
    # component, whose template does not compile; nothing is stored.
    before = stored_state()
    new_component(
        400, slug="todos", pointer=EXPLANATION, inside_type=True, template="{% for %}"
    )
    assert problem()["type"] == TEMPLATE_INVALID
    assert stored_state() == before
    # May select wip and write components there, not stylesheets: the
    # stylesheet's write (the last step) is refused as the API refuses it,
    # after the components were created; nothing is stored.
    user = assign_role(
        {
            f"http.{m}": [
                UI_COMPONENT_TYPE_URI,
                collection_type_uri_for(UI_COMPONENT_TYPE_URI),
            ]
            for m in ("GET", "POST", "PUT")
        },
        "alice",
    )
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    Given.http.force_authenticate(user)
    When.http.post(
        reverse("ui-stylesheets-list"),
        data={
            "name": "studio",
            "applies_to": "",
            "rules": [],
            "branch": "wip",
            "rbac_context": "alice",
        },
        format="json",
    )
    Then.http.status_code_is(403)
    api = Http.response.json()
    assert new_component(403) == api
    assert stored_state() == before
    assert (
        new_component(
            403, slug="todos", pointer=EXPLANATION, inside_type=True, template=MARK
        )
        == api
    )
    assert stored_state() == before


@pytest.mark.django_db
@test_context
def test_new_component_adds_no_rights(admin_login, ui_alice_login):
    main_root = make_component(
        "todo-root",
        "<div>{% for name, child in child_items %}{{ child }}{% endfor %}</div>",
    )
    make_stylesheet("s", [("tests://ToDo", "todo-root")])
    alice_on_wip(admin_login, ui_alice_login)
    # alice may not read main's root component (the API refuses her its
    # GET): "inside this type" is not offered, and its name is not shown
    # (rulings S9, S13); overriding it (a copy she could read) is refused.
    When.http.get(reverse("ui-components-detail", args=[main_root.pk]))
    assert Http.response.status_code >= 400
    got = options(slug="todos", pointer=EXPLANATION)
    assert got["inside_type"] is None
    assert "todo-root" not in Http.response.content.decode()
    before = stored_state()
    new_component(
        400, slug="todos", pointer=EXPLANATION, inside_type=True, template=MARK
    )
    assert "inside_type" in problem()["errors"]
    assert "todo-root" not in Http.response.content.decode()
    assert stored_state() == before
    # May select wip and write stylesheets there, but not components: the
    # API's 403, and the stylesheet is not written either.
    user = assign_role(
        {
            f"http.{m}": [
                UI_STYLESHEET_TYPE_URI,
                collection_type_uri_for(UI_STYLESHEET_TYPE_URI),
            ]
            for m in ("GET", "POST", "PUT")
        },
        "alice",
    )
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    Given.http.force_authenticate(user)
    When.http.post(
        reverse("ui-components-list"),
        data=component(name="x", branch="wip", rbac_context="alice"),
        format="json",
    )
    Then.http.status_code_is(403)
    api = Http.response.json()
    assert new_component(403) == api
    assert stored_state() == before


@pytest.mark.django_db
@test_context
def test_generated_name_is_unique(admin_login, ui_alice_login):
    # Taken on the branch: an inherited name (an action must never override
    # main's component) and the branch's own.
    main_row = make_component("todo-lists-items-view", "<b>main</b>")
    make_wip(admin_login)
    create(
        "ui-components",
        component(name="todo-lists-items-view-2", branch="wip", rbac_context="alice"),
    )
    Given.http.force_authenticate(ui_alice_login)
    assert options(pointer="/items/0")["name"] == "todo-lists-items-view-3"
    got = new_component()
    assert got["written"][0] == {"kind": "component", "name": "todo-lists-items-view-3"}
    assert options(pointer="/items/0")["name"] == "todo-lists-items-view-4"
    assert [c["name"] for c in own_rows(UI_COMPONENT_TYPE_URI, "wip")] == [
        "todo-lists-items-view-2",
        "todo-lists-items-view-3",
    ]
    assert Resource.objects.get(pk=main_row.pk).etag == main_row.etag
    # Another mode is another name; on another branch the names are its own.
    assert options(pointer="/items/0", mode="edit")["name"] == "todo-lists-items-edit"
    assert options(pointer="/items/0", branch="main")["name"] == (
        "todo-lists-items-view-2"
    )


@pytest.mark.django_db
@test_context
def test_published_branch_refused(admin_login):
    make_wip(admin_login)
    make_branch("shop", "", parent="main", prefix="/shop/")
    before = stored_state()
    # Even for a superuser, and before anything is dispatched.
    Given.http.force_authenticate(admin_login)
    with (
        mock.patch(
            "scrud_django.ui.studio.new_component.call_view",
            side_effect=AssertionError("dispatched"),
        ),
        mock.patch(
            "scrud_django.ui.studio.views.call_view",
            side_effect=AssertionError("dispatched"),
        ),
    ):
        for branch in ("main", "shop"):
            new_component(400, branch=branch)
            assert problem()["type"] == READ_ONLY
            assert branch in problem()["detail"]
        new_component(400, branch="no-such-branch")
        assert problem()["type"] == UNAVAILABLE
    assert stored_state() == before


@pytest.mark.django_db
@test_context
def test_bad_requests_are_refused(admin_login, ui_alice_login):
    alice_on_wip(admin_login, ui_alice_login)
    before = stored_state()
    for body in (
        {"match": "number"},  # not one of the node's terms
        {"match": 3},
        {"inside_type": True},  # it has an IRI term
        {"pointer": "", "slug": "todos", "inside_type": True},  # the root
        {"inside_type": "yes"},
        {"template": None},
        {"pointer": "/nope"},
        {"mode": "other"},
        {"scope": ["page"]},
        {"slug": 3},
    ):
        new_component(400, **body)
    new_component(404, slug="no-such-type")
    assert stored_state() == before
    When.http.get(reverse("scrud-studio:new-component"))
    Then.http.status_code_is(405)
    Given.http.force_authenticate(None)
    new_component(401)
