"""The studio's live preview (E1): a POST (form-encoded, one ``request``
field of JSON) that renders a type's sample or an instance on a branch with
unsaved drafts layered over it, never storing them; marks the nodes a rule
matches as winning or losing; can show a target branch as it will be after a
push; and answers with a sandboxed document with an opaque origin, its
branch's CSS and JS inlined under a per-response nonce. These tests serve the
API at ``/api/`` and the studio at ``/studio/`` (``tests.mounted_urls``)."""

import json
import logging
import re
from html import unescape
from unittest import mock
from urllib.parse import urlencode

import pytest
from django.test import RequestFactory
from django.urls import reverse
from rest_framework.test import force_authenticate
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django import collection_type_uri_for
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.ui.branches import (
    UI_BRANCH_TYPE_URI,
    UI_CSS_TYPE_URI,
    UI_JS_TYPE_URI,
)
from scrud_django.ui.studio import views
from scrud_django.ui.studio.live_preview import MAX_DRAFTS
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import TYPE_URI_TODO_LIST
from .steps import *  # noqa: F403, F401
from .test_studio_page import setup_branches
from .test_ui_branch_push import stored_state
from .test_ui_branch_render import put_item
from .test_ui_branch_types import make_branch, problem
from .test_ui_embeds import embedded_content, host, make_list, url_of
from .test_ui_http import PREFIXES, grant, html, make_component, make_stylesheet

SLUG = "todo-lists"
UNAVAILABLE = "https://api.openteams.com/http-problem/ui-branch-unavailable"
READ_ONLY = "https://api.openteams.com/http-problem/ui-studio-branch-read-only"
INVALID = "https://api.openteams.com/http-problem/ui-studio-preview-invalid"
FAILED = '<!-- scrud-ui: component "{}" failed -->'
WIP = {"branch": "wip", "rbac_context": "alice"}
WRITES = ("GET", "POST", "PUT", "DELETE")


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def live(status=200, **fields):
    """POST a live preview request (its JSON in the form field
    ``request``) with `fields` over the defaults; returns the page."""
    reverse("todo-lists-list")  # the URLconf registers the types
    body = {
        "type": SLUG,
        "branch": "wip",
        "mode": "view",
        "scope": [],
        "context": "",
        "instance": None,
        "drafts": [],
        "mark": None,
        "after_push": None,
        **fields,
    }
    When.http.post(
        reverse("scrud-studio:live-preview"),
        data=urlencode({"request": json.dumps(body)}),
        content_type="application/x-www-form-urlencoded",
    )
    Then.http.status_code_is(status)
    return html()


def draft(kind, **content):
    return {"kind": kind, "content": content}


def django_component(name, template, mode="view"):
    return draft("component", name=name, engine="django", mode=mode, template=template)


def errors_of(page):
    """The draft errors the page lists (``#scrud-preview-errors``)."""
    found = re.search(
        r'<script type="application/json" id="scrud-preview-errors"'
        r' nonce="[^"]+">(.*?)</script>',
        page,
        re.S,
    )
    assert found, page
    return json.loads(found.group(1))


def grant_items(user, context, *type_uris, methods=WRITES):
    """Give `user` `methods` on `type_uris` (and their lists) in `context`."""
    uris = [u for t in type_uris for u in (t, collection_type_uri_for(t))]
    grant(user, {f"http.{m}": uris for m in methods}, context)


def wip_for(user):
    """``wip`` (in "alice", over main), which `user` may select."""
    reverse("todo-lists-list")
    make_branch("wip", "alice")
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    Given.http.force_authenticate(user)


def main_row():
    """main styles each to-do item with the component ``row``."""
    make_component("row", "<b>main {{ value.text }}</b>")
    make_stylesheet("s", [("ex:TodoItem", "row")])


@pytest.mark.django_db
@test_context
def test_drafts_render_and_are_never_stored(ui_alice_login):
    main_row()
    grant_items(ui_alice_login, "alice", UI_CSS_TYPE_URI, UI_JS_TYPE_URI)
    wip_for(ui_alice_login)
    # wip's own row of "row": a draft replaces it (it is wip's own too).
    put_item("ui-components", "row", "<u>wip {{ value.text }}</u>", "wip", "alice")
    before = (stored_state(), Resource.objects.count())
    page = live()
    assert "<u>wip Lorem ipsum</u>" in page
    page = live(
        drafts=[
            django_component("row", "<i>draft {{ value.text }}</i>"),
            django_component("title-c", "<h1>{{ value }}</h1>"),
            draft(
                "stylesheet",
                name="studio",
                prefixes=PREFIXES,
                rules=[{"select": "ex:title", "component": "title-c"}],
            ),
            draft("css", name="site", css="h1 { color: rebeccapurple }"),
            draft("js", name="hello", js="window.hello = 1;"),
        ]
    )
    assert errors_of(page) == []
    assert "<i>draft Lorem ipsum</i>" in page
    assert "<u>wip" not in page and "<b>main" not in page
    assert "<h1>Lorem ipsum</h1>" in page
    nonce = re.search(r"'nonce-([^']+)'", Http.response["Content-Security-Policy"])
    nonce = nonce.group(1)
    assert f'<style nonce="{nonce}">h1 {{ color: rebeccapurple }}</style>' in page
    assert f'<script nonce="{nonce}">window.hello = 1;</script>' in page
    # Nothing was written.
    assert (stored_state(), Resource.objects.count()) == before
    # The next preview, without the drafts, shows what is stored.
    page = live()
    assert "<u>wip Lorem ipsum</u>" in page and "draft" not in page
    assert "rebeccapurple" not in page and "window.hello" not in page


@pytest.mark.django_db
@test_context
def test_draft_stylesheet_ranks_as_its_save_would(ui_alice_login):
    """A draft of a stylesheet ranks where its save would put it: an own
    row's draft where that row is, a new one after every stored one."""
    make_component("one", "<i>one {{ value.text }}</i>")
    make_component("two", "<i>two {{ value.text }}</i>")
    make_component("three", "<i>three {{ value.text }}</i>")
    wip_for(ui_alice_login)
    rules = [{"select": "ex:TodoItem", "component": "one"}]
    for name, component in (("a", "one"), ("b", "two")):
        ResourceRegistration.register(
            {
                "name": name,
                "prefixes": PREFIXES,
                "rules": [{"select": "ex:TodoItem", "component": component}],
                "applies_to": "",
                **WIP,
            },
            "ui-stylesheets",
        )
    # b is stored after a: it ranks higher, and wins.
    assert "<i>two Lorem ipsum</i>" in live()
    # A draft of b ranks where b is: it still wins over a.
    rules = [{"select": "ex:TodoItem", "component": "three"}]
    page = live(drafts=[draft("stylesheet", name="b", prefixes=PREFIXES, rules=rules)])
    assert "<i>three Lorem ipsum</i>" in page
    # A draft of a stays below b.
    page = live(drafts=[draft("stylesheet", name="a", prefixes=PREFIXES, rules=rules)])
    assert "<i>two Lorem ipsum</i>" in page
    # A new stylesheet ranks above both, as a new row would.
    page = live(drafts=[draft("stylesheet", name="c", prefixes=PREFIXES, rules=rules)])
    assert "<i>three Lorem ipsum</i>" in page


@pytest.mark.django_db
@test_context
def test_draft_user_could_not_save_is_refused(login_no_roles):
    main_row()
    user = login_no_roles
    wip_for(user)
    # May read components in alice, not write them.
    grant_items(user, "alice", UI_COMPONENT_TYPE_URI, methods=("GET",))
    page = live(drafts=[django_component("row", "<i>draft</i>")])
    assert "<i>draft</i>" not in page and "<b>main Lorem ipsum</b>" in page
    (refused,) = errors_of(page)
    assert refused["kind"] == "component" and refused["name"] == "row"
    assert refused["messages"] and all(isinstance(m, str) for m in refused["messages"])
    assert "permission" in " ".join(refused["messages"]).lower()
    # May create components in alice, not update them: a draft of wip's own
    # row is an update, and is refused; a new one is a create, and renders.
    grant_items(user, "alice", UI_COMPONENT_TYPE_URI, methods=("POST",))
    page = live(drafts=[django_component("row", "<i>draft {{ value.text }}</i>")])
    assert errors_of(page) == [] and "<i>draft Lorem ipsum</i>" in page
    put_item("ui-components", "row", "<u>wip {{ value.text }}</u>", "wip", "alice")
    page = live(drafts=[django_component("row", "<i>draft {{ value.text }}</i>")])
    assert "<i>draft" not in page and "<u>wip Lorem ipsum</u>" in page
    (refused,) = errors_of(page)
    assert refused["name"] == "row" and refused["messages"]


@pytest.mark.django_db
@test_context
def test_invalid_draft_reports_save_errors(ui_alice_login):
    main_row()
    wip_for(ui_alice_login)
    bad = [
        django_component("row", "{% if %}"),
        draft(
            "stylesheet",
            name="studio",
            rules=[{"select": "ex:TodoItem", "component": "row"}, {"select": "strin"}],
        ),
        draft("component", name="no-engine", mode="view", template="x"),
        draft("component", engine="django", mode="view", template="x"),
    ]
    page = live(drafts=[*bad, django_component("ok", "<i>ok</i>")])
    errors = errors_of(page)
    assert [(e["kind"], e["name"]) for e in errors] == [
        ("component", "row"),
        ("stylesheet", "studio"),
        ("component", "no-engine"),
        ("component", None),
    ]
    # Each is what a save of it answers.
    for found, item in zip(errors[:3], bad[:3], strict=True):
        content = {k: v for k, v in item["content"].items() if k != "name"}
        When.http.post(
            reverse("scrud-studio:save-item"),
            data={
                "kind": item["kind"],
                "branch": "wip",
                "name": item["content"]["name"],
                "content": content,
            },
            format="json",
        )
        Then.http.status_code_is(400)
        assert found["messages"] == [problem()["detail"]]
    assert "template is invalid" in errors[0]["messages"][0]
    assert "selector is invalid" in errors[1]["messages"][0]
    assert errors[3]["messages"] == ["A name is required."]
    # The preview renders without them.
    assert "<b>main Lorem ipsum</b>" in page
    assert not Resource.objects.filter(content__branch="wip").exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "engine, template",
    [
        ("jinja2", "{{ value.text.upper().nope() }}"),
        ("django", '{% url "no-such-route" %}'),
    ],
)
@test_context
def test_draft_raising_at_render_falls_back(ui_alice_login, engine, template):
    """Review Focus 3: a draft that compiles but raises renders the built-in,
    after the usual comment, never a 500."""
    main_row()
    wip_for(ui_alice_login)
    page = live(
        drafts=[
            draft(
                "component", name="row", engine=engine, mode="view", template=template
            )
        ]
    )
    assert errors_of(page) == []
    assert FAILED.format("row") + '<dl class="scrud-object">' in page
    assert "Lorem ipsum" in page and "<b>main" not in page


@pytest.mark.django_db
@test_context
def test_drafts_on_published_branch_refused(admin_login):
    main_row()
    make_branch("shop", "", parent="main", prefix="/shop/")
    Given.http.force_authenticate(admin_login)
    before = stored_state()
    for branch in ("main", "shop"):
        live(400, branch=branch, drafts=[django_component("row", "<i>x</i>")])
        assert problem()["type"] == READ_ONLY
        assert Http.response["Content-Security-Policy"].startswith("sandbox ")
        # Without drafts it previews.
        assert "<b>main Lorem ipsum</b>" in live(branch=branch)
    assert stored_state() == before
    # Unknown, or not a selection value: unavailable.
    for branch in ("nope", "Not A Value"):
        live(400, branch=branch)
        assert problem()["type"] == UNAVAILABLE


@pytest.mark.django_db
@test_context
def test_after_push_needs_get_only_and_shows_source_overrides(
    login_no_roles, ui_bob_login
):
    main_row()
    make_component("shout", "<em>SHOUT</em>")
    make_stylesheet("extra", [("ex:title", "shout")])
    make_branch("wip", "alice")
    put_item("ui-components", "row", "<u>wip {{ value.text }}</u>", "wip", "alice")
    # wip deletes main's "extra".
    ResourceRegistration.register(
        {"name": "extra", "deleted": True, "applies_to": "", **WIP}, "ui-stylesheets"
    )
    # May GET wip, and nothing else: no write anywhere.
    user = login_no_roles
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    Given.http.force_authenticate(user)
    before = stored_state()
    page = live(branch="main")
    assert "<b>main Lorem ipsum</b>" in page and "<em>SHOUT</em>" in page
    page = live(branch="main", after_push={"source": "wip"})
    assert "<u>wip Lorem ipsum</u>" in page and "<b>main" not in page
    assert "<em>SHOUT</em>" not in page
    assert errors_of(page) == []
    assert stored_state() == before
    # Without GET on the source: 403, as for one that does not exist.
    Given.http.force_authenticate(ui_bob_login)
    for source in ("wip", "nope"):
        live(403, branch="main", after_push={"source": source})
        assert Http.response["Content-Security-Policy"].startswith("sandbox ")
    live(400, branch="main", after_push={"source": 5})
    assert problem()["type"] == INVALID


@pytest.mark.django_db
@test_context
def test_after_push_with_drafts_refused(admin_login):
    main_row()
    make_branch("wip", "alice")
    Given.http.force_authenticate(admin_login)
    live(
        400,
        branch="main",
        after_push={"source": "wip"},
        drafts=[django_component("row", "<i>x</i>")],
    )
    assert problem()["type"] == INVALID
    # A request that is not the expected JSON is invalid too.
    reverse("todo-lists-list")
    too_many = [django_component(f"c{i}", "x") for i in range(MAX_DRAFTS + 1)]
    for body in (
        "not json",
        "[]",
        json.dumps({"type": SLUG, "mode": "other"}),
        # Nested deeper than the JSON parser recurses: never a 500.
        "[" * 200_000 + "]" * 200_000,
        json.dumps({"type": SLUG, "drafts": too_many}),
    ):
        When.http.post(
            reverse("scrud-studio:live-preview"),
            data=urlencode({"request": body}),
            content_type="application/x-www-form-urlencoded",
        )
        Then.http.status_code_is(400)
        assert problem()["type"] == INVALID
        assert Http.response["Content-Security-Policy"].startswith("sandbox ")
    # As many as allowed is fine.
    live(drafts=too_many[:MAX_DRAFTS], branch="wip")
    When.http.post(
        reverse("scrud-studio:live-preview"),
        data=urlencode({}),
        content_type="application/x-www-form-urlencoded",
    )
    Then.http.status_code_is(400)
    assert problem()["type"] == INVALID


def nodes(page):
    """``{pointer: (match, outcome)}`` of the top-level scrud-nodes of
    `page`, from their markers (``None`` when unmarked)."""
    found = {}
    for tag in re.findall(r"<scrud-node [^>]*>", page):
        pointer = unescape(re.search(r'data-pointer="([^"]*)"', tag).group(1))
        match = re.search(r'data-scrud-match="([^"]*)"', tag)
        outcome = re.search(r'data-scrud-match-outcome="([^"]*)"', tag)
        found.setdefault(
            pointer,
            (
                match.group(1) if match else None,
                unescape(outcome.group(1)) if outcome else None,
            ),
        )
    return found


def expected_marks(pointer, mark, mode, scope):
    """The marker the inspector's explanation implies for the node at
    `pointer`, with `mark`."""
    url = reverse("scrud-studio:explain", args=[SLUG])
    When.http.get(
        url,
        {"branch": "main", "mode": mode, "scope": " ".join(scope), "pointer": pointer},
    )
    Then.http.status_code_is(200)
    explained = Http.response.json()
    marked = [
        index
        for index, rule in enumerate(explained["rules"])
        if rule["stylesheet"] == mark["stylesheet"]
        and rule["applies_to"] == mark["applies_to"]
        and rule["rank"][-1] == mark["index"]
    ]
    if not marked:
        return (None, None)
    if explained["winner"] in marked:
        return ("wins", "wins")
    return ("loses", explained["rules"][marked[0]]["outcome"])


def two_sheets():
    make_stylesheet("a", [("ex:TodoItem", "row")])
    make_stylesheet("b", [("ex:TodoItem", "row-b")])


# (setup, mark, mode, scope, {pointer: expected match})
MARKS = {
    "scoped rule wins in its scope": (
        lambda: make_stylesheet(
            "s", [("ex:TodoItem", "row"), ("compact ex:TodoItem", "row-b")]
        ),
        {"stylesheet": "s", "applies_to": "", "index": 1},
        "view",
        ["compact"],
        {"/items/0": "wins"},
    ),
    "scoped rule does not match out of its scope": (
        lambda: make_stylesheet(
            "s", [("ex:TodoItem", "row"), ("compact ex:TodoItem", "row-b")]
        ),
        {"stylesheet": "s", "applies_to": "", "index": 1},
        "view",
        [],
        {"/items/0": None},
    ),
    "fewer scopes lose": (
        lambda: make_stylesheet(
            "s", [("ex:TodoItem", "row"), ("compact ex:TodoItem", "row-b")]
        ),
        {"stylesheet": "s", "applies_to": "", "index": 0},
        "view",
        ["compact"],
        {"/items/0": "loses"},
    ),
    "a less specific term loses": (
        lambda: make_stylesheet(
            "s", [("object", "scrud:object"), ("ex:TodoItem", "row-b")]
        ),
        {"stylesheet": "s", "applies_to": "", "index": 0},
        "view",
        [],
        {"/items/0": "loses", "": "wins"},
    ),
    "a lower cascade rank loses": (
        two_sheets,
        {"stylesheet": "a", "applies_to": "", "index": 0},
        "view",
        [],
        {"/items/0": "loses"},
    ),
    "the higher cascade rank wins": (
        two_sheets,
        {"stylesheet": "b", "applies_to": "", "index": 0},
        "view",
        [],
        {"/items/0": "wins"},
    ),
    "a missing component loses": (
        lambda: make_stylesheet("s", [("ex:TodoItem", "gone")]),
        {"stylesheet": "s", "applies_to": "", "index": 0},
        "view",
        [],
        {"/items/0": "loses"},
    ),
    "the wrong mode loses": (
        lambda: make_stylesheet("s", [("ex:TodoItem", "row-edit")]),
        {"stylesheet": "s", "applies_to": "", "index": 0},
        "view",
        [],
        {"/items/0": "loses"},
    ),
    "the right mode wins": (
        lambda: make_stylesheet("s", [("ex:TodoItem", "row-edit")]),
        {"stylesheet": "s", "applies_to": "", "index": 0},
        "edit",
        [],
        {"/items/0": "wins"},
    ),
    "the default stylesheet's rules are marked too": (
        lambda: None,
        {"stylesheet": None, "applies_to": "", "index": None},
        "view",
        [],
        {},
    ),
    "a rule that is not there marks nothing": (
        lambda: make_stylesheet("s", [("ex:TodoItem", "row")]),
        {"stylesheet": "s", "applies_to": "", "index": 7},
        "view",
        [],
        {"/items/0": None, "": None},
    ),
    "another context's stylesheet marks nothing": (
        lambda: make_stylesheet("s", [("ex:TodoItem", "row")]),
        {"stylesheet": "s", "applies_to": "acme", "index": 0},
        "view",
        [],
        {"/items/0": None},
    ),
}


@pytest.mark.django_db
@pytest.mark.parametrize("case", list(MARKS), ids=list(MARKS))
@test_context
def test_match_markers_agree_with_resolve_component(case, admin_login):
    setup, mark, mode, scope, expected = MARKS[case]
    make_component("row", "<b>{{ value.text }}</b>")
    make_component("row-b", "<i>{{ value.text }}</i>")
    make_component("row-edit", "<u>{{ value.text }}</u>", mode="edit")
    setup()
    Given.http.force_authenticate(admin_login)
    if mark["stylesheet"] is None:
        # The default stylesheet's first rule, by its name.
        from scrud_django.ui.cascade import _default_stylesheet

        mark = {**mark, "stylesheet": _default_stylesheet()["name"], "index": 0}
    page = live(branch="main", mode=mode, scope=scope, mark=mark)
    found = nodes(page)
    assert found, page
    for pointer, match in expected.items():
        assert found[pointer][0] == match, (pointer, found[pointer])
    marked = 0
    for pointer, actual in found.items():
        assert actual == expected_marks(pointer, mark, mode, scope), pointer
        marked += actual[0] is not None
    if case == "the default stylesheet's rules are marked too":
        assert marked
    # Without a mark, no node is marked.
    assert "data-scrud-match" not in live(branch="main", mode=mode, scope=scope)


@pytest.mark.django_db
@test_context
def test_mark_on_a_draft_rule(ui_alice_login):
    main_row()
    wip_for(ui_alice_login)
    rules = [{"select": "ex:TodoItem", "component": "row-d"}]
    page = live(
        drafts=[
            django_component("row-d", "<i>{{ value.text }}</i>"),
            draft("stylesheet", name="studio", prefixes=PREFIXES, rules=rules),
        ],
        mark={"stylesheet": "studio", "applies_to": "", "index": 0},
    )
    assert nodes(page)["/items/0"] == ("wins", "wins")
    # main's rule now loses to it (marking it needs GET on main's
    # stylesheet, as the inspector's showing it does).
    grant_items(ui_alice_login, "", UI_STYLESHEET_TYPE_URI, methods=("GET",))
    page = live(
        drafts=[
            django_component("row-d", "<i>{{ value.text }}</i>"),
            draft("stylesheet", name="studio", prefixes=PREFIXES, rules=rules),
        ],
        mark={"stylesheet": "s", "applies_to": "", "index": 0},
    )
    assert nodes(page)["/items/0"] == ("loses", "lower cascade rank")
    for bad in (
        {"stylesheet": "s"},
        {"stylesheet": "s", "applies_to": "", "index": "0"},
    ):
        live(400, mark=bad)
        assert problem()["type"] == INVALID


EXECUTABLE = re.compile(r'^(?:|text/javascript|module)$')


@pytest.mark.django_db
@test_context
def test_preview_csp_is_opaque_and_nonce_only(ui_alice_login):
    main_row()
    grant_items(ui_alice_login, "alice", UI_CSS_TYPE_URI, UI_JS_TYPE_URI)
    wip_for(ui_alice_login)
    drafts = [
        draft("css", name="site", css="h1 { color: red }"),
        draft("js", name="hello", js="window.hello = 1;"),
        django_component("row", "{% if %}"),  # an error to list
    ]
    page = live(drafts=drafts)
    policy = Http.response["Content-Security-Policy"]
    nonce = re.fullmatch(
        r"sandbox allow-scripts; default-src 'none'; "
        r"script-src 'self' 'nonce-([A-Za-z0-9_-]{16,})'; "
        r"style-src 'self' 'nonce-\1'; img-src 'self' data:; connect-src 'self'",
        policy,
    )
    assert nonce, policy
    nonce = nonce.group(1)
    assert "allow-same-origin" not in policy
    assert Http.response["X-Frame-Options"] == "SAMEORIGIN"
    assert Http.response["Content-Type"] == "text/html; charset=utf-8"
    # Every inline style and every script the browser runs carries the
    # nonce; the bridge and the page's scripts load by URL.
    styles = re.findall(r"<style[^>]*>", page)
    assert styles and all(tag == f'<style nonce="{nonce}">' for tag in styles)
    scripts = re.findall(r"<script([^>]*)>", page)
    srcs = []
    for attrs in scripts:
        kind = re.search(r'type="([^"]*)"', attrs)
        src = re.search(r'src="([^"]*)"', attrs)
        if src:
            srcs.append(src.group(1))
            continue
        if kind is None or EXECUTABLE.match(kind.group(1)):
            assert f'nonce="{nonce}"' in attrs, attrs
        else:
            # Data blocks: the errors list carries the nonce; the frame's
            # content is JSON, which no browser runs.
            assert kind.group(1) == "application/json", attrs
            assert f'nonce="{nonce}"' in attrs or 'class="scrud-content"' in attrs
    assert "/static/scrud_studio/preview-bridge.js" in srcs
    assert 'style="' not in page.split("<main>")[0]
    assert errors_of(page)[0]["name"] == "row"
    # A nonce per response.
    live(drafts=drafts)
    assert Http.response["Content-Security-Policy"] != policy
    # Refusals carry the sandbox too.
    live(400, branch="nope")
    assert Http.response["Content-Security-Policy"].startswith(
        "sandbox allow-scripts; default-src 'none';"
    )
    assert Http.response["X-Frame-Options"] == "SAMEORIGIN"


@pytest.mark.django_db
@test_context
def test_live_preview_needs_login_and_post():
    reverse("todo-lists-list")
    url = reverse("scrud-studio:live-preview")
    When.http.post(
        url,
        data=urlencode({"request": json.dumps({"type": SLUG})}),
        content_type="application/x-www-form-urlencoded",
    )
    Then.http.status_code_is(401)
    When.http.get(url)
    Then.http.status_code_is(401)


@pytest.mark.django_db
@test_context
def test_live_preview_is_a_post_with_csrf(ui_alice_login):
    from rest_framework.test import APIClient

    main_row()
    reverse("todo-lists-list")
    make_branch("wip", "alice")
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(ui_alice_login)
    body = urlencode({"request": json.dumps({"type": SLUG, "branch": "main"})})
    response = client.post(
        reverse("scrud-studio:live-preview"),
        data=body,
        content_type="application/x-www-form-urlencoded",
    )
    assert response.status_code == 403
    assert response["Content-Security-Policy"].startswith("sandbox allow-scripts;")
    assert response["X-Frame-Options"] == "SAMEORIGIN"
    response = client.get(reverse("scrud-studio:live-preview"))
    assert response.status_code == 405
    assert response["Content-Security-Policy"].startswith("sandbox allow-scripts;")


@pytest.mark.django_db
@test_context
def test_errors_django_answers_are_sandboxed_too(ui_alice_login, settings):
    """An exception DRF does not answer (too many form fields, an error) is
    answered as Django answers it, with the sandbox."""
    reverse("todo-lists-list")
    make_branch("wip", "alice")
    Given.http.force_authenticate(ui_alice_login)
    settings.DATA_UPLOAD_MAX_NUMBER_FIELDS = 3
    When.http.post(
        reverse("scrud-studio:live-preview"),
        data=urlencode([("request", "{}"), *[(f"f{i}", "x") for i in range(5)]]),
        content_type="application/x-www-form-urlencoded",
    )
    Then.http.status_code_is(400)
    assert Http.response["Content-Security-Policy"].startswith("sandbox ")
    assert Http.response["X-Frame-Options"] == "SAMEORIGIN"
    settings.DATA_UPLOAD_MAX_NUMBER_FIELDS = 1000
    # An unexpected error: Django's 500, sandboxed (the test settings
    # propagate errors, as Django then does: not here).
    settings.DEBUG_PROPAGATE_EXCEPTIONS = False
    request = RequestFactory().post(
        reverse("scrud-studio:live-preview"),
        data=urlencode({"request": json.dumps({"type": SLUG, "branch": "main"})}),
        content_type="application/x-www-form-urlencoded",
    )
    force_authenticate(request, user=ui_alice_login)
    with mock.patch(
        "scrud_django.ui.studio.views.render_live_preview",
        side_effect=RuntimeError("boom"),
    ):
        response = views.live_preview(request)
    assert response.status_code == 500
    assert response["Content-Security-Policy"].startswith("sandbox allow-scripts;")
    assert response["X-Frame-Options"] == "SAMEORIGIN"


@pytest.mark.django_db
@test_context
def test_mark_tells_nothing_of_an_unreadable_stylesheet(login_no_roles):
    """A user who may select wip but not GET its stylesheets marks nothing
    with them, exactly as with a stylesheet that does not exist; the
    inspector hides their rules alike."""
    make_component("row", "<b>{{ value.text }}</b>")
    wip_for(login_no_roles)
    ResourceRegistration.register(
        {
            "name": "secret",
            "prefixes": PREFIXES,
            "rules": [{"select": "ex:TodoItem", "component": "row"}],
            "applies_to": "",
            **WIP,
        },
        "ui-stylesheets",
    )
    secret = {"stylesheet": "secret", "applies_to": "", "index": 0}
    missing = {"stylesheet": "missing", "applies_to": "", "index": 0}

    def without_nonce(page):
        return re.sub(r'nonce="[^"]*"', "", page)

    page = live(mark=secret)
    assert "<b>Lorem ipsum</b>" in page  # the rule applies, and wins
    assert "data-scrud-match" not in page
    assert without_nonce(page) == without_nonce(live(mark=missing))
    When.http.get(
        reverse("scrud-studio:explain", args=[SLUG]),
        {"branch": "wip", "pointer": "/items/0"},
    )
    Then.http.status_code_is(200)
    explained = Http.response.json()
    assert explained["rules"][explained["winner"]]["readable"] is False
    # With GET on stylesheets in alice, the same mark marks the node.
    grant_items(login_no_roles, "alice", UI_STYLESHEET_TYPE_URI, methods=("GET",))
    assert nodes(live(mark=secret))["/items/0"] == ("wins", "wins")


@pytest.mark.django_db
@test_context
def test_mark_on_a_packaged_stylesheet_for_anyone(login_no_roles):
    """A packaged layer's stylesheet is public (spec R1): a user who may
    read no stylesheet marks the nodes of its rules, as the inspector shows
    them."""
    wip_for(login_no_roles)
    mark = {"stylesheet": "layertest:sheet", "applies_to": "", "index": 0}
    page = live(scope=["layertest"], mark=mark)
    assert nodes(page)[""] == ("wins", "wins")


@pytest.mark.django_db
@test_context
def test_embeds_in_live_preview_compose_server_side(todo_list, todo_owner_login):
    other = make_list("Chores", items=["Sweep"])
    main_row()
    # In view mode: the owner's own mode would be edit, where "row" (a view
    # component) does not serve.
    host(
        '<section class="host">{{ embed("' + url_of(other) + '", mode="view") }}'
        "</section>"
    )
    user = todo_owner_login
    grant_items(user, "alice", UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI)
    # To mark main's "s", as the inspector shows its rules.
    grant_items(user, "", UI_STYLESHEET_TYPE_URI, methods=("GET",))
    wip_for(user)
    page = live(
        instance=todo_list.pk,
        scope=["host"],
        drafts=[django_component("row", "<i>draft {{ value.text }}</i>")],
        mark={"stylesheet": "s", "applies_to": "", "index": 0},
    )
    assert errors_of(page) == []
    assert '<section class="host">' in page
    # The embedded list, rendered in process on wip with the draft.
    assert "<i>draft Sweep</i>" in page
    for attribute in ("hx-put", "hx-ext", "hx-trigger", "hx-delete"):
        assert attribute not in page
    assert "<form" not in page
    # Its nodes carry their own markers, and the mark applies there too.
    embedded = page[page.index(f'data-scrud-href="{url_of(other)}"') :]
    assert re.search(
        r'<scrud-node data-pointer="/items/0" data-scrud-match="wins"', embedded
    )
    assert not Resource.objects.filter(content__branch="wip").exists()


@pytest.mark.django_db
@test_context
def test_studio_data_embedded_in_live_preview_shows_stored_items_not_drafts(
    todo_list, ui_alice_login
):
    """A live preview's drafts are render-only: the studio's computed data
    embedded in the preview (a branch's items) is what its URL answers as
    JSON, the stored items, never the drafts the preview renders with."""
    setup_branches(ui_alice_login)
    items = reverse("ui-branch-items-detail", args=["wip"])
    host('<section class="host">{{ embed("' + items + '", mode="view") }}</section>')
    grant(
        ui_alice_login,
        {"http.GET": [TYPE_URI_TODO_LIST, collection_type_uri_for(TYPE_URI_TODO_LIST)]},
        todo_list.content["rbac_context"],
    )
    Given.http.force_authenticate(ui_alice_login)
    When.http.get(items, HTTP_ACCEPT="application/json")
    Then.http.status_code_is(200)
    plain = Http.response.json()
    page = live(
        instance=todo_list.pk,
        scope=["host"],
        drafts=[
            draft(
                "component",
                name="row",
                engine="jinja2",
                mode="view",
                template="<u>DRAFT</u>",
            )
        ],
    )
    assert errors_of(page) == []
    section = re.search(r'<section class="host">(.*?)</section>', page, re.S)
    assert section, page
    assert embedded_content(section.group(1)) == plain
    assert "DRAFT" not in section.group(1)


@pytest.mark.django_db
@test_context
def test_live_preview_of_unreadable_instance_shows_nothing_of_it(
    todo_list, todo_reader_login
):
    """An instance the user may not GET is refused as its resource view
    refuses it (the same status and problem as its URL answers as JSON),
    sandboxed, and the answer shows nothing of it: not its content, not
    its title, not a frame of it."""
    secret = make_list("Secret plans", context="project-b", items=["Hidden item"])
    wip_for(todo_reader_login)
    When.http.get(url_of(secret), HTTP_ACCEPT="application/json")
    status = Http.response.status_code
    assert status in (403, 404)
    refusal = Http.response.json()
    page = live(status=status, instance=secret.pk)
    assert Http.response.json() == refusal
    assert Http.response["Content-Security-Policy"].startswith("sandbox ")
    for shown in ("Secret plans", "Hidden item", "project-b", url_of(secret)):
        assert shown not in page
    # The readable instance previews, as a check that the request is right.
    assert "Groceries" in live(instance=todo_list.pk)


@pytest.mark.django_db
@test_context
def test_layer_prefixed_drafts_follow_the_save_rules(ui_alice_login):
    """A draft with a layer prefix is what its save would be: an override
    of an installed layer's item (``layertest:sheet``) renders, and ranks
    as the stored override it would be (above main's stored stylesheet, not
    beneath it as the layer's own); a new prefixed name (``layertest:new``)
    and a ``scrud:`` name are listed in the errors with the message their
    save answers."""
    main_row()
    make_component("over", "<i>over {{ value.text }}</i>")
    wip_for(ui_alice_login)
    rules = [{"select": "ex:TodoItem", "component": "over"}]
    override = draft(
        "stylesheet", name="layertest:sheet", prefixes=PREFIXES, rules=rules
    )
    page = live(drafts=[override])
    assert errors_of(page) == []
    assert "<i>over Lorem ipsum</i>" in page and "<b>main" not in page
    bad = [
        draft("stylesheet", name="layertest:new", prefixes=PREFIXES, rules=rules),
        django_component("scrud:x", "<i>x</i>"),
    ]
    page = live(drafts=bad)
    errors = errors_of(page)
    assert [(e["kind"], e["name"]) for e in errors] == [
        ("stylesheet", "layertest:new"),
        ("component", "scrud:x"),
    ]
    for found, item in zip(errors, bad, strict=True):
        content = {k: v for k, v in item["content"].items() if k != "name"}
        When.http.post(
            reverse("scrud-studio:save-item"),
            data={
                "kind": item["kind"],
                "branch": "wip",
                "name": item["content"]["name"],
                "content": content,
            },
            format="json",
        )
        Then.http.status_code_is(400)
        assert found["messages"] == [problem()["detail"]]
        assert item["content"]["name"] in found["messages"][0]
    # The preview renders without them: main's own presentation.
    assert "<b>main Lorem ipsum</b>" in page
    assert not Resource.objects.filter(content__branch="wip").exists()


@pytest.mark.django_db
@test_context
def test_draft_check_failing_as_a_server_error_is_logged(ui_alice_login, caplog):
    """A check that fails as a server error (an ``APIException`` of status
    500 or more) is listed as the draft's error, as any refusal is, and is
    also logged at ERROR with its traceback: it is no answer about the
    draft."""
    from rest_framework.exceptions import APIException

    from scrud_django.ui.views import UIComponentViewSet

    main_row()
    wip_for(ui_alice_login)
    with (
        mock.patch.object(
            UIComponentViewSet,
            "validate_content",
            side_effect=APIException("The check broke."),
        ),
        caplog.at_level(logging.ERROR, logger="scrud_django.ui.studio.drafts"),
    ):
        page = live(drafts=[django_component("row", "<i>{{ value.text }}</i>")])
    assert errors_of(page) == [
        {"kind": "component", "name": "row", "messages": ["The check broke."]}
    ]
    logged = [
        r
        for r in caplog.records
        if r.name == "scrud_django.ui.studio.drafts" and r.levelno == logging.ERROR
    ]
    assert len(logged) == 1 and logged[0].exc_info is not None
    assert "row" in logged[0].getMessage()
