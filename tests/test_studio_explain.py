"""The inspector explanation: for a node, every rule that matched it, best
first as `resolve_component` tries them, with its rank parts, its source and
why it won or lost; and the explain endpoint, which reads it on a branch in a
data context, hiding the rules of stylesheets the user may not read. These
tests serve the API at ``/api/`` and the studio at ``/studio/``
(``tests.mounted_urls``)."""

from types import SimpleNamespace

import pytest
from django.test import RequestFactory
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.exceptions import UIPointerNotFoundError
from scrud_django.registration import ResourceRegistration
from scrud_django.ui.cascade import (
    Rule,
    request_memo,
    resolve_component,
)
from scrud_django.ui.render import NodeInfo, node_at
from scrud_django.ui.selectors import TypeTerm, parse_selector
from scrud_django.ui.studio.explain import explain
from scrud_django.ui.types import UI_STYLESHEET_TYPE_URI

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_render import (
    may_select_branches,
    put_item,
    setup_main_and_wip,
)
from .test_ui_branch_types import make_branch, problem
from .test_ui_branch_versions import versions_of
from .test_ui_http import PREFIXES, grant, make_component, make_stylesheet
from .test_ui_render import make_component as render_component

SLUG = "todo-lists"
EX = "http://example.org/todo#"
UNAVAILABLE = "https://api.openteams.com/http-problem/ui-branch-unavailable"
NO_CONTEXT = "https://api.openteams.com/http-problem/ui-studio-context-unavailable"
NO_NODE = "https://api.openteams.com/http-problem/ui-pointer-not-found"
SOURCE_KEYS = {"selector", "component", "stylesheet", "branch", "applies_to"}


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def everything(source):
    return True


def rule(select, component, rank):
    return Rule(parse_selector(select, PREFIXES), component, rank)


def chosen(explained):
    """The component name `explained` says the node gets."""
    if explained["winner"] is None:
        return explained["fallback"]
    return explained["rules"][explained["winner"]]["component"]


STRING = [TypeTerm(schema_type="string")]
DATE = [
    TypeTerm(iri=EX + "Day"),
    TypeTerm(schema_type="string", format="date"),
    TypeTerm(schema_type="string"),
]

# (rules, scope chain, terms, mode, outcomes best first, chosen component)
AGREEMENT = {
    "more scopes win": (
        [
            rule("string", "scrud:string", (3, 0, 0)),
            rule("compact string", "scrud:json", (0, 0, 0)),
        ],
        ("page", "compact"),
        STRING,
        "view",
        ["wins", "outranked"],
        "scrud:json",
    ),
    "a scope not in the chain does not match": (
        [
            rule("string", "scrud:string", (0, 0, 0)),
            rule("sidebar string", "scrud:json", (3, 0, 0)),
        ],
        ("page",),
        STRING,
        "view",
        ["wins"],
        "scrud:string",
    ),
    "class term beats bare type": (
        [
            rule("string", "scrud:string", (3, 0, 0)),
            rule("ex:Day", "scrud:json", (0, 0, 0)),
        ],
        (),
        DATE,
        "view",
        ["wins", "outranked"],
        "scrud:json",
    ),
    # A bare type matches the format term too: the cascade rank decides.
    "bare type and format term: rank decides": (
        [
            rule("string", "scrud:string", (3, 0, 0)),
            rule("string[format=date]", "scrud:json", (0, 0, 0)),
        ],
        (),
        DATE,
        "view",
        ["wins", "outranked"],
        "scrud:string",
    ),
    "higher cascade rank wins": (
        [
            rule("string", "scrud:json", (1, 0, 5, 0)),
            rule("string", "scrud:string", (2, 0, 0, 7, 0)),
            rule("string", "scrud:number", (0, 0, 9)),
        ],
        (),
        STRING,
        "view",
        ["wins", "outranked", "outranked"],
        "scrud:string",
    ),
    "a missing component loses": (
        [
            rule("ex:Day", "scrud:nope", (0, 0, 2)),
            rule("string[format=date]", "never-stored", (0, 0, 1)),
            rule("string", "scrud:string", (0, 0, 0)),
        ],
        (),
        DATE,
        "view",
        ["missing component", "missing component", "wins"],
        "scrud:string",
    ),
    "the wrong mode loses": (
        [
            rule("ex:Day", "scrud:string-edit", (0, 0, 0)),
            rule("string", "scrud:string", (0, 0, 1)),
        ],
        (),
        DATE,
        "view",
        ["wrong mode", "wins"],
        "scrud:string",
    ),
    "the right mode wins": (
        [
            rule("ex:Day", "scrud:string-edit", (0, 0, 0)),
            rule("string", "scrud:string", (0, 0, 1)),
        ],
        (),
        DATE,
        "edit",
        ["wins", "outranked"],
        "scrud:string-edit",
    ),
    "a stored component in the wrong mode": (
        [rule("ex:Day", "stored-edit", (1, 0, 1, 0))],
        (),
        DATE,
        "view",
        ["wrong mode"],
        "scrud:string",
    ),
    "a stored component in its mode": (
        [rule("ex:Day", "stored-edit", (1, 0, 1, 0))],
        (),
        DATE,
        "edit",
        ["wins"],
        "stored-edit",
    ),
    "nothing serves: the fallback": (
        [
            rule("string", "scrud:nope", (0, 0, 0)),
            rule("string", "scrud:number-edit", (0, 0, 1)),
        ],
        (),
        STRING,
        "view",
        ["wrong mode", "missing component"],
        "scrud:string",
    ),
    "nothing matches: the fallback": (
        [rule("number", "scrud:number", (0, 0, 0))],
        (),
        STRING,
        "edit",
        [],
        "scrud:string-edit",
    ),
}


@pytest.mark.django_db
@pytest.mark.parametrize("case", list(AGREEMENT), ids=list(AGREEMENT))
@test_context
def test_explain_agrees_with_resolve_component(case):
    rules, scope_chain, terms, mode, outcomes, component = AGREEMENT[case]
    make_component("stored-edit", "<i>{{ value }}</i>", mode="edit")
    info = NodeInfo(
        pointer="/x",
        terms=terms,
        scope_chain=scope_chain,
        linked=(),
        rules=tuple(rules),
    )
    explained = explain(info, mode, readable=everything)
    resolved = resolve_component(tuple(rules), scope_chain, terms, mode)
    assert chosen(explained) == resolved.name == component
    assert [r["outcome"] for r in explained["rules"]] == outcomes
    if explained["winner"] is None:
        assert "wins" not in outcomes
    else:
        assert outcomes.index("wins") == explained["winner"]
        assert explained["fallback"] is None
    # Best first, as resolve_component tries them.
    ranks = [r["rank"] for r in explained["rules"]]
    assert ranks == sorted(ranks, reverse=True)


def request_for(user):
    request = RequestFactory().get("/")
    request.user = user
    return request


@pytest.mark.django_db
@test_context
def test_explanation_of_every_node_agrees_with_resolve_component(
    todo_list, login_no_roles
):
    make_component("row", "<b>{{ value.text }}</b>")
    make_component("row-edit", "<b>{{ value.text }}</b>", mode="edit")
    make_stylesheet(
        "s",
        [
            ("ex:TodoItem", "row"),
            ("compact ex:TodoItem", "row-edit"),
            ("compact string", "scrud:json"),
            ("boolean", "gone"),
        ],
    )
    pointers = ["", "/title", "/rbac_context", "/items", "/items/0"]
    pointers += ["/items/1/text", "/items/1/done"]
    for mode in ("view", "edit"):
        for scope_chain in ((), ("compact",)):
            for pointer in pointers:
                with request_memo():
                    info = node_at(
                        todo_list.content,
                        resource_type=todo_list.resource_type,
                        request=request_for(login_no_roles),
                        pointer=pointer,
                        scope_chain=scope_chain,
                        mode=mode,
                        contexts=["project-a"],
                    )
                    explained = explain(info, mode, readable=everything)
                    resolved = resolve_component(
                        info.rules, info.scope_chain, info.terms, mode
                    )
                assert explained["pointer"] == pointer
                assert chosen(explained) == resolved.name, (mode, pointer)


@pytest.mark.django_db
@test_context
def test_losers_say_why(todo_list, login_no_roles):
    make_component("row", "<b>{{ value.text }}</b>")
    make_component("row-edit", "<b>{{ value.text }}</b>", mode="edit")
    sheet = make_stylesheet(
        "s",
        [
            ("object", "scrud:json"),
            ("ex:TodoItem", "row"),
            ("ex:TodoItem", "gone"),
            ("compact ex:TodoItem", "row-edit"),
        ],
    )
    info = node_at(
        todo_list.content,
        resource_type=todo_list.resource_type,
        request=request_for(login_no_roles),
        pointer="/items/0",
        scope_chain=("compact",),
        mode="view",
        contexts=["project-a"],
    )
    explained = explain(info, "view", readable=everything)
    assert explained["pointer"] == "/items/0"
    assert explained["terms"] == [EX + "TodoItem", "object"]
    assert explained["scope"] == ["compact"]
    assert explained["winner"] == 2 and explained["fallback"] is None
    stored = {
        "stylesheet": "s",
        "branch": "main",
        "applies_to": "",
        "readable": True,
    }
    first, second, third, *rest = explained["rules"]
    # More scopes, but no view component.
    assert first == {
        **stored,
        "selector": f"compact {EX}TodoItem",
        "component": "row-edit",
        "rank": [1, 0, 1, 0, sheet.pk, 3],
        "outcome": "wrong mode",
    }
    # The later rule of the sheet ranks higher, but has no component.
    assert second == {
        **stored,
        "selector": f"{EX}TodoItem",
        "component": "gone",
        "rank": [0, 0, 1, 0, sheet.pk, 2],
        "outcome": "missing component",
    }
    assert third == {
        **stored,
        "selector": f"{EX}TodoItem",
        "component": "row",
        "rank": [0, 0, 1, 0, sheet.pk, 1],
        "outcome": "wins",
    }
    # A less specific term: outranked, whatever their cascade rank.
    assert rest[0] == {
        **stored,
        "selector": "object",
        "component": "scrud:json",
        "rank": [0, -1, 1, 0, sheet.pk, 0],
        "outcome": "outranked",
    }
    defaults = rest[1:]
    assert {r["component"] for r in defaults} == {"scrud:object", "scrud:object-edit"}
    for r in defaults:
        assert r["stylesheet"] == "scrud:default" and r["outcome"] == "outranked"
        assert r["rank"][:3] == [0, -1, 0] and r["readable"] is True
    # Nothing matched: the fallback, and no rule.
    info = node_at(
        todo_list.content,
        resource_type=todo_list.resource_type,
        request=request_for(login_no_roles),
        pointer="/items/0/done",
        scope_chain=(),
        mode="view",
        contexts=["project-a"],
    )
    explained = explain(
        NodeInfo(info.pointer, info.terms, info.scope_chain, info.linked, rules=()),
        "view",
        readable=everything,
    )
    assert explained["rules"] == [] and explained["winner"] is None
    assert explained["fallback"] == "scrud:boolean"


@pytest.mark.django_db
@test_context
def test_pointer_with_tilde_and_slash(login_no_roles):
    resource_type = SimpleNamespace(
        type_uri="tests://Odd",
        schema=SimpleNamespace(
            content={
                "type": "object",
                "properties": {
                    "a/b~c": {"type": "string", "format": "date"},
                    "absent": {"type": "string"},
                    "n": {"type": "string"},
                },
            }
        ),
        context=SimpleNamespace(content={}),
    )
    content = {"a/b~c": "2020-01-02", "unknown": "x", "n": 5}

    def at(pointer):
        return node_at(
            content,
            resource_type=resource_type,
            request=request_for(login_no_roles),
            pointer=pointer,
            scope_chain=(),
            mode="view",
            contexts=[""],
        )

    info = at("/a~1b~0c")
    assert info.pointer == "/a~1b~0c"
    explained = explain(info, "view", readable=everything)
    assert explained["pointer"] == "/a~1b~0c"
    assert explained["terms"] == ["string[format=date]", "string"]
    assert chosen(explained) == "scrud:string"
    assert at("").pointer == ""
    # A value not of its schema's type renders as JSON, whatever the rules.
    info = at("/n")
    assert info.as_json and info.rules == ()
    explained = explain(info, "view", readable=everything)
    assert explained["terms"] == ["integer"] and explained["rules"] == []
    assert explained["winner"] is None and explained["fallback"] == "scrud:json"
    # Pointers that name no node the preview renders.
    for pointer in ("/a/b~c", "/a~1b~1c", "/absent", "/unknown", "a~1b~0c"):
        with pytest.raises(UIPointerNotFoundError):
            at(pointer)
    with pytest.raises(UIPointerNotFoundError):
        at("/a~1b~0c/0")


@pytest.mark.django_db
@test_context
def test_ancestors_scopes_and_linked_stylesheets(todo_list, login_no_roles):
    render_component(
        "list-root",
        "{{ children.title }}",
        scopes={"title": "heading"},
        stylesheets=["inner"],
    )
    make_stylesheet("outer", [("ex:TodoList", "list-root")])
    inner = make_stylesheet("inner", [("heading string", "scrud:json")])
    info = node_at(
        todo_list.content,
        resource_type=todo_list.resource_type,
        request=request_for(login_no_roles),
        pointer="/title",
        scope_chain=("page",),
        mode="view",
        contexts=["project-a"],
    )
    assert info.scope_chain == ("page", "heading") and info.linked == ("inner",)
    explained = explain(info, "view", readable=everything)
    assert explained["scope"] == ["page", "heading"]
    # The linked stylesheet ranks above it as one of the app's.
    first, second = explained["rules"][:2]
    assert first["stylesheet"] == second["stylesheet"] == "inner"
    assert first["rank"] == [1, -1, 3, 0, 0] and first["outcome"] == "wins"
    assert second["rank"] == [1, -1, 1, 0, inner.pk, 0]
    assert second["outcome"] == "outranked"


def explain_url(slug=SLUG):
    reverse("todo-lists-list")  # the URLconf registers the types
    return reverse("scrud-studio:explain", args=[slug])


def get_explain(status=200, **params):
    When.http.get(explain_url(), params)
    Then.http.status_code_is(status)
    return Http.response.json()


def winning(explained):
    return explained["rules"][explained["winner"]]


def may_read_stylesheets(user, *contexts):
    for context in contexts:
        grant(user, {"http.GET": [UI_STYLESHEET_TYPE_URI]}, context)
    return user


def wip_stylesheet(name, rules):
    """A stylesheet stored as is on branch ``wip``, applying everywhere."""
    reverse("ui-stylesheets-list")
    return ResourceRegistration.register(
        {
            "name": name,
            "prefixes": PREFIXES,
            "rules": [{"select": s, "component": c} for s, c in rules],
            "rbac_context": "project-a",
            "applies_to": "",
            "branch": "wip",
        },
        "ui-stylesheets",
    )


@pytest.mark.django_db
@test_context
def test_explain_endpoint_uses_branch_and_context(todo_list, todo_reader_login):
    setup_main_and_wip()
    put_item("ui-components", "wip-row", "<s>{{ value.text }}</s>", "wip")
    wip_stylesheet("w", [("ex:TodoItem", "wip-row")])
    make_component("acme-row", "<u>{{ value.text }}</u>")
    make_stylesheet("acme", [("ex:TodoItem", "acme-row")], context="acme")
    make_stylesheet("pa", [("ex:TodoItem", "acme-row")], context="project-a")
    user = may_select_branches(todo_reader_login)
    may_read_stylesheets(user, "", "project-a", "acme")
    Given.http.force_authenticate(user)
    # main, by default or by name: main's stylesheet.
    for params in ({}, {"branch": "main"}, {"branch": ""}):
        explained = get_explain(pointer="/items/0", **params)
        assert {r.get("stylesheet") for r in explained["rules"]} == {
            "s",
            "scrud:default",
        }
        assert winning(explained) == {
            "readable": True,
            "selector": f"{EX}TodoItem",
            "component": "row",
            "stylesheet": "s",
            "branch": "main",
            "applies_to": "",
            "rank": winning(explained)["rank"],
            "outcome": "wins",
        }
    # wip: its own stylesheet ranks above main's.
    explained = get_explain(pointer="/items/0", branch="wip")
    assert winning(explained)["stylesheet"] == "w"
    assert winning(explained)["branch"] == "wip"
    assert winning(explained)["component"] == "wip-row"
    assert {r.get("stylesheet") for r in explained["rules"]} == {
        "s",
        "w",
        "scrud:default",
    }
    # A data context: its stylesheet ranks above the app-wide ones.
    explained = get_explain(pointer="/items/0", branch="wip", context="acme")
    assert winning(explained)["stylesheet"] == "acme"
    assert winning(explained)["applies_to"] == "acme"
    assert winning(explained)["branch"] == "main"
    # An instance is in its own contexts.
    explained = get_explain(pointer="/items/1", instance=todo_list.pk)
    assert winning(explained)["stylesheet"] == "pa"
    assert winning(explained)["applies_to"] == "project-a"
    # The root by default, and the mode and the scope chain.
    explained = get_explain(mode="edit", scope="page compact")
    assert explained["pointer"] == "" and explained["scope"] == ["page", "compact"]
    assert winning(explained)["component"] == "scrud:object-edit"
    explained = get_explain(pointer="/items/0/text", mode="edit")
    assert explained["terms"] == [EX + "text", "string"]
    assert winning(explained)["component"] == "scrud:string-edit"
    get_explain(400, mode="other")
    # Refusals, as the preview's.
    get_explain(400, branch="nope")
    assert problem()["type"] == UNAVAILABLE
    get_explain(400, context="elsewhere")
    assert problem()["type"] == NO_CONTEXT
    for pointer in ("/items/9", "/nope", "items", "/items/0/text/x"):
        get_explain(400, pointer=pointer)
        assert problem()["type"] == NO_NODE
    get_explain(404, instance=999999)
    When.http.get(reverse("scrud-studio:explain", args=["no-such-type"]))
    Then.http.status_code_is(404)
    Given.http.force_authenticate(None)
    get_explain(401)


@pytest.mark.django_db
@test_context
def test_unreadable_rules_show_rank_only(login_no_roles, ui_acme_stylesheet_login):
    make_component("secret-row", "<b>{{ value.text }}</b>")
    make_stylesheet("hidden-sheet", [("ex:TodoItem", "secret-row")])
    make_component("acme-row", "<u>{{ value.text }}</u>")
    make_stylesheet("acme-sheet", [("ex:TodoItem", "acme-row")], context="acme")
    Given.http.force_authenticate(login_no_roles)
    explained = get_explain(pointer="/items/0")
    body = Http.response.content.decode()
    hidden = winning(explained)
    assert set(hidden) == {"readable", "rank", "outcome"}
    assert hidden["readable"] is False and hidden["outcome"] == "wins"
    assert hidden["rank"][:3] == [0, 0, 1]
    for secret in ("secret-row", "hidden-sheet"):
        assert secret not in body
    # The default stylesheet's rules stay readable.
    defaults = [r for r in explained["rules"] if r["readable"]]
    assert defaults and all(r["stylesheet"] == "scrud:default" for r in defaults)
    assert all(
        set(r) == SOURCE_KEYS | {"readable", "rank", "outcome"} for r in defaults
    )
    # One who reads acme's stylesheets, not the app-wide ones.
    Given.http.force_authenticate(ui_acme_stylesheet_login)
    explained = get_explain(pointer="/items/0", context="acme")
    body = Http.response.content.decode()
    assert winning(explained)["stylesheet"] == "acme-sheet"
    unreadable = [r for r in explained["rules"] if not r["readable"]]
    assert len(unreadable) == 1
    assert set(unreadable[0]) == {"readable", "rank", "outcome"}
    assert unreadable[0]["outcome"] == "outranked"
    assert "secret-row" not in body and "hidden-sheet" not in body


@pytest.mark.django_db
@test_context
def test_unreadable_rules_of_a_pinned_snapshot(todo_reader_login):
    make_component("secret-row", "<b>{{ value.text }}</b>")
    make_stylesheet("hidden-sheet", [("ex:TodoItem", "secret-row")])
    make_branch("wip", "project-a", branching_point={"main": versions_of("main")[-1]})
    user = may_select_branches(todo_reader_login)
    Given.http.force_authenticate(user)
    explained = get_explain(pointer="/items/0", branch="wip@pinned")
    body = Http.response.content.decode()
    assert set(winning(explained)) == {"readable", "rank", "outcome"}
    assert "secret-row" not in body and "hidden-sheet" not in body
    # Readable, the snapshot's rule shows its source.
    may_read_stylesheets(user, "")
    explained = get_explain(pointer="/items/0", branch="wip@pinned")
    assert winning(explained)["stylesheet"] == "hidden-sheet"
    assert winning(explained)["component"] == "secret-row"
