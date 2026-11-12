"""The self-hosted studio's workbench: the root of a ``UIBranch`` (the branch
T) in the scope ``studio``, requested in view mode, with the items tree
(``studio:items``) and the branch strip (``studio:status``) embedded from
T's studio data, and the bare live preview. The page itself renders on the
branch S its URL selects (the studio root's prefix here); T is the branch
named by the resource. These tests serve the API at ``/api/`` and the
bootstrap studio at ``/studio/`` (``tests.mounted_urls``), with the studio
layer installed (the ``studio_layer`` fixture)."""

import importlib.util
import io
import json
from html.parser import HTMLParser
from pathlib import Path

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.registration import ResourceRegistration
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI, UI_CSS_TYPE_URI
from scrud_django.ui.layers import layer_item
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import assign_role
from .steps import *  # noqa: F403, F401
from .test_studio_computed import pushes_into
from .test_studio_layer import layer_files
from .test_studio_page import CONFLICT, store_item
from .test_ui_branch_css import make_css
from .test_ui_branch_types import make_branch
from .test_ui_branch_versions import versions_of
from .test_ui_http import HTML, grant, html, make_component

STUDIO_ROOT = "/workbench/"
STUDIO_CONTEXT = "ui-studio"
WORKBENCH = "?ui-scope=studio&ui-mode=view"
UIBRANCH = "http://api.openteams.com/json-ld/UIBranch"
UIBRANCH_ITEMS = "http://api.openteams.com/json-ld/UIBranchItems"
UIBRANCH_STATUS = "http://api.openteams.com/json-ld/UIBranchStatus"
UISELECTABLE = "http://api.openteams.com/json-ld/UISelectableBranches"
PROMOTE = ("Push to", "Propose to", "Apply to", "Promote to")
WRITE_ATTRIBUTES = ("hx-post", "hx-put", "hx-patch", "hx-delete", "data-scrud-json")


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def setup_studio(user):
    """main has the components ``row`` and ``head``; the studio root
    ``studio`` (published at `STUDIO_ROOT`, in `STUDIO_CONTEXT`) has its own
    component ``studio-only``; ``alice-wip`` (in "alice", over main)
    overrides ``row`` (a conflict a pull listed) and the layer's
    ``studio:page`` (based on an older layer item: a layer conflict), and
    adds the CSS ``site``; ``bobs`` is in "bob". `user` may GET the studio
    root, and components in ``""``, ``alice`` and `STUDIO_CONTEXT`, and CSS
    in "alice". Returns ``(studio, alice_wip, bobs)``."""
    make_component("row", "<b>main</b>")
    make_component("head", "<h1>main</h1>")
    studio = make_branch("studio", STUDIO_CONTEXT, parent=None, prefix=STUDIO_ROOT)
    store_item(
        "ui-components",
        "studio",
        STUDIO_CONTEXT,
        name="studio-only",
        engine="jinja2",
        mode="view",
        template="<i>studio</i>",
    )
    alice_wip = make_branch(
        "alice-wip",
        "alice",
        conflicts=[CONFLICT],
        branching_point={"main": versions_of("main")[-1]},
    )
    bobs = make_branch("bobs", "bob")
    store_item(
        "ui-components",
        "alice-wip",
        "alice",
        name="row",
        engine="jinja2",
        mode="view",
        template="<i>{{ value }}</i>",
    )
    _etag, page = layer_item("component", ("studio:page",))
    page = {k: v for k, v in page.items() if k not in ("branch", "rbac_context")}
    store_item("ui-components", "alice-wip", "alice", **page, base="0" * 64)
    make_css("site", "p { color: green }", branch="alice-wip", context="alice")
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI]}, STUDIO_CONTEXT)
    grant(user, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "")
    grant(user, {"http.GET": [UI_COMPONENT_TYPE_URI]}, STUDIO_CONTEXT)
    grant(user, {"http.GET": [UI_CSS_TYPE_URI]}, "alice")
    return studio, alice_wip, bobs


def workbench_url(branch, prefix=None):
    """The workbench of `branch` (a stored ``ui-branches`` resource): its
    detail in the scope studio, in view mode, at the API's URL or under
    `prefix`."""
    path = reverse("ui-branches-detail", args=[branch.pk])
    if prefix is not None:
        path = prefix + path[len("/api/") :]
    return path + WORKBENCH


def get_page(url, status=200):
    When.http.get(url, HTTP_ACCEPT=HTML)
    Then.http.status_code_is(status)
    return html()


class Element:
    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.text = ""

    def classes(self):
        return (self.attrs.get("class") or "").split()

    def within(self, other):
        node = self.parent
        while node is not None:
            if node is other:
                return True
            node = node.parent
        return False


class Page(HTMLParser):
    """The elements of a page, parsed as a browser reads its markup:
    attribute values and text unescaped, and the text of an element its
    descendants' too."""

    VOID = {"input", "meta", "link", "br", "img", "hr"}

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.stack = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        parent = self.stack[-1] if self.stack else None
        element = Element(tag, dict(attrs), parent)
        self.elements.append(element)
        if tag not in self.VOID:
            self.stack.append(element)

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop().tag == tag:
                break

    def handle_data(self, data):
        for element in self.stack:
            element.text += data

    def having(self, attribute, within=None):
        return [
            e
            for e in self.elements
            if attribute in e.attrs and (within is None or e.within(within))
        ]

    def writes(self):
        """The elements with a write attribute."""
        return [e for e in self.elements if any(a in e.attrs for a in WRITE_ATTRIBUTES)]

    def buttons(self):
        return [" ".join(e.text.split()) for e in self.elements if e.tag == "button"]

    def entries(self):
        """``{(kind, name): (classes, open, entry)}`` of the items tree: each
        ``<li class="studio-item ...">``'s classes, its ``data-studio-open``
        JSON and the element."""
        found = {}
        for li in self.elements:
            if li.tag != "li" or "studio-item" not in li.classes():
                continue
            [opener] = self.having("data-studio-open", within=li)
            opened = json.loads(opener.attrs["data-studio-open"])
            found[(opened["kind"], opened["name"])] = (li.classes(), opened, li)
        return found

    def one(self, attribute):
        [element] = self.having(attribute)
        return element


@pytest.mark.django_db
def test_workbench_rules_select_branch_items_and_status(studio_layer):
    _etag, sheet = layer_item("stylesheet", ("studio:studio", ""))
    rules = {(r["select"], r["component"]) for r in sheet["rules"]}
    assert (f"studio {UIBRANCH}", "studio:workbench") in rules
    assert (f"studio studio-items {UIBRANCH_ITEMS}", "studio:items") in rules
    assert (f"studio studio-status {UIBRANCH_STATUS}", "studio:status") in rules
    assert (
        f"studio studio-branches {UISELECTABLE}",
        "studio:branch-picker",
    ) in rules
    for name in (
        "studio:workbench",
        "studio:items",
        "studio:status",
        "studio:branch-picker",
    ):
        _etag, component = layer_item("component", (name,))
        assert component["engine"] == "jinja2" and component["mode"] == "view"


@pytest.mark.django_db
@test_context
def test_workbench_renders_for_branch_user_may_get(studio_layer, ui_alice_login):
    _studio, alice_wip, bobs = setup_studio(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    text = get_page(workbench_url(alice_wip))
    page = Page(text)
    workbench = page.one("data-studio-workbench")
    assert workbench.tag == "div" and workbench.classes() == ["studio-workbench"]
    assert workbench.attrs["data-studio-branch"] == "alice-wip"
    assert workbench.attrs["data-studio-published"] == "false"
    # The top bar's toggles, the three panes and the strip.
    toggles = page.having("data-studio-toggle", within=workbench)
    assert [t.attrs["data-studio-toggle"] for t in toggles] == [
        "items",
        "editor",
        "preview",
    ]
    assert all(t.attrs["type"] == "button" for t in toggles)
    panes = page.having("data-studio-pane", within=workbench)
    assert [(p.tag, p.classes(), p.attrs["data-studio-pane"]) for p in panes] == [
        ("section", ["studio-pane"], name) for name in ("items", "editor", "preview")
    ]
    notice = page.one("data-studio-notice")
    assert "hidden" in notice.attrs and notice.attrs["role"] == "alert"
    # The items and the strip are embedded: T's studio data in their scopes.
    items_url = reverse("ui-branch-items-detail", args=["alice-wip"])
    status_url = reverse("ui-branch-status-detail", args=["alice-wip"])
    embeds = {
        e.attrs["data-studio-embed"]: (
            e.tag,
            e.attrs["data-studio-src"],
            e.attrs["data-studio-scope"],
        )
        for e in page.having("data-studio-embed")
    }
    # Task 8 adds the branch settings: T's own resource in the scope
    # studio studio-branch (see test_studio_rollback_presentations). The
    # final review adds the top bar's branch picker: the user's selectable
    # branches in the scope studio studio-branches
    # (test_workbench_branch_picker_links_readable_branches).
    branch_url = reverse("ui-branches-detail", args=[alice_wip.pk])
    branches_url = reverse("ui-selectable-branches-detail", args=["mine"])
    assert embeds == {
        "branches": ("div", branches_url, "studio studio-branches"),
        "branch": ("div", branch_url, "studio studio-branch"),
        "items": ("div", items_url, "studio studio-items"),
        "status": ("footer", status_url, "studio studio-status"),
    }
    [picker, settings, items_pane, strip] = page.having("data-studio-embed")
    assert picker.within(page.one("data-studio-topbar"))
    assert settings.within(page.one("data-studio-branch-panel"))
    assert not any(settings.within(pane) for pane in panes)
    assert items_pane.within(panes[0]) and not strip.within(panes[2])
    frames = {e.attrs["data-scrud-href"]: e for e in page.having("data-scrud-href")}
    assert frames[items_url].within(items_pane)
    assert frames[items_url].attrs["data-scrud-scope"] == "studio studio-items"
    assert frames[status_url].within(strip)
    assert frames[status_url].attrs["data-scrud-scope"] == "studio studio-status"
    assert ("component", "row") in page.entries()
    assert page.one("data-studio-status").within(strip)
    # The bare preview: a hidden form into a sandboxed frame; studio.js
    # fills the token and the request.
    form = page.one("data-studio-preview-form")
    assert form.tag == "form" and form.within(panes[2])
    assert form.attrs["method"] == "post" and form.attrs["target"] == "studio-preview"
    assert "hidden" in form.attrs
    inputs = [e.attrs for e in page.elements if e.tag == "input" and e.within(form)]
    assert inputs == [
        {"type": "hidden", "name": "csrfmiddlewaretoken", "value": ""},
        {"type": "hidden", "name": "request", "value": ""},
    ]
    [iframe] = [e for e in page.elements if e.tag == "iframe"]
    assert iframe.within(panes[2])
    assert iframe.attrs["name"] == "studio-preview"
    assert iframe.attrs["sandbox"] == "allow-scripts"
    assert "studio-preview" in iframe.classes()
    # The workbench is a view of T: alice may PUT and DELETE alice-wip, yet
    # the requested view mode gives no form and no Delete button.
    [frame, *_settings] = [
        e
        for e in page.having("data-scrud-href")
        if e.attrs["data-scrud-href"] == branch_url
    ]
    assert frame.tag == "div"
    assert workbench.within(frame)
    # Task 8: T's settings are T's own resource embedded in the scope
    # studio studio-branch with the user's own mode, so alice gets its edit
    # form there, with the runtime's Delete; the workbench's own frame has
    # neither. The other form is "New branch".
    [settings] = _settings
    assert settings.tag == "form" and settings.within(workbench)
    assert settings.attrs["data-scrud-scope"] == "studio studio-branch"
    deletes = [e for e in page.elements if e.attrs.get("hx-delete") == branch_url]
    assert [d.within(settings) for d in deletes] == [True]
    others = [e for e in page.elements if e.tag == "form" and e is not form]
    assert others == [settings, page.one("data-studio-new-branch")]
    # A branch the user may not GET: as ui-branches answers it for that
    # user (alice may GET no branch in "bob": not found), and nothing of the
    # branch shows, neither its name nor its label nor its items.
    ResourceRegistration.update(
        {**bobs.content, "label": "Label Only Bob Sees"}, "ui-branches", bobs.pk
    )
    store_item(
        "ui-components",
        "bobs",
        "bob",
        name="bobs-own-row",
        engine="jinja2",
        mode="view",
        template="template only bob sees",
    )
    When.http.get(
        reverse("ui-branches-detail", args=[bobs.pk]), HTTP_ACCEPT="application/json"
    )
    refused = Http.response.status_code
    assert refused == 404
    refused_page = get_page(workbench_url(bobs), status=refused)
    assert not Page(refused_page).having("data-studio-workbench")
    for secret in ("bobs", "Label Only Bob Sees", "template only bob sees"):
        assert secret not in refused_page, secret
    get_page(reverse("ui-branches-detail", args=[999999]) + WORKBENCH, status=404)


@pytest.mark.django_db
@test_context
def test_workbench_embeds_items_and_strip_for_T_not_S(studio_layer, ui_alice_login):
    _studio, alice_wip, _bobs = setup_studio(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    # S is the studio root (the prefix's branch); T is alice-wip.
    page = Page(get_page(workbench_url(alice_wip, STUDIO_ROOT)))
    assert page.one("data-studio-workbench").attrs["data-studio-branch"] == "alice-wip"
    found = page.entries()
    # T's items: its own, main's, the layer's; never S's own.
    assert ("component", "studio-only") not in found
    row_classes, row, row_li = found[("component", "row")]
    assert "studio-own" in row_classes and "studio-conflict-parent" in row_classes
    assert row["href"].startswith(STUDIO_ROOT + "ui-components/")
    assert row["override"] is None and row["applies_to"] is None
    assert not page.having("data-studio-content", within=row_li)
    assert not [e for e in page.writes() if e.within(row_li)]
    head_classes, head, head_li = found[("component", "head")]
    assert "studio-inherited" in head_classes
    assert not any(c.startswith("studio-conflict") for c in head_classes)
    assert head["override"]["method"] == "POST"
    assert head["override"]["body"]["branch"] == "alice-wip"
    assert head["href"].startswith(STUDIO_ROOT + "ui-components/")
    page_classes, _page_item, _li = found[("component", "studio:page")]
    assert "studio-own" in page_classes and "studio-conflict-layer" in page_classes
    css_classes, _css, _li = found[("css", "site")]
    assert "studio-own" in css_classes
    # A packaged item: no href, its content as indented JSON in a hidden
    # <pre>.
    wb_classes, wb, wb_li = found[("component", "studio:workbench")]
    assert "studio-packaged" in wb_classes and wb["href"] is None
    [pre] = page.having("data-studio-content", within=wb_li)
    assert pre.tag == "pre" and "hidden" in pre.attrs
    _etag, content = layer_item("component", ("studio:workbench",))
    assert json.loads(pre.text) == content
    assert pre.text.startswith("{\n  ")
    sheet_classes, sheet, _li = found[("stylesheet", "studio:studio")]
    assert "studio-packaged" in sheet_classes and sheet["applies_to"] == ""
    # The strip: T's chain and counts.
    status = page.one("data-studio-status")
    chain = [e.text for e in page.elements if "studio-chain-branch" in e.classes()]
    assert chain == ["alice-wip", "main"]
    assert page.one("data-studio-changed").attrs["data-studio-changed"] == "3"
    count = page.one("data-studio-conflict-count")
    assert count.attrs["data-studio-conflict-count"] == "2"
    endpoints = json.loads(status.attrs["data-studio-endpoints"])
    assert endpoints["live_preview"] == reverse("scrud-studio:live-preview")
    # The strip has no browse link without a type (studio.js asks with one).
    assert not [e for e in page.elements if "studio-browse" in e.classes()]


def picker_entries(page):
    """``[(name, classes, link href or None, badge, current)]`` of the top
    bar's branch picker, in order."""
    picker = page.one("data-studio-branch-picker")
    found = []
    for entry in page.having("data-studio-branch-name", within=picker):
        links = [e for e in page.elements if e.tag == "a" and e.within(entry)]
        [badge] = [
            e
            for e in page.elements
            if e.within(entry) and "studio-badge" in e.classes()
        ]
        found.append(
            (
                entry.attrs["data-studio-branch-name"],
                entry.classes(),
                links[0].attrs["href"] if links else None,
                badge.text,
                "data-studio-current" in entry.attrs,
            )
        )
    return found


@pytest.mark.django_db
@test_context
def test_workbench_branch_picker_links_readable_branches(studio_layer, ui_alice_login):
    """The top bar's branch picker (spec Goal 2) lists the branches the user
    may select (``ui-selectable-branches/mine/``), main first: each one whose
    resource the user may GET links to its workbench under the page's prefix;
    one they may not GET (main here) is text, with no workbench to open. A
    branch the user may not select (bobs) is absent."""
    studio, alice_wip, _bobs = setup_studio(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    page = Page(get_page(workbench_url(alice_wip, STUDIO_ROOT)))
    topbar = page.one("data-studio-topbar")
    [embed] = [
        e
        for e in page.having("data-studio-embed")
        if e.attrs["data-studio-embed"] == "branches"
    ]
    assert embed.within(topbar)
    assert (
        embed.attrs["data-studio-src"] == STUDIO_ROOT + "ui-selectable-branches/mine/"
    )
    assert embed.attrs["data-studio-scope"] == "studio studio-branches"
    picker = page.one("data-studio-branch-picker")
    assert picker.tag == "nav" and picker.within(embed)
    assert picker.attrs["aria-label"] == "Branches"
    frames = [e for e in page.having("data-scrud-href") if e.within(embed)]
    assert [
        (f.attrs["data-scrud-href"], f.attrs["data-scrud-scope"]) for f in frames
    ] == [(embed.attrs["data-studio-src"], "studio studio-branches")]
    assert picker_entries(page) == [
        (
            "main",
            ["studio-branch-choice", "studio-branch-published"],
            None,
            "published",
            False,
        ),
        (
            "alice-wip",
            ["studio-branch-choice", "studio-branch-working"],
            workbench_url(alice_wip, STUDIO_ROOT),
            "working",
            False,
        ),
        (
            "studio",
            ["studio-branch-choice", "studio-branch-published"],
            workbench_url(studio, STUDIO_ROOT),
            "published",
            False,
        ),
    ]
    # main, which alice may not GET, is its name as text.
    [main] = [
        e
        for e in page.having("data-studio-branch-name", within=picker)
        if e.attrs["data-studio-branch-name"] == "main"
    ]
    [name] = [
        e
        for e in page.elements
        if e.within(main) and "studio-branch-name" in e.classes()
    ]
    assert name.tag == "span" and name.text == "main"
    # The picker writes nothing.
    assert not [e for e in page.writes() if e.within(picker)]
    # At the API's URLs the links stay there.
    page = Page(get_page(workbench_url(alice_wip)))
    links = [href for _n, _c, href, _b, _cur in picker_entries(page) if href]
    assert links == [workbench_url(alice_wip), workbench_url(studio)]
    # Each link is the workbench of its branch.
    for href in links:
        assert Page(get_page(href)).one("data-studio-workbench")


@pytest.mark.django_db
@test_context
def test_override_button_is_the_items_action(studio_layer, ui_alice_login):
    _studio, alice_wip, _bobs = setup_studio(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    page = Page(get_page(workbench_url(alice_wip)))
    _classes, head, head_li = page.entries()[("component", "head")]
    [button] = [e for e in page.writes() if e.within(head_li)]
    action = head["override"]
    assert button.tag == "button" and button.text == "Override on this branch"
    assert button.attrs["type"] == "button"
    assert button.attrs["hx-post"] == action["href"]
    assert button.attrs["hx-ext"] == "scrud-json"
    assert button.attrs["hx-swap"] == "none"
    assert button.attrs["data-scrud-json"] == "fixed"
    assert json.loads(button.attrs["data-scrud-body"]) == action["body"]
    assert button.attrs["data-scrud-etag"] == ""


@pytest.mark.django_db
@test_context
def test_items_tree_collapses_packaged_items_per_kind(studio_layer, ui_alice_login):
    """The packaged items (the studio layer's 26 components among them) made the
    Items pane so long that the strip landed far below the preview: each
    kind lists the branch's own and inherited items, and its packaged ones
    in a collapsed ``<details data-studio-packaged>`` "Packaged (N)"."""
    _studio, alice_wip, _bobs = setup_studio(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    page = Page(get_page(workbench_url(alice_wip)))
    sections = {
        e.attrs["data-studio-kind"]: e
        for e in page.elements
        if e.tag == "section" and "studio-kind" in e.classes()
    }
    assert list(sections) == ["component", "stylesheet", "css", "js"]
    groups = {}
    for group in page.having("data-studio-packaged"):
        assert group.tag == "details" and "open" not in group.attrs
        assert "studio-packaged-group" in group.classes()
        [kind] = [k for k, section in sections.items() if group.within(section)]
        assert kind not in groups
        groups[kind] = group
    assert list(groups) == ["component", "stylesheet", "css", "js"]
    entries = page.entries()
    for (kind, name), (classes, _opened, li) in entries.items():
        packaged = "studio-packaged" in classes
        assert li.within(sections[kind]), (kind, name)
        assert li.within(groups[kind]) == packaged, (kind, name)
    # Own and inherited items stay in view.
    assert not entries[("component", "row")][2].within(groups["component"])
    assert not entries[("component", "head")][2].within(groups["component"])
    assert not entries[("css", "site")][2].within(groups["css"])
    # Each summary counts its group: every packaged item of the kind, the
    # studio layer's (but studio:page, which alice-wip overrides: its own
    # item now) among them.
    for kind, group in groups.items():
        packaged = {
            name
            for (k, name), (classes, _o, _li) in entries.items()
            if k == kind and "studio-packaged" in classes
        }
        studio = {content["name"] for content in layer_files(kind).values()}
        assert studio - {"studio:page"} <= packaged, kind
        assert "studio:page" not in packaged
        [summary] = [e for e in page.elements if e.tag == "summary" and e.within(group)]
        assert summary.text == f"Packaged ({len(packaged)})"
    assert len(layer_files("component")) > 20


@pytest.mark.django_db
@test_context
def test_items_tree_kind_headings(studio_layer, ui_alice_login):
    """Each kind of the items tree has its proper heading, as the version
    page has them: Components, Stylesheets, CSS (the text is upper case; in
    the system font's bold its C and S can look like lower case) and
    JavaScript (it was "JS")."""
    _studio, alice_wip, _bobs = setup_studio(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    page = Page(get_page(workbench_url(alice_wip)))
    headings = [
        (e.parent.attrs["data-studio-kind"], e.text)
        for e in page.elements
        if "studio-kind-title" in e.classes()
    ]
    assert headings == [
        ("component", "Components"),
        ("stylesheet", "Stylesheets"),
        ("css", "CSS"),
        ("js", "JavaScript"),
    ]
    assert all(
        e.tag == "h3" for e in page.elements if "studio-kind-title" in e.classes()
    )


@pytest.mark.django_db
@test_context
def test_reader_without_ui_grants_sees_no_write_controls(studio_layer, ui_alice_login):
    """Review Focus 4: a user who may GET T but holds no grant on the UI
    item types gets the workbench, without the items they can't read and
    without a write control."""
    _studio, alice_wip, _bobs = setup_studio(ui_alice_login)
    reader = assign_role({"http.GET": [UI_BRANCH_TYPE_URI]}, "alice", STUDIO_CONTEXT)
    Given.http.force_authenticate(reader)
    page = Page(get_page(workbench_url(alice_wip, STUDIO_ROOT)))
    assert page.one("data-studio-workbench").attrs["data-studio-branch"] == "alice-wip"
    found = page.entries()
    # Only the packaged items (public); none of the stored rows.
    assert ("component", "studio:workbench") in found
    for key, (classes, opened, _li) in found.items():
        assert "studio-packaged" in classes and opened["href"] is None, key
    for name in ("row", "head", "studio-only", "site"):
        assert not [key for key in found if key[1] == name], name
    # alice-wip's override of studio:page is left out, and the packaged item
    # it hides does not show in its place.
    assert ("component", "studio:page") not in found
    assert all(o["override"] is None for _c, o, _li in found.values())
    assert page.writes() == []
    assert "Override on this branch" not in page.buttons()
    # The review of a push into main is a read (the push preview, loaded
    # when its panel opens): its opener is offered, with no Pull, Apply or
    # Propose.
    assert [b for b in page.buttons() if b == "Pull" or b.startswith(PROMOTE)] == [
        "Promote to main…"
    ]
    assert page.one("data-studio-changed").attrs["data-studio-changed"] == "0"
    count = page.one("data-studio-conflict-count")
    assert count.attrs["data-studio-conflict-count"] == "0"


@pytest.mark.django_db
@test_context
def test_published_T_strip_has_no_write_actions(studio_layer, admin_login):
    studio, _alice_wip, _bobs = setup_studio(admin_login)
    main = make_branch("main", "", parent=None)
    Given.http.force_authenticate(admin_login)
    for branch in (studio, main):
        page = Page(get_page(workbench_url(branch, STUDIO_ROOT)))
        workbench = page.one("data-studio-workbench")
        assert workbench.attrs["data-studio-branch"] == branch.content["name"]
        assert workbench.attrs["data-studio-published"] == "true"
        found = page.entries()
        assert found
        assert all(o["override"] is None for _c, o, _li in found.values())
        # The superuser may do anything, yet no write control is offered
        # but "New branch" (Task 8: creating a branch under a published one
        # is normal; it writes nothing of T) and the history's restores
        # (rolling back a publish: a push record, as a promote is). T's
        # settings are embedded in view mode: no form.
        new_branch, *restores = page.writes()
        assert "data-studio-new-branch" in new_branch.attrs, branch
        assert new_branch.attrs["hx-post"] == STUDIO_ROOT + "ui-branches/"
        assert restores, branch
        for restore in restores:
            assert "studio-restore" in restore.classes()
            assert restore.attrs["hx-post"] == STUDIO_ROOT + "ui-pushes/"
            body = json.loads(restore.attrs["data-scrud-body"])
            assert body["source"] is None
            assert body["target"] == branch.content["name"]
        [settings] = page.having("data-studio-branch-settings")
        assert settings.attrs["data-studio-mode"] == "view", branch
        assert "Override on this branch" not in page.buttons()
        assert not [b for b in page.buttons() if b == "Pull" or b.startswith(PROMOTE)]


@pytest.mark.django_db
@test_context
def test_workbench_hrefs_stay_in_mount(studio_layer, ui_alice_login):
    _studio, alice_wip, _bobs = setup_studio(ui_alice_login)
    pushes_into(ui_alice_login, "")
    Given.http.force_authenticate(ui_alice_login)
    page = Page(get_page(workbench_url(alice_wip, STUDIO_ROOT)))
    opened = [o for _c, o, _li in page.entries().values()]
    hrefs = [o["href"] for o in opened if o["href"] is not None]
    assert hrefs and all(h.startswith(STUDIO_ROOT) for h in hrefs), hrefs
    for embed in page.having("data-studio-embed"):
        assert embed.attrs["data-studio-src"].startswith(STUDIO_ROOT), embed.attrs
    # Every write control: the overrides, Pull, and Apply and Propose to main.
    writes = page.writes()
    urls = [
        e.attrs[f"hx-{m}"]
        for e in writes
        for m in ("post", "put", "patch", "delete")
        if f"hx-{m}" in e.attrs
    ]
    assert len(urls) == len(writes) > 3
    assert all(url.startswith(STUDIO_ROOT) for url in urls), urls
    by_label = {" ".join(e.text.split()): e for e in writes}
    pull = by_label["Pull"]
    assert pull.attrs["hx-put"] == STUDIO_ROOT + f"ui-branches/{alice_wip.pk}/"
    assert json.loads(pull.attrs["data-scrud-body"])["pull"] is True
    assert pull.attrs["data-scrud-etag"].startswith('"')
    # Apply and Propose sit in the "Promote to main…" panel; Apply is off
    # until the panel's review shows no conflict (studio:promote).
    for label, state in (("Apply to main", "applied"), ("Propose to main", "proposed")):
        button = by_label[label]
        assert ("disabled" in button.attrs) is (state == "applied")
        assert button.attrs["hx-post"] == STUDIO_ROOT + "ui-pushes/"
        assert button.attrs["hx-swap"] == "none"
        assert button.attrs["data-scrud-etag"] == ""
        assert json.loads(button.attrs["data-scrud-body"])["state"] == state


SEED = Path(__file__).parent.parent / "demo/demo/management/commands/seed_todo_demo.py"


def load_seed():
    """The demo's seed command module. The suite does not put ``demo/`` on
    the path (and a package called ``demo`` is not this project's), so it is
    loaded from its file; it imports only scrud_django and Django."""
    spec = importlib.util.spec_from_file_location("seed_todo_demo", SEED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_seed(module):
    out = io.StringIO()
    module.Command(stdout=out, stderr=io.StringIO()).handle()
    return out.getvalue()


def stored_state():
    from scoped_rbac.models import Role, RoleAssignment

    from scrud_django.models import Resource

    return (
        sorted(
            (r.pk, r.etag, json.dumps(r.content, sort_keys=True))
            for r in Resource.objects.all()
        ),
        sorted((r.pk, r.etag) for r in Role.objects.all()),
        sorted((a.pk, a.etag) for a in RoleAssignment.objects.all()),
    )


def seeded_branch(name):
    from scrud_django.models import Resource

    [branch] = [
        r
        for r in Resource.objects.filter(resource_type__slug="ui-branches")
        if r.content["name"] == name
    ]
    return branch


@pytest.mark.django_db
@test_context
def test_seed_creates_studio_tree_and_grants(studio_layer, settings):
    from django.contrib.auth import get_user_model

    module = load_seed()
    first = run_seed(module)
    state = stored_state()
    second = run_seed(module)
    assert stored_state() == state
    assert "created" not in second and "updated" not in second

    studio = seeded_branch("studio")
    assert studio.content["parent"] is None
    assert studio.content["prefix"] == STUDIO_ROOT
    assert studio.content["rbac_context"] == STUDIO_CONTEXT
    wip = seeded_branch("studio-wip")
    assert wip.content["parent"] == "studio"
    assert wip.content.get("prefix") is None
    assert wip.content["rbac_context"] == "ui-studio-wip"
    alice = seeded_branch("alice-wip")
    for branch in (alice, wip):
        assert f"/workbench/ui-branches/{branch.pk}/{WORKBENCH}" in first

    author = get_user_model().objects.get(username="author")
    Given.http.force_authenticate(author)
    # The workbench of the working branch, served by the published studio.
    get_page(workbench_url(wip, STUDIO_ROOT))
    get_page(workbench_url(studio, STUDIO_ROOT))
    component = {
        "name": "from-seed-test",
        "engine": "jinja2",
        "mode": "view",
        "template": "<b>x</b>",
    }
    When.http.post(
        reverse("ui-components-list"),
        data={**component, "branch": "studio-wip", "rbac_context": "ui-studio-wip"},
        format="json",
    )
    Then.http.status_code_is(201)
    # Pushing the working branch into the studio root is allowed.
    When.http.post(
        reverse("ui-pushes-list"),
        data={
            "source": "studio-wip",
            "target": "studio",
            "rbac_context": STUDIO_CONTEXT,
            "state": "applied",
        },
        format="json",
    )
    Then.http.status_code_is(201)
    # Only the author holds the studio's grants: the owner may not write
    # its working branch. (The author's POST on the studio root itself is
    # allowed too: applying a push needs the grants a direct write needs.)
    Given.http.force_authenticate(get_user_model().objects.get(username="owner"))
    When.http.post(
        reverse("ui-components-list"),
        data={**component, "branch": "studio-wip", "rbac_context": "ui-studio-wip"},
        format="json",
    )
    Then.http.status_code_is(403)
    # The owner holds the site-level ui-branches grant (PUT in "") that
    # publishing a prefix needs.
    from scoped_rbac.models import RoleAssignment

    assignment = RoleAssignment.objects.get(
        user__username="owner", rbac_context=DEFAULT_CONTEXT
    )
    assert UI_BRANCH_TYPE_URI in assignment.role.definition["http.PUT"]


@pytest.mark.django_db
@test_context
def test_seed_todo_row_override_is_editable_in_the_workbench(studio_layer):
    """The acceptance run's step 3: in alice-wip's workbench, the author
    overrides the demo's ``todo-row`` and types in its template. The studio
    edits Jinja only (Django-engine components are read-only), so the seed's
    ``todo-row`` is a Jinja component and its override opens in edit mode,
    with the named code textarea and Save."""
    from django.contrib.auth import get_user_model

    from .test_studio_editors import open_editor

    run_seed(load_seed())
    alice = seeded_branch("alice-wip")
    Given.http.force_authenticate(get_user_model().objects.get(username="author"))
    page = Page(get_page(workbench_url(alice, STUDIO_ROOT)))
    _classes, row, _li = page.entries()[("component", "todo-row")]
    action = row["override"]
    assert action["body"]["engine"] == "jinja2"
    When.http.post(action["href"], data=action["body"], format="json")
    Then.http.status_code_is(201)
    page = Page(get_page(workbench_url(alice, STUDIO_ROOT)))
    classes, own, _li = page.entries()[("component", "todo-row")]
    assert "studio-own" in classes
    editor = Page(open_editor(own["href"]))
    [code] = editor.having("data-studio-code")
    assert code.tag == "textarea" and code.attrs["name"] == "/template"
    assert "readonly" not in code.attrs
    assert "Save" in editor.buttons()
