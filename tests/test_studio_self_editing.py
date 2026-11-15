"""Editing the self-hosted studio with the studio, without locking anyone
out. The studio is its own published tree: the root ``studio`` (S, at
`STUDIO_ROOT`) and its working branch ``studio-wip`` (T). An override of a
studio item on T shows only in T's preview until it is pushed; a studio
item broken on S itself leaves safe mode, the studio links and the bootstrap
studio working, and the change can be undone from them. And the app's
(``main``'s) CSS and JS are not linked under the studio's prefix. These
tests serve the API at ``/api/`` and the bootstrap studio at ``/studio/``
(``tests.mounted_urls``), with the studio layer installed (the
``studio_layer`` fixture)."""

import json
import re
from urllib.parse import urlencode

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django import collection_type_uri_for
from scrud_django.models import Resource
from scrud_django.ui.branches import (
    UI_BRANCH_TYPE_URI,
    UI_BRANCH_VERSION_TYPE_URI,
    UI_CSS_TYPE_URI,
    UI_JS_TYPE_URI,
    UI_PUSH_TYPE_URI,
)
from scrud_django.ui.layers import layer_item
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import assign_role
from .steps import *  # noqa: F403, F401
from .test_studio_editors import content_of, root_frame
from .test_studio_live_preview import errors_of
from .test_studio_page import store_item
from .test_studio_promote import box_button, press, promote_boxes
from .test_studio_rollback_presentations import restore_buttons
from .test_studio_workbench import Page, workbench_url
from .test_ui_branch_css import css_hrefs, make_css
from .test_ui_branch_js import make_js, script_srcs
from .test_ui_branch_push import created, own_rows
from .test_ui_branch_types import make_branch
from .test_ui_branch_versions import versions_of
from .test_ui_http import HTML, grant

STUDIO_ROOT = "/workbench/"
STUDIO_CONTEXT = "ui-studio"
WIP_CONTEXT = "ui-studio-wip"
JSON = "application/json"
EDITOR_SCOPE = "studio studio-editor"
ITEMS_SCOPE = "studio studio-items"
READ_ONLY = "https://api.openteams.com/http-problem/ui-studio-branch-read-only"
# A Jinja template that compiles and raises when it renders.
RAISES = "{{ value.name.upper().nope() }}"
PAGE_FAILED = '<!-- scrud-ui: component "studio:page" failed -->'
FAILED = '<!-- scrud-ui: component "{}" failed -->'
NAV = '<nav class="scrud-studio-links">'
ITEM_TYPES = [
    uri
    for type_uri in (
        UI_COMPONENT_TYPE_URI,
        UI_STYLESHEET_TYPE_URI,
        UI_CSS_TYPE_URI,
        UI_JS_TYPE_URI,
    )
    for uri in (type_uri, collection_type_uri_for(type_uri))
]
PUSHES = [UI_PUSH_TYPE_URI, collection_type_uri_for(UI_PUSH_TYPE_URI)]
BRANCHES = [
    uri
    for type_uri in (UI_BRANCH_TYPE_URI, UI_BRANCH_VERSION_TYPE_URI)
    for uri in (type_uri, collection_type_uri_for(type_uri))
]


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"
    settings.SCRUD_UI_STUDIO_BRANCH = "studio"


def studio_tree():
    """The studio's tree, as the demo seeds it: the root ``studio``
    published at `STUDIO_ROOT` (in `STUDIO_CONTEXT`) with its own component
    ``studio-note``, and its working branch ``studio-wip`` (in
    `WIP_CONTEXT`). Returns ``(studio, studio_wip, author)``: the author
    has the demo author's grants in the tree, which are the ones a push
    into the root needs: GET on the root and its versions, the item writes
    and ``ui-pushes`` there; everything on the working branch."""
    studio = make_branch("studio", STUDIO_CONTEXT, parent=None, prefix=STUDIO_ROOT)
    store_item(
        "ui-components",
        "studio",
        STUDIO_CONTEXT,
        name="studio-note",
        engine="jinja2",
        mode="view",
        template="<i>note</i>",
    )
    studio_wip = make_branch("studio-wip", WIP_CONTEXT, parent="studio")
    author = assign_role(
        {
            "http.GET": [*BRANCHES, *ITEM_TYPES, *PUSHES],
            "http.POST": [*ITEM_TYPES, *PUSHES],
            "http.PUT": ITEM_TYPES,
            "http.DELETE": ITEM_TYPES,
        },
        STUDIO_CONTEXT,
    )
    grant(
        author,
        {
            f"http.{method}": [*BRANCHES, *ITEM_TYPES, *PUSHES]
            for method in ("GET", "POST", "PUT", "DELETE")
        },
        WIP_CONTEXT,
    )
    Given.http.force_authenticate(author)
    return studio, studio_wip, author


def packaged(name):
    """The studio layer's component `name`, without its name."""
    _etag, content = layer_item("component", (name,))
    return {k: v for k, v in content.items() if k != "name"}


def override(name, branch, template):
    """POST `branch`'s override of the layer component `name` with
    `template`, through the API, as the signed-in user (the server sets its
    ``base``); returns the stored row."""
    context = {"studio": STUDIO_CONTEXT, "studio-wip": WIP_CONTEXT}[branch]
    When.http.post(
        reverse("ui-components-list"),
        data={
            **packaged(name),
            "name": name,
            "template": template,
            "branch": branch,
            "rbac_context": context,
        },
        format="json",
    )
    Then.http.status_code_is(201)
    return created()


def get_text(url, status=200, **headers):
    When.http.get(url, HTTP_ACCEPT=HTML, **headers)
    Then.http.status_code_is(status)
    return Http.response.content.decode()


def mounted(path):
    """An API path (``/api/...``) under the studio root's prefix."""
    assert path.startswith("/api/"), path
    return STUDIO_ROOT + path[len("/api/") :]


def stylesheet_editor(href):
    """What studio.js loads into the Editor pane for a stylesheet: its
    `href` as an htmx fragment in the editor's scope, in view mode."""
    return get_text(
        href,
        HTTP_HX_REQUEST="true",
        HTTP_SCRUD_UI_SCOPE=EDITOR_SCOPE,
        HTTP_SCRUD_UI_MODE="view",
    )


def live_preview(instance, drafts=()):
    """The workbench's live preview of the ``ui-stylesheets`` resource
    `instance` on ``studio-wip`` (T), in the editor's scope, in view mode,
    with `drafts`; returns the preview document."""
    request = {
        "type": "ui-stylesheets",
        "branch": "studio-wip",
        "mode": "view",
        "scope": EDITOR_SCOPE.split(),
        "context": "",
        "instance": instance.pk,
        "drafts": list(drafts),
        "mark": None,
        "after_push": None,
    }
    When.http.post(
        reverse("scrud-studio:live-preview"),
        data=urlencode({"request": json.dumps(request)}),
        content_type="application/x-www-form-urlencoded",
    )
    Then.http.status_code_is(200)
    return Http.response.content.decode()


def theme_stylesheet():
    """The studio root's own stylesheet ``theme``, with one rule: the
    resource the preview shows (T inherits it)."""
    return store_item(
        "ui-stylesheets",
        "studio",
        STUDIO_CONTEXT,
        name="theme",
        rules=[{"select": "array", "component": "todo-row"}],
    )


ROW_OVERRIDE = (
    '<span class="studio-rule-row wip-row">WIP {{ value.get("select") }}'
    ' -- {{ value.get("component") }}</span>'
)


@pytest.mark.django_db
@test_context
def test_override_studio_component_on_studio_wip_changes_only_its_preview(
    studio_layer,
):
    _studio, studio_wip, _author = studio_tree()
    theme = theme_stylesheet()
    # Before: the packaged rule row, in the preview and in S's editor.
    page = live_preview(theme)
    assert '<code class="studio-rule-select">array</code>' in page
    assert "wip-row" not in page
    # The author overrides the rule editor's view row on T, studio-wip.
    override("studio:rule-row-view", "studio-wip", ROW_OVERRIDE)
    # T's preview of a stylesheet shows the override.
    page = live_preview(theme)
    assert '<span class="studio-rule-row wip-row">WIP array -- todo-row</span>' in page
    assert '<code class="studio-rule-select">' not in page
    # S, the studio root, is unchanged: it has no override, and the
    # studio at its prefix renders the stylesheet with the packaged row.
    assert [c["name"] for c in own_rows(UI_COMPONENT_TYPE_URI, "studio")] == [
        "studio-note"
    ]
    editor = stylesheet_editor(
        mounted(reverse("ui-stylesheets-detail", args=[theme.pk]))
    )
    assert '<code class="studio-rule-select">array</code>' in editor
    assert "wip-row" not in editor
    # T's workbench, at S's prefix, lists the override as T's own item (T's
    # data), and renders with S's components.
    workbench = Page(get_text(workbench_url(studio_wip, STUDIO_ROOT)))
    classes, _open, _li = workbench.entries()[("component", "studio:rule-row-view")]
    assert "studio-own" in classes
    # A broken edit on T breaks only T's preview: a draft that raises falls
    # back there, and is never stored; S's editor is untouched.
    draft = {
        "kind": "component",
        "content": {
            **packaged("studio:rule-row-view"),
            "name": "studio:rule-row-view",
            "template": RAISES,
        },
    }
    page = live_preview(theme, drafts=[draft])
    assert errors_of(page) == []
    assert FAILED.format("studio:rule-row-view") in page
    [stored] = [
        c
        for c in own_rows(UI_COMPONENT_TYPE_URI, "studio-wip")
        if c["name"] == "studio:rule-row-view"
    ]
    assert stored["template"] == ROW_OVERRIDE
    editor = stylesheet_editor(
        mounted(reverse("ui-stylesheets-detail", args=[theme.pk]))
    )
    assert FAILED.format("studio:rule-row-view") not in editor
    assert '<code class="studio-rule-select">array</code>' in editor


@pytest.mark.django_db
@test_context
def test_push_studio_override_promotes_it(studio_layer):
    _studio, studio_wip, _author = studio_tree()
    theme = theme_stylesheet()
    override("studio:rule-row-view", "studio-wip", ROW_OVERRIDE)
    editor_href = mounted(reverse("ui-stylesheets-detail", args=[theme.pk]))
    assert "wip-row" not in stylesheet_editor(editor_href)
    # Promote, from T's workbench at the studio's prefix: the review shows
    # the change, and Apply pushes studio-wip into studio.
    page = Page(get_text(workbench_url(studio_wip, STUDIO_ROOT)))
    box = promote_boxes(page)["studio"]
    review = Page(
        get_text(
            box.attrs["data-studio-promote-src"] + "?target=studio",
            HTTP_HX_REQUEST="true",
            HTTP_SCRUD_UI_SCOPE="studio studio-push",
        )
    )
    [preview] = review.having("data-studio-push-preview")
    assert preview.attrs["data-studio-changes"] == "1"
    assert preview.attrs["data-studio-conflicts"] == "0"
    before = versions_of("studio")[-1]
    press(box_button(page, box, "studio-apply"), 201)
    push = created()
    assert push.content["source"] == "studio-wip"
    assert push.content["target"] == "studio"
    assert push.content["result"] == {"version": before + 1}
    # S now holds the override, and T is emptied.
    [row] = [
        c
        for c in own_rows(UI_COMPONENT_TYPE_URI, "studio")
        if c["name"] == "studio:rule-row-view"
    ]
    assert row["template"] == ROW_OVERRIDE
    assert own_rows(UI_COMPONENT_TYPE_URI, "studio-wip") == []
    # The studio under /workbench/ uses it: the stylesheet's editor, as the
    # workbench (of any branch) loads it into its Editor pane at S's prefix.
    editor = stylesheet_editor(editor_href)
    assert (
        '<span class="studio-rule-row wip-row">WIP array -- todo-row</span>' in editor
    )
    assert '<code class="studio-rule-select">' not in editor
    # Outside the studio's tree (main's, at /api/) the packaged row stays.
    editor = stylesheet_editor(reverse("ui-stylesheets-detail", args=[theme.pk]))
    assert '<code class="studio-rule-select">array</code>' in editor
    assert "wip-row" not in editor


def studio_links(page):
    """``{text: href}`` of the page's studio links."""
    found = re.search(f"{NAV}(.*?)</nav>", page, re.S)
    assert found, page
    return {
        text: href.replace("&amp;", "&")
        for href, text in re.findall(r'<a href="([^"]*)">([^<]*)</a>', found.group(1))
    }


@pytest.mark.django_db
@test_context
def test_broken_studio_page_on_S_recoverable(studio_layer):
    """Review Focus 1: an author breaks the studio's page, workbench and
    items components on the published studio root S. The studio links,
    safe mode and the bootstrap studio still work, and the change is undone
    from them, with no database access and no hand-made API call."""
    studio, studio_wip, _author = studio_tree()
    good = versions_of("studio")[-1]
    # The break: overrides on S that raise when they render (a direct write
    # by an author with the grants, which the API allows).
    for name in ("studio:page", "studio:workbench", "studio:items"):
        override(name, "studio", RAISES)
    assert versions_of("studio")[-1] == good + 3
    url = workbench_url(studio_wip, STUDIO_ROOT)
    # The page falls back to the built-in page, which carries the studio
    # links; the workbench itself is broken.
    page = get_text(url)
    assert page.startswith(PAGE_FAILED)
    assert FAILED.format("studio:workbench") in page
    assert not Page(page).having("data-studio-workbench")
    links = studio_links(page)
    assert list(links) == ["Studio", "Safe mode", "Bootstrap studio"]
    assert links["Studio"] == workbench_url(studio, STUDIO_ROOT)
    assert links["Safe mode"] == url + "&ui-safe=1"
    assert links["Bootstrap studio"] == reverse("scrud-studio:types")
    # Safe mode: the full workbench from the layer, panes included, on the
    # layer's page.
    safe = get_text(links["Safe mode"])
    assert PAGE_FAILED not in safe and "scrud-ui: component" not in safe
    assert '<div class="studio" data-studio><main>' in safe
    assert 'class="scrud-safe-mode"' in safe
    page = Page(safe)
    [workbench] = page.having("data-studio-workbench")
    assert workbench.attrs["data-studio-branch"] == "studio-wip"
    panes = [p.attrs["data-studio-pane"] for p in page.having("data-studio-pane")]
    assert panes == ["items", "editor", "preview"]
    assert ("component", "studio-note") in page.entries()
    assert page.having("data-studio-status") and page.having("data-studio-preview-form")
    # The panes' refreshes (an htmx GET of data-studio-src in the pane's
    # scope) carry Scrud-UI-Safe: 1 on a safe page: the layer's pane.
    [items] = [
        e
        for e in page.having("data-studio-embed")
        if e.attrs["data-studio-embed"] == "items"
    ]
    refresh = {
        "HTTP_HX_REQUEST": "true",
        "HTTP_SCRUD_UI_SCOPE": items.attrs["data-studio-scope"],
    }
    assert items.attrs["data-studio-scope"] == ITEMS_SCOPE
    pane = get_text(items.attrs["data-studio-src"], HTTP_SCRUD_UI_SAFE="1", **refresh)
    assert "scrud-ui: component" not in pane
    assert ("component", "studio:items") in Page(pane).entries()
    # Without the header, the stored (broken) pane.
    pane = get_text(items.attrs["data-studio-src"], **refresh)
    assert FAILED.format("studio:items") in pane
    # The bootstrap studio still works: its pages are not UI-type resources.
    types = get_text(reverse("scrud-studio:types"))
    type_page = reverse("scrud-studio:type", args=["ui-stylesheets"])
    assert f'href="{type_page}"' in types
    assert 'value="studio-wip"' in get_text(type_page + "?branch=studio-wip")
    # Its strip on S lists S's own items, the broken overrides among them,
    # for its editor to open and revert.
    strip = get_text(type_page + "?branch=studio")
    for name in ("studio:page", "studio:workbench", "studio:items"):
        assert f'data-kind="component" data-name="{name}"' in strip
    # It saves nothing on S, a published branch (changes reach it by a
    # push) ...
    When.http.post(
        reverse("scrud-studio:save-item"),
        data={
            "kind": "component",
            "name": "studio:workbench",
            "branch": "studio",
            "content": packaged("studio:workbench"),
        },
        format="json",
    )
    Then.http.status_code_is(400)
    assert Http.response.json()["type"] == READ_ONLY
    # ... so it repairs S through the working branch: save a working
    # workbench on studio-wip, and push it.
    When.http.post(
        reverse("scrud-studio:save-item"),
        data={
            "kind": "component",
            "name": "studio:workbench",
            "branch": "studio-wip",
            "content": packaged("studio:workbench"),
        },
        format="json",
    )
    Then.http.status_code_is(201)
    When.http.post(
        reverse("scrud-studio:push"), data={"branch": "studio-wip"}, format="json"
    )
    Then.http.status_code_is(201)
    page = get_text(url)
    assert page.startswith(PAGE_FAILED)  # studio:page is still broken
    assert FAILED.format("studio:workbench") not in page
    assert Page(page).having("data-studio-workbench")
    # The bootstrap studio reverts S's broken page override: its revert
    # (with the ETag its item read gives) drops S's own row, and the
    # workbench renders on the layer's studio:page again.
    page_item = {"kind": "component", "name": "studio:page", "branch": "studio"}
    When.http.get(
        reverse("scrud-studio:item") + "?" + urlencode(page_item), HTTP_ACCEPT=JSON
    )
    Then.http.status_code_is(200)
    assert Http.response.json()["own"] is True
    etag = Http.response["ETag"]
    # Without If-Match it deletes nothing (the view's 428).
    When.http.post(reverse("scrud-studio:revert-item"), data=page_item, format="json")
    Then.http.status_code_is(428)
    assert "studio:page" in [
        c["name"] for c in own_rows(UI_COMPONENT_TYPE_URI, "studio")
    ]
    When.http.post(
        reverse("scrud-studio:revert-item"),
        data=page_item,
        format="json",
        HTTP_IF_MATCH=etag,
    )
    Then.http.status_code_is(204)
    assert "studio:page" not in [
        c["name"] for c in own_rows(UI_COMPONENT_TYPE_URI, "studio")
    ]
    page = get_text(url)
    assert PAGE_FAILED not in page and "scrud-ui: page" not in page
    assert '<div class="studio" data-studio><main>' in page
    assert Page(page).having("data-studio-workbench")
    assert list(studio_links(page)) == ["Studio", "Safe mode", "Bootstrap studio"]
    # The item S shows now is the layer's.
    When.http.get(
        reverse("scrud-studio:item") + "?" + urlencode(page_item), HTTP_ACCEPT=JSON
    )
    Then.http.status_code_is(200)
    assert Http.response.json()["own"] is False
    # The page is broken again, so that the restore below has a broken
    # studio:page to repair, as it had before the bootstrap revert.
    override("studio:page", "studio", RAISES)
    assert get_text(url).startswith(PAGE_FAILED)
    # From S's own workbench in safe mode, the history rolls S back: a
    # restore of the version before the break removes the overrides.
    safe = Page(get_text(studio_links(get_text(links["Studio"]))["Safe mode"]))
    assert safe.one("data-studio-workbench").attrs["data-studio-published"] == "true"
    [restore] = [
        b
        for b in restore_buttons(safe)
        if json.loads(b.attrs["data-scrud-body"])["restore"] == good
    ]
    press(restore, 201)
    assert [c["name"] for c in own_rows(UI_COMPONENT_TYPE_URI, "studio")] == [
        "studio-note"
    ]
    # The studio is the packaged one again.
    page = get_text(url)
    assert "scrud-ui: component" not in page and "scrud-ui: page" not in page
    assert '<div class="studio" data-studio><main>' in page
    assert ("component", "studio-note") in Page(page).entries()


@pytest.mark.django_db
@test_context
def test_broken_override_deleted_from_the_items_pages(studio_layer):
    """The way back that needs no earlier version of S (when the break was
    S's first change, there is none to restore): the UI items' own pages,
    under S's prefix, are not in the scope ``studio``. The list of
    components shows each as its own frame, with the runtime's Delete for a
    user who may DELETE it; so does the override's own page (here in safe
    mode, from its Safe mode link)."""
    _studio, studio_wip, _author = studio_tree()
    url = workbench_url(studio_wip, STUDIO_ROOT)
    for broken_name in ("studio:workbench", "studio:items"):
        override(broken_name, "studio", RAISES)
    assert FAILED.format("studio:workbench") in get_text(url)
    [workbench_pk, items_pk] = [
        r.pk
        for r in sorted(
            Resource.objects.filter(resource_type__type_uri=UI_COMPONENT_TYPE_URI),
            key=lambda r: r.pk,
        )
        if r.content["name"] in ("studio:workbench", "studio:items")
    ]
    # From the list, under S's prefix: the frames' URLs are absolute.
    listed = Page(get_text(mounted(reverse("ui-components-list"))))
    href = "http://testserver" + mounted(
        reverse("ui-components-detail", args=[workbench_pk])
    )
    frame = root_frame(listed, href)
    assert content_of(listed, frame)["template"] == RAISES
    [delete] = [e for e in listed.elements if e.attrs.get("hx-delete") == href]
    assert delete.within(frame)
    delete_with_frame_etag(delete, frame)
    # From the override's own page, in safe mode.
    href = mounted(reverse("ui-components-detail", args=[items_pk]))
    safe = Page(get_text(studio_links(get_text(href))["Safe mode"]))
    frame = root_frame(safe, href)
    [delete] = [e for e in safe.elements if e.attrs.get("hx-delete") == href]
    delete_with_frame_etag(delete, frame)
    assert [c["name"] for c in own_rows(UI_COMPONENT_TYPE_URI, "studio")] == [
        "studio-note"
    ]
    page = get_text(url)
    assert "scrud-ui: component" not in page
    assert Page(page).having("data-studio-workbench")


def delete_with_frame_etag(button, frame):
    """Send a frame's Delete `button` as scrud-ui.js does: with the frame's
    ``If-Match``."""
    When.http.delete(
        button.attrs["hx-delete"],
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
        HTTP_IF_MATCH=frame.attrs["data-scrud-etag"],
    )
    Then.http.status_code_is(204)


@pytest.mark.django_db
@test_context
def test_app_css_and_js_not_under_studio_prefix(studio_layer):
    """The studio is its own tree: main's CSS and JS items apply to the
    app's pages, not to the studio's (spec: "the app's CSS and JS don't
    load under the studio's prefix"); the layer's do, on both."""
    studio, studio_wip, author = studio_tree()
    site = make_css("site", "p { color: red }")
    app = make_js("app", "console.log('app')")
    grant(author, {"http.GET": [UI_BRANCH_TYPE_URI, UI_COMPONENT_TYPE_URI]}, "")
    # The app's pages, at /api/, link main's items and the layer's.
    page = get_text(reverse("ui-components-list"))
    assert any(f"/ui-css-files/site/{site.etag}" in h for h in css_hrefs(page))
    assert any(f"/ui-js-files/app/{app.etag}" in s for s in script_srcs(page))
    assert any("/ui-css-files/studio:studio/" in h for h in css_hrefs(page))
    # The studio's, under /workbench/: the layer's only.
    for branch in (studio_wip, studio):
        page = get_text(workbench_url(branch, STUDIO_ROOT))
        assert Page(page).having("data-studio-workbench")
        hrefs, srcs = css_hrefs(page), script_srcs(page)
        assert hrefs and all(h.startswith(STUDIO_ROOT) for h in hrefs), hrefs
        assert not any("/site/" in h for h in hrefs), hrefs
        assert not any("/app/" in s for s in srcs), srcs
        assert any("/ui-js-files/studio:studio/" in s for s in srcs), srcs
    # A workbench of main itself, shown by the studio (T is main), still
    # renders on S: main's items are T's data, not the studio's page.
    main = make_branch("main", "", parent=None)
    page = get_text(workbench_url(main, STUDIO_ROOT))
    assert Page(page).having("data-studio-workbench")
    assert not any("/site/" in h for h in css_hrefs(page))
    assert not any("/app/" in s for s in script_srcs(page))
