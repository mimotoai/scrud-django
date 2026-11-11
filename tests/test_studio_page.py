"""The studio page of a type: the branch picker and preview controls, the
sandboxed preview, the inspector and editor shells that ``studio.js`` fills,
and the branch strip; and the two reads it adds, the strip partial and the
editor's item. These tests serve the API at ``/api/`` and the studio at
``/studio/`` (``tests.mounted_urls``)."""

import re
from html import unescape

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.registration import ResourceRegistration
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI, UI_CSS_TYPE_URI
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_css import make_css
from .test_ui_branch_push import quoted
from .test_ui_branch_types import make_branch, problem
from .test_ui_branch_versions import versions_of
from .test_ui_http import grant, html, make_component

SLUG = "todo-lists"
UNAVAILABLE = "https://api.openteams.com/http-problem/ui-branch-unavailable"
CONFLICT = {"kind": "component", "key": ["row"], "base": "old", "current": "new"}


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def store_item(slug, branch, context, **content):
    """An item of type `slug` stored as is on `branch`."""
    reverse(f"{slug}-list")
    return ResourceRegistration.register(
        {**content, "branch": branch, "rbac_context": context}, slug
    )


def setup_branches(user):
    """main has the components ``row`` and ``head`` and the CSS ``base``;
    ``shop`` is published at ``/shop/``; ``wip`` (in "alice", over main)
    overrides ``row``, adds the CSS ``site`` and lists a conflict on
    ``row``; ``bobs`` is in "bob". `user` may GET ``shop``, components in
    "" and CSS in "alice"; not ``bobs``, nor CSS in ""."""
    make_component("row", "<b>main</b>")
    make_component("head", "<h1>main</h1>")
    make_css("base", "p { color: red }")
    make_branch("shop", "project-a", prefix="/shop/")
    make_branch(
        "wip",
        "alice",
        conflicts=[CONFLICT],
        branching_point={"main": versions_of("main")[-1]},
    )
    make_branch("bobs", "bob")
    row = store_item(
        "ui-components",
        "wip",
        "alice",
        name="row",
        engine="jinja2",
        mode="view",
        template="<i>{{ value }}</i>",
    )
    store_item("ui-css", "wip", "alice", name="site", css="p { color: green }")
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI]}, "project-a")
    grant(user, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "")
    grant(user, {"http.GET": [UI_CSS_TYPE_URI]}, "alice")
    return row


def page(status=200, slug=SLUG, **params):
    reverse("todo-lists-list")  # the URLconf registers the types
    When.http.get(reverse("scrud-studio:type", args=[slug]), params)
    Then.http.status_code_is(status)
    return html()


def attr(page, name):
    """The value of the one `name` attribute of `page`, unescaped."""
    found = re.findall(rf'\s{name}="([^"]*)"', page)
    assert len(found) == 1, (name, found)
    return unescape(found[0])


def options(page):
    """``{value: (label, read-only)}`` of the branch picker's options."""
    picker = re.search(r'<select name="branch"[^>]*>(.*?)</select>', page, re.S)
    found = {}
    for tag, label in re.findall(r"(<option[^>]*>)([^<]*)</option>", picker.group(1)):
        value = unescape(re.search(r'value="([^"]*)"', tag).group(1))
        found[value] = (label.strip(), "data-read-only" in tag, "selected" in tag)
    return found


def section(page, ident):
    """The inner HTML of the element with id `ident` (nested elements of its
    tag balanced)."""
    match = re.search(rf'<(\w+)[^>]*\sid="{ident}"[^>]*>', page)
    assert match, ident
    tag, depth, at = match.group(1), 1, match.end()
    for found in re.finditer(rf"<(/?){tag}\b[^>]*>", page[at:]):
        depth += -1 if found.group(1) else 1
        if depth == 0:
            return page[at : at + found.start()]
    raise AssertionError(f"#{ident} is not closed")


def strip_text(fragment):
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


@pytest.mark.django_db
@test_context
def test_type_page_wires_endpoints_and_sandbox(ui_alice_login):
    setup_branches(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    shown = page(branch="wip")
    # The preview: exactly one iframe, sandboxed with no allow-scripts.
    (iframe,) = re.findall(r"<iframe[^>]*>", shown)
    assert re.findall(r'\ssandbox="([^"]*)"', iframe) == ["allow-same-origin"]
    preview_url = reverse("scrud-studio:preview", args=[SLUG]) + "?branch=wip&mode=view"
    assert unescape(re.search(r'\ssrc="([^"]*)"', iframe).group(1)) == preview_url
    # Every endpoint reaches studio.js as a data- attribute.
    for name, url in (
        ("explain", reverse("scrud-studio:explain", args=[SLUG])),
        ("options", reverse("scrud-studio:new-component-options", args=[SLUG])),
        ("new-component", reverse("scrud-studio:new-component")),
        ("save", reverse("scrud-studio:save-item")),
        ("revert", reverse("scrud-studio:revert-item")),
        ("pull", reverse("scrud-studio:pull")),
        ("push", reverse("scrud-studio:push")),
        ("item", reverse("scrud-studio:item")),
        ("preview", preview_url),
    ):
        assert attr(shown, f"data-{name}-url") == url, name
    assert attr(shown, "data-branch") == "wip"
    assert attr(shown, "data-query") == "branch=wip&mode=view"
    assert attr(shown, "data-slug") == SLUG
    assert attr(shown, "data-editable") == "true"
    # The picker lists every branch alice may GET; published ones read-only.
    picked = options(shown)
    assert set(picked) == {"main", "shop", "wip"}
    assert picked["main"][1] and picked["shop"][1] and not picked["wip"][1]
    assert "read-only" in picked["main"][0] and "read-only" in picked["shop"][0]
    assert "read-only" not in picked["wip"][0]
    assert picked["wip"][2] and not picked["main"][2]
    # The preview controls.
    for name in ("pinned", "mode", "scope", "context", "instance"):
        assert re.search(rf'<(input|select)[^>]*\sname="{name}"', shown), name
    # No inline script and no inline handler (CSP script-src 'self').
    assert not re.search(r"<script(?![^>]*\ssrc=)", shown)
    assert not re.search(r"<[^>]+\son[a-z]+\s*=", shown, re.I)


@pytest.mark.django_db
@test_context
def test_type_page_controls_reach_the_preview_and_default_to_a_working_branch(
    ui_alice_login,
):
    setup_branches(ui_alice_login)
    grant(ui_alice_login, {"http.GET": [UI_STYLESHEET_TYPE_URI]}, "acme")
    Given.http.force_authenticate(ui_alice_login)
    # Without ?branch=, the first working branch alice may select.
    assert attr(page(), "data-branch") == "wip"
    shown = page(
        branch="wip",
        pinned="1",
        mode="edit",
        scope=" sidebar  card ",
        context="acme",
        instance="12",
    )
    query = "branch=wip%40pinned&mode=edit&scope=sidebar+card&context=acme&instance=12"
    assert attr(shown, "data-query") == query
    assert attr(shown, "data-branch") == "wip"  # writes go to the live branch
    assert attr(shown, "data-preview-url").endswith("?" + query)
    # A pinned snapshot that is missing: live, with a notice.
    make_branch("gap", "alice", branching_point={"main": 999})
    shown = page(branch="gap", pinned="1")
    assert attr(shown, "data-query") == "branch=gap&mode=view"
    assert "pinned" in strip_text(section(shown, "studio-notices"))
    # main has no parent to pin to; an unknown mode is view.
    shown = page(branch="main", pinned="1", mode="bogus")
    assert attr(shown, "data-query") == "branch=main&mode=view"
    assert attr(shown, "data-editable") == "false"


@pytest.mark.django_db
@test_context
def test_unavailable_branch_or_context_shows_a_notice(ui_alice_login):
    setup_branches(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    notices = {}
    for value in ("bobs", "nope"):
        shown = page(branch=value)
        assert attr(shown, "data-branch") == "main"
        notices[value] = strip_text(section(shown, "studio-notices"))
    assert "not available" in notices["nope"]
    assert notices["bobs"].replace("bobs", "nope") == notices["nope"]
    # A data context whose stylesheets alice may not read is dropped.
    shown = page(branch="wip", context="acme")
    assert attr(shown, "data-query") == "branch=wip&mode=view"
    assert "acme" in strip_text(section(shown, "studio-notices"))


@pytest.mark.django_db
@test_context
def test_strip_shows_changes_conflicts_css_and_browse_link(ui_alice_login):
    setup_branches(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    shown = page(branch="wip")
    strip = section(shown, "studio-strip-body")
    text = strip_text(strip)
    assert "wip" in text and "main" in text  # the branch, parent and root
    # Changed items: wip's own readable rows (alice may not read CSS in "").
    changes = section(shown, "studio-changes")
    assert re.search(r'data-kind="component"[^>]*data-name="row"', changes)
    assert re.search(r'data-kind="css"[^>]*data-name="site"', changes)
    assert "base" not in changes
    conflicts = section(shown, "studio-conflicts")
    assert re.search(r'data-kind="component"[^>]*data-name="row"', conflicts)
    # The branch's CSS items alice may read, effective on wip.
    css = section(shown, "studio-css")
    assert 'data-name="site"' in css and 'data-name="base"' not in css
    # Pull, Push to the parent, and the app on this branch.
    assert re.search(r'<button[^>]*id="studio-pull"', strip)
    assert "main" in strip_text(
        re.search(r'<button[^>]*id="studio-push"[^>]*>[^<]*', strip).group(0)
    )
    browse = re.search(r'<a[^>]*id="studio-browse"[^>]*href="([^"]*)"', strip)
    assert unescape(browse.group(1)) == reverse("todo-lists-list") + "?ui-branch=wip"
    # The strip refreshes itself on a save, from its own partial.
    url = attr(shown, "hx-get")
    assert url == reverse("scrud-studio:strip", args=[SLUG]) + "?branch=wip"
    When.http.get(url, HTTP_HX_REQUEST="true")
    Then.http.status_code_is(200)
    assert section(html(), "studio-strip-body") == strip
    When.http.get(reverse("scrud-studio:strip", args=[SLUG]), {"branch": "bobs"})
    Then.http.status_code_is(400)
    assert problem()["type"] == UNAVAILABLE


@pytest.mark.django_db
@test_context
def test_strip_hides_own_rows_the_user_may_not_read(ui_alice_login):
    """wip's own rows that alice may not read (a component stored in "bob",
    CSS in "") are not listed as changes; readable, they are."""
    setup_branches(ui_alice_login)
    store_item(
        "ui-components",
        "wip",
        "bob",
        name="secret",
        engine="jinja2",
        mode="view",
        template="<s>{{ value }}</s>",
    )
    store_item("ui-css", "wip", "", name="hidden", css="p { color: blue }")
    Given.http.force_authenticate(ui_alice_login)
    changes = section(page(branch="wip"), "studio-changes")
    assert re.search(r'data-kind="component"[^>]*data-name="row"', changes)
    assert 'data-name="secret"' not in changes and "secret" not in changes
    assert 'data-name="hidden"' not in changes and "hidden" not in changes
    # Readable, they are listed.
    grant(ui_alice_login, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "bob")
    grant(ui_alice_login, {"http.GET": [UI_CSS_TYPE_URI]}, "")
    changes = section(page(branch="wip"), "studio-changes")
    assert re.search(r'data-kind="component"[^>]*data-name="secret"', changes)
    assert re.search(r'data-kind="css"[^>]*data-name="hidden"', changes)


@pytest.mark.django_db
@test_context
def test_published_branch_is_read_only_and_browses_under_its_prefix(ui_alice_login):
    setup_branches(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    shown = page(branch="shop")
    assert attr(shown, "data-editable") == "false"
    strip = section(shown, "studio-strip-body")
    assert "read-only" in strip_text(strip)
    assert "studio-pull" not in strip and "studio-push" not in strip
    browse = re.search(r'<a[^>]*id="studio-browse"[^>]*href="([^"]*)"', strip)
    assert unescape(browse.group(1)) == "/shop/todo-lists/?ui-branch=shop"
    # The new component form is disabled there.
    assert re.search(r'<fieldset[^>]*\sdisabled', section(shown, "studio-inspector"))


@pytest.mark.django_db
@test_context
def test_published_branch_lists_its_own_items_to_revert(ui_alice_login):
    """A published branch's own rows are listed in its strip, as a working
    branch's are, so that the editor opens them and offers Revert; the
    strip says so. Inherited items are not listed."""
    setup_branches(ui_alice_login)
    store_item(
        "ui-components",
        "shop",
        "project-a",
        name="row",
        engine="jinja2",
        mode="view",
        template="<u>{{ value }}</u>",
    )
    grant(ui_alice_login, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "project-a")
    Given.http.force_authenticate(ui_alice_login)
    shown = page(branch="shop")
    assert attr(shown, "data-editable") == "false"
    changes = section(shown, "studio-changes")
    assert re.search(r'data-kind="component"[^>]*data-name="row"', changes)
    assert 'data-name="head"' not in changes  # main's, inherited
    text = strip_text(section(shown, "studio-strip-body"))
    assert "read-only" in text and "revert" in text.lower()
    # The editor has its Revert control (studio.js shows it for an own item).
    assert re.search(r'<button[^>]*id="studio-revert"', section(shown, "studio-editor"))


@pytest.mark.django_db
@test_context
def test_item_says_what_a_revert_shows(studio_layer, ui_alice_login):
    """``reverts_to`` of the editor's item: what the branch shows once its
    own row is deleted, ``inherited`` (its parent's) or ``packaged`` (a
    layer's, on a root), when the user may read it; ``null`` when nothing
    is beneath, when what is beneath is unreadable, and for an item the
    branch does not own. The editor labels its Revert or Delete with it."""
    setup_branches(ui_alice_login)
    # wip's own CSS over main's "base", which alice may not read (CSS in "").
    store_item("ui-css", "wip", "alice", name="base", css="p { color: blue }")
    # main's own override of a packaged studio layer component.
    store_item(
        "ui-components",
        "main",
        "",
        name="studio:items",
        engine="jinja2",
        mode="view",
        template="<i>main's</i>",
    )
    Given.http.force_authenticate(ui_alice_login)
    url = reverse("scrud-studio:item")

    def reverts_to(branch, kind, name):
        When.http.get(url, {"branch": branch, "kind": kind, "name": name})
        Then.http.status_code_is(200)
        return Http.response.json()["own"], Http.response.json()["reverts_to"]

    assert reverts_to("wip", "component", "row") == (True, "inherited")
    assert reverts_to("main", "component", "studio:items") == (True, "packaged")
    assert reverts_to("wip", "css", "site") == (True, None)  # nothing beneath
    assert reverts_to("wip", "css", "base") == (True, None)  # unreadable beneath
    assert reverts_to("wip", "component", "head") == (False, None)  # inherited
    assert reverts_to("main", "component", "head") == (True, None)  # main's own


@pytest.mark.django_db
@test_context
def test_item_reads_the_effective_item_the_user_may_read(ui_alice_login):
    row = setup_branches(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    url = reverse("scrud-studio:item")

    When.http.get(url, {"branch": "wip", "kind": "component", "name": "row"})
    Then.http.status_code_is(200)
    body = Http.response.json()
    assert body["own"] is True and body["from"] == "wip"
    assert body["etag"] == quoted(row.etag) == Http.response["ETag"]
    assert body["content"]["template"] == "<i>{{ value }}</i>"
    # Inherited from main.
    When.http.get(url, {"branch": "wip", "kind": "component", "name": "head"})
    Then.http.status_code_is(200)
    body = Http.response.json()
    assert body["own"] is False and body["from"] == "main"
    assert body["content"]["template"] == "<h1>main</h1>"
    # Unreadable (CSS in "") and absent are the same 404.
    When.http.get(url, {"branch": "wip", "kind": "css", "name": "base"})
    Then.http.status_code_is(404)
    unreadable = problem()
    When.http.get(url, {"branch": "wip", "kind": "css", "name": "nothing"})
    Then.http.status_code_is(404)
    assert problem() == unreadable
    When.http.get(url, {"branch": "wip", "kind": "css", "name": "site"})
    Then.http.status_code_is(200)
    assert Http.response.json()["content"]["css"] == "p { color: green }"
    # A branch alice may not select is refused as an unknown one.
    When.http.get(url, {"branch": "bobs", "kind": "component", "name": "row"})
    Then.http.status_code_is(400)
    assert problem()["type"] == UNAVAILABLE
    When.http.get(url, {"branch": "wip", "kind": "bogus", "name": "row"})
    Then.http.status_code_is(400)


@pytest.mark.django_db
@test_context
def test_studio_page_needs_login():
    reverse("todo-lists-list")
    for url in (
        reverse("scrud-studio:item"),
        reverse("scrud-studio:strip", args=[SLUG]),
    ):
        When.http.get(url)
        Then.http.status_code_is(401)
