"""Promote in the self-hosted studio: the strip's "Promote to <target>…"
review panels (one per ``actions.promote`` entry: the push preview loaded on
demand in the scope ``studio studio-push``, Apply and Propose as the entry's
fixed-body actions) and the ``studio:push`` page of a push (its changes and
a state form). Every write goes through the API with the exact body the
strip or the page renders. These tests serve the API at ``/api/``
(``tests.mounted_urls``), with the studio layer installed (the
``studio_layer`` fixture)."""

import json
from urllib.parse import quote

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)
from scrud_django.ui.branches import ITEM_KINDS, UI_BRANCH_TYPE_URI, UI_PUSH_TYPE_URI
from scrud_django.ui.layers import layer_item
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import assign_ui_role
from .steps import *  # noqa: F403, F401
from .test_studio_computed import store_push
from .test_studio_editors import content_of, root_frame
from .test_studio_workbench import PROMOTE, Page, workbench_url
from .test_ui_branch_push import (
    APPROVAL_POLICY,
    WIP,
    component_writer,
    create,
    created,
    make_wip,
    own_rows,
    pusher,
    stored_branch,
    stored_state,
    wip_overrides_row,
)
from .test_ui_branch_types import component, make_branch, stylesheet
from .test_ui_branch_versions import versions_of
from .test_ui_http import HTML, grant, make_component, make_stylesheet

JSON = "application/json"
PUSH_SCOPE = "studio studio-push"
UIPUSH = "http://api.openteams.com/json-ld/UIPush"
UIPUSH_PREVIEW = "http://api.openteams.com/json-ld/UIPushPreview"


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def get_page(url, status=200, **headers):
    When.http.get(url, HTTP_ACCEPT=HTML, **headers)
    Then.http.status_code_is(status)
    return Page(Http.response.content.decode())


def text_of(element):
    return " ".join(element.text.split())


def promote_boxes(page):
    """``{target: box}`` of the strip's promote panels, in order."""
    return {
        e.attrs["data-studio-promote"]: e for e in page.having("data-studio-promote")
    }


def box_button(page, box, cls):
    found = [
        e
        for e in page.elements
        if e.tag == "button" and cls in e.classes() and e.within(box)
    ]
    assert len(found) <= 1
    return found[0] if found else None


def load_review(box, status=200):
    """The push preview as studio:promote loads it into `box`: an htmx GET
    of the box's source with ``?target=`` (URL-encoded) in the scope
    ``studio studio-push``."""
    url = box.attrs["data-studio-promote-src"] + "?target="
    url += quote(box.attrs["data-studio-promote"], safe="")
    return get_page(
        url, status=status, HTTP_HX_REQUEST="true", HTTP_SCRUD_UI_SCOPE=PUSH_SCOPE
    )


def press(button, status):
    """Send `button`, a fixed-body element, as scrud-json does: its method
    to its URL with its ``data-scrud-body`` byte for byte, and its
    ``data-scrud-etag`` (none when empty)."""
    [method] = [
        m for m in ("post", "put", "patch", "delete") if f"hx-{m}" in button.attrs
    ]
    headers = {"HTTP_ACCEPT": HTML, "HTTP_HX_REQUEST": "true"}
    if button.attrs["data-scrud-etag"]:
        headers["HTTP_IF_MATCH"] = button.attrs["data-scrud-etag"]
    getattr(When.http, method)(
        button.attrs[f"hx-{method}"],
        data=button.attrs["data-scrud-body"],
        content_type=JSON,
        **headers,
    )
    Then.http.status_code_is(status)
    return Http.response


def preview_of(page):
    [found] = page.having("data-studio-push-preview")
    return found


def counts(found):
    return {
        name: found.attrs[f"data-studio-{name}"]
        for name in (
            "changes",
            "conflicts",
            "unreadable-changes",
            "unreadable-conflicts",
        )
    }


def push_page_url(href):
    return href + "?ui-scope=studio"


def put_frame(page, href, **fields):
    """The push page's frame PUT: its ``scrud-content`` with `fields`
    applied, ``If-Match`` from the frame, in the frame's scope."""
    frame = root_frame(page, href)
    body = {**content_of(page, frame), **fields}
    When.http.put(
        href,
        data=json.dumps(body),
        content_type=JSON,
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
        HTTP_SCRUD_UI_SCOPE=frame.attrs["data-scrud-scope"],
        HTTP_SCRUD_UI_POINTER="",
        HTTP_IF_MATCH=frame.attrs["data-scrud-etag"],
    )
    return Http.response


def wip_with_changes(admin_login):
    """main has ``row``; ``wip`` (in "alice", over main) overrides it and
    adds ``fresh``. Returns ``(main's row, wip's override, wip)``."""
    main_row, override = wip_overrides_row(admin_login)
    create("ui-components", component(name="fresh", template="<em>fresh</em>", **WIP))
    return main_row, override, stored_branch("wip")


def change_main_row(main_row, template="<b>changed</b>"):
    """main changes ``row`` after wip's override was based on it: a
    conflict for a push of wip."""
    ResourceRegistration.update(
        {**main_row.content, "template": template}, "ui-components", main_row.pk
    )
    main_row.refresh_from_db()


@pytest.mark.django_db
def test_promote_rules_and_components(studio_layer):
    _etag, sheet = layer_item("stylesheet", ("studio:studio", ""))
    rules = [(r["select"], r["component"]) for r in sheet["rules"]]
    assert (f"studio studio-push {UIPUSH_PREVIEW}", "studio:push-preview") in rules
    # The push page, edit mode first: in view mode the view twin wins.
    edit = rules.index((f"studio {UIPUSH}", "studio:push"))
    view = rules.index((f"studio {UIPUSH}", "studio:push-view"))
    assert edit < view
    for name, mode in (
        ("studio:push-preview", "view"),
        ("studio:push", "edit"),
        ("studio:push-view", "view"),
    ):
        _etag, found = layer_item("component", (name,))
        assert found["engine"] == "jinja2" and found["mode"] == mode, name


@pytest.mark.django_db
@test_context
def test_promote_panel_shows_changes_and_conflicts(studio_layer, admin_login):
    main_row, override, wip = wip_with_changes(admin_login)
    change_main_row(main_row)
    page = get_page(workbench_url(wip))
    # One promote panel, for the parent: closed, nothing embedded at render.
    boxes = promote_boxes(page)
    assert list(boxes) == ["main"]
    box = boxes["main"]
    assert box.attrs["data-studio-promote-src"] == reverse(
        "ui-push-previews-detail", args=["wip"]
    )
    assert box.within(page.one("data-studio-status"))
    [opener] = page.having("data-studio-promote-open", within=box)
    assert opener.tag == "button" and opener.attrs["type"] == "button"
    assert text_of(opener) == "Promote to main…"
    assert opener.attrs["aria-expanded"] == "false"
    [panel] = page.having("data-studio-promote-panel", within=box)
    assert (
        "hidden" in panel.attrs and opener.attrs["aria-controls"] == panel.attrs["id"]
    )
    [review] = page.having("data-studio-promote-review", within=panel)
    assert review.text.strip() == "" and not page.having("data-studio-push-preview")
    [after] = page.having("data-studio-after-push", within=panel)
    assert after.tag == "button" and text_of(after) == "Preview after push"
    assert "hx-post" not in after.attrs and "data-scrud-json" not in after.attrs
    [reason] = page.having("data-studio-promote-reason", within=panel)
    assert "hidden" in reason.attrs
    # The review, as the panel loads it: the changes with before and after,
    # and the conflicts.
    loaded = load_review(box)
    found = preview_of(loaded)
    assert found.attrs["data-studio-source"] == "wip"
    assert found.attrs["data-studio-target"] == "main"
    assert counts(found) == {
        "changes": "2",
        "conflicts": "1",
        "unreadable-changes": "0",
        "unreadable-conflicts": "0",
    }
    frames = loaded.having("data-scrud-href")
    assert frames[0].attrs["data-scrud-scope"] == PUSH_SCOPE
    assert loaded.writes() == []
    changes = [e for e in loaded.elements if "studio-push-change" in e.classes()]
    assert [c.attrs["data-studio-change-action"] for c in changes] == [
        "update",
        "create",
    ]
    row, fresh = changes
    assert text_of(row).startswith("update component row")
    [before] = [
        e
        for e in loaded.elements
        if "studio-push-before" in e.classes() and e.within(row)
    ]
    [after] = [
        e
        for e in loaded.elements
        if "studio-push-after" in e.classes() and e.within(row)
    ]
    # Text, escaped: the templates show as they are stored.
    assert "<b>changed</b>" in before.text and "<i>wip</i>" not in before.text
    assert "<i>wip</i>" in after.text
    changed = [
        text_of(e)
        for e in loaded.elements
        if "studio-push-field-changed" in e.classes() and e.within(after)
    ]
    assert any(t.startswith("template") for t in changed), changed
    assert not any(t.startswith("name") for t in changed), changed
    assert "<em>fresh</em>" in fresh.text and "Nothing" in text_of(
        [
            e
            for e in loaded.elements
            if "studio-push-before" in e.classes() and e.within(fresh)
        ][0]
    )
    [conflict] = [e for e in loaded.elements if "studio-push-conflict" in e.classes()]
    assert text_of(conflict).startswith("component row")
    # Nothing on items the user can't read: no such note.
    assert not loaded.having("data-studio-unreadable")
    # The strip's actions: Apply (off until the review is loaded) and
    # Propose, the entry's fixed bodies, inside the panel.
    apply = box_button(page, panel, "studio-apply")
    propose = box_button(page, panel, "studio-propose")
    assert text_of(apply) == "Apply to main" and "disabled" in apply.attrs
    assert text_of(propose) == "Propose to main" and "disabled" not in propose.attrs
    for button, state in ((apply, "applied"), (propose, "proposed")):
        assert button.attrs["hx-post"] == reverse("ui-pushes-list")
        assert button.attrs["hx-ext"] == "scrud-json"
        assert button.attrs["hx-swap"] == "none"
        assert button.attrs["data-scrud-json"] == "fixed"
        assert button.attrs["data-scrud-etag"] == ""
        assert json.loads(button.attrs["data-scrud-body"]) == {
            "source": "wip",
            "target": "main",
            "state": state,
            "rbac_context": "",
        }
    # The push itself is refused for the conflict.
    press(apply, 409)
    main_row.refresh_from_db()
    assert main_row.content["template"] == "<b>changed</b>"
    assert Resource.objects.filter(pk=override.pk).exists()


@pytest.mark.django_db
@test_context
def test_apply_push_from_panel_round_trip(studio_layer, admin_login):
    main_row, _override, wip = wip_with_changes(admin_login)
    page = get_page(workbench_url(wip))
    box = promote_boxes(page)["main"]
    assert counts(preview_of(load_review(box))) == {
        "changes": "2",
        "conflicts": "0",
        "unreadable-changes": "0",
        "unreadable-conflicts": "0",
    }
    before = versions_of("main")[-1]
    press(box_button(page, box, "studio-apply"), 201)
    push = created()
    assert push.content["state"] == "applied"
    assert push.content["result"] == {"version": versions_of("main")[-1]}
    assert versions_of("main")[-1] > before
    # The target changed, and T is emptied.
    main_row.refresh_from_db()
    assert main_row.content["template"] == "<i>wip</i>"
    fresh = [c for c in own_rows(UI_COMPONENT_TYPE_URI, "main") if c["name"] == "fresh"]
    assert [c["template"] for c in fresh] == ["<em>fresh</em>"]
    assert own_rows(UI_COMPONENT_TYPE_URI, "wip") == []
    # The strip, fetched again: nothing changed, the push listed with its
    # page, and the review has nothing left to push.
    page = get_page(workbench_url(wip))
    assert page.one("data-studio-changed").attrs["data-studio-changed"] == "0"
    [link] = [
        e
        for e in page.having("href", within=page.one("data-studio-status"))
        if "studio-push-link" in e.classes()
    ]
    href = reverse("ui-pushes-detail", args=[push.pk])
    assert link.attrs["href"] == push_page_url(href)
    assert text_of(link) == "wip → main"
    assert (
        counts(preview_of(load_review(promote_boxes(page)["main"])))["changes"] == "0"
    )


@pytest.mark.django_db
@test_context
def test_propose_then_apply_from_push_page(studio_layer, admin_login):
    main_row, _override, wip = wip_with_changes(admin_login)
    page = get_page(workbench_url(wip))
    box = promote_boxes(page)["main"]
    press(box_button(page, box, "studio-propose"), 201)
    push = created()
    assert push.content["state"] == "proposed" and "result" not in push.content
    main_row.refresh_from_db()
    assert main_row.content["template"] == "<b>main</b>"
    # The strip lists the push, linked to its page in the scope studio
    # (edit mode: no ui-mode=view).
    page = get_page(workbench_url(wip))
    href = reverse("ui-pushes-detail", args=[push.pk])
    [entry] = [e for e in page.elements if "studio-push-entry" in e.classes()]
    assert entry.attrs["data-studio-push-state"] == "proposed"
    assert "proposed" in text_of(entry)
    [link] = [e for e in page.having("href") if e.within(entry)]
    assert link.attrs["href"] == push_page_url(href) == href + "?ui-scope=studio"
    # The push page: its record, its review embedded, and the state form.
    page = get_page(link.attrs["href"])
    record = page.one("data-studio-push")
    assert record.attrs["data-studio-mode"] == "edit"
    assert text_of(page.one("data-studio-push-source")) == "wip"
    assert text_of(page.one("data-studio-push-target")) == "main"
    assert text_of(page.one("data-studio-push-state")) == "proposed"
    assert text_of(page.one("data-studio-push-result")) == "Not applied yet"
    found = preview_of(page)
    assert found.within(record)
    assert counts(found)["changes"] == "2"
    [embedded] = [
        e
        for e in page.having("data-scrud-href")
        if e.attrs["data-scrud-href"].startswith(
            reverse("ui-push-previews-detail", args=["wip"])
        )
    ]
    assert embedded.attrs["data-scrud-scope"] == PUSH_SCOPE
    frame = root_frame(page, href)
    assert frame.tag == "form" and frame.attrs["hx-put"] == href
    [state] = [e for e in page.elements if e.attrs.get("name") == "/state"]
    assert state.tag == "input" and state.attrs["type"] == "text"
    assert state.attrs["value"] == "proposed" and state.within(frame)
    # studio:promote stops its change (only Save writes) by this marker.
    assert "data-studio-push-state-input" in state.attrs
    [datalist] = [e for e in page.elements if e.tag == "datalist"]
    assert datalist.attrs["id"] == state.attrs["list"]
    options = [
        e.attrs["value"]
        for e in page.elements
        if e.tag == "option" and e.within(datalist)
    ]
    assert options == ["proposed", "applied"]
    save = [e for e in page.elements if e.tag == "button" and e.within(frame)]
    assert [(b.attrs.get("type"), text_of(b)) for b in save][0] == ("submit", "Save")
    for e in page.elements:
        assert not [a for a in e.attrs if a.startswith("on")], e.attrs
        if e.tag == "script":
            # No inline code: linked scripts and JSON data only.
            assert "src" in e.attrs or e.attrs.get("type") == JSON, e.attrs
    # Applying it: the frame's PUT with the state applied.
    response = put_frame(page, href, state="applied")
    assert response.status_code == 200, response.content
    main_row.refresh_from_db()
    assert main_row.content["template"] == "<i>wip</i>"
    assert own_rows(UI_COMPONENT_TYPE_URI, "wip") == []
    # The page again: an applied push is a record, with no form and no
    # review.
    page = Page(response.content.decode())
    assert text_of(page.one("data-studio-push-state")) == "applied"
    version = versions_of("main")[-1]
    assert text_of(page.one("data-studio-push-result")) == f"Version {version} of main"
    assert not [e for e in page.elements if e.attrs.get("name") == "/state"]
    assert not page.having("data-studio-push-preview")


@pytest.mark.django_db
@test_context
def test_refused_transition_shows_server_error(studio_layer, admin_login):
    main_row, _override, _wip = wip_with_changes(admin_login)
    workflow_policy_resource_type()
    ResourceRegistration.register(APPROVAL_POLICY, "workflow-policies")
    user = component_writer()
    grant(user, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "")
    grant(user, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "alice")
    Given.http.force_authenticate(user)
    When.http.post(
        reverse("ui-pushes-list"),
        data={
            "source": "wip",
            "target": "main",
            "state": "proposed",
            "rbac_context": "",
        },
        format="json",
    )
    Then.http.status_code_is(201)
    push = created()
    href = reverse("ui-pushes-detail", args=[push.pk])
    # What the API answers a move from proposed straight to applied.
    When.http.put(
        href,
        data={**push.content, "state": "applied"},
        format="json",
        HTTP_ACCEPT=JSON,
        HTTP_IF_MATCH=f'"{push.etag}"',
    )
    Then.http.status_code_is(400)
    problem = Http.response.json()
    assert problem["type"].endswith("/workflow-transition-invalid")
    page = get_page(push_page_url(href))
    assert page.one("data-studio-push").attrs["data-studio-mode"] == "edit"
    response = put_frame(page, href, state="applied")
    assert response.status_code == 400
    # The runtime's refused-PUT re-render: the server's message, and the
    # form again with the submitted state, in the studio's push page.
    refused = Page(response.content.decode())
    notices = [text_of(e) for e in refused.elements if "scrud-notice" in e.classes()]
    assert notices and problem["title"] in " ".join(notices)
    assert refused.one("data-studio-push").within(root_frame(refused, href))
    [state] = [e for e in refused.elements if e.attrs.get("name") == "/state"]
    assert state.attrs["value"] == "applied"
    assert text_of(refused.one("data-studio-push-result")) == "Not applied yet"
    push.refresh_from_db()
    assert push.content["state"] == "proposed"
    main_row.refresh_from_db()
    assert main_row.content["template"] == "<b>main</b>"


@pytest.mark.django_db
@test_context
def test_push_page_view_mode_and_restore(studio_layer, admin_login):
    wip_with_changes(admin_login)
    proposed = store_push("wip", "main", "", label="ship it")
    restore = store_push(None, "main", "", restore=1)
    reader = assign_ui_role((), "")
    grant(reader, {"http.GET": [UI_PUSH_TYPE_URI]}, "")
    grant(reader, {"http.GET": [UI_BRANCH_TYPE_URI, UI_COMPONENT_TYPE_URI]}, "alice")
    grant(reader, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "")
    Given.http.force_authenticate(reader)
    href = reverse("ui-pushes-detail", args=[proposed.pk])
    page = get_page(push_page_url(href))
    # A user who may not PUT the push: the view twin, no form.
    assert page.one("data-studio-push").attrs["data-studio-mode"] == "view"
    assert root_frame(page, href).tag == "div"
    assert not [e for e in page.elements if e.attrs.get("name") == "/state"]
    assert not [e for e in page.elements if e.tag in ("form", "datalist")]
    assert page.writes() == []
    assert text_of(page.one("data-studio-push-state")) == "proposed"
    assert text_of(page.one("data-studio-push-label")) == "ship it"
    assert counts(preview_of(page))["changes"] == "2"
    # A restore has no source: no review.
    Given.http.force_authenticate(admin_login)
    href = reverse("ui-pushes-detail", args=[restore.pk])
    page = get_page(push_page_url(href))
    assert page.one("data-studio-push").attrs["data-studio-mode"] == "edit"
    assert (
        text_of(page.one("data-studio-push-source")) == "None (a restore of version 1)"
    )
    assert not page.having("data-studio-push-preview")
    assert [e for e in page.elements if e.attrs.get("name") == "/state"]


@pytest.mark.django_db
@test_context
def test_apply_disabled_with_conflicts(studio_layer, admin_login):
    """The panel's Apply starts disabled; studio:promote enables it only for
    a review with no conflict, readable or not (the counts it reads)."""
    main_row, _override, _wip = wip_with_changes(admin_login)
    theme = make_stylesheet("acme-theme", [("array", "row")], context="acme")
    create("ui-stylesheets", stylesheet(name="acme-theme", applies_to="acme", **WIP))
    ResourceRegistration.update(
        {**theme.content, "order": 1}, theme.resource_type.slug, theme.pk
    )
    # May read the source's items and the target's components, not acme's
    # stylesheets: its only conflict is on an item it can't read.
    user = pusher(
        **{
            "": {"http.GET": [UI_COMPONENT_TYPE_URI]},
            "alice": {"http.GET": [UI_COMPONENT_TYPE_URI, ITEM_KINDS["stylesheet"]]},
        }
    )
    Given.http.force_authenticate(user)
    page = get_page(workbench_url(stored_branch("wip")))
    box = promote_boxes(page)["main"]
    apply = box_button(page, box, "studio-apply")
    assert apply is not None and "disabled" in apply.attrs
    found = preview_of(load_review(box))
    assert counts(found) == {
        "changes": "2",
        "conflicts": "0",
        "unreadable-changes": "1",
        "unreadable-conflicts": "1",
    }
    # The server refuses it alike, and writes nothing.
    before = stored_state()
    press(apply, 409)
    assert stored_state() == before
    # A reader of acme's stylesheets sees the conflict; with main's row
    # changed too, both.
    grant(user, {"http.GET": [ITEM_KINDS["stylesheet"]]}, "acme")
    change_main_row(main_row)
    found = preview_of(load_review(box))
    assert counts(found)["conflicts"] == "2"
    assert counts(found)["unreadable-conflicts"] == "0"
    conflicts = [
        text_of(e)
        for e in load_review(box).elements
        if "studio-push-conflict" in e.classes()
    ]
    assert [c.split(":")[0] for c in conflicts] == [
        "component row",
        "stylesheet acme-theme (applies to acme)",
    ]


@pytest.mark.django_db
@test_context
def test_unreadable_counts_shown(studio_layer, admin_login):
    Given.http.force_authenticate(admin_login)
    reverse("ui-components-list")
    make_component("row", "<b>main</b>")
    make_wip(admin_login)
    create("ui-components", component(name="row", template="<i>wip</i>", **WIP))
    wip = stored_branch("wip")
    # May GET wip, and components in main only: the write is counted, not
    # shown.
    user = assign_ui_role((), "")
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    grant(user, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "")
    Given.http.force_authenticate(user)
    page = get_page(workbench_url(wip))
    # No grant to push: the panel offers the review only.
    box = promote_boxes(page)["main"]
    assert box_button(page, box, "studio-apply") is None
    assert box_button(page, box, "studio-propose") is None
    assert page.writes() == []
    loaded = load_review(box)
    assert counts(preview_of(loaded)) == {
        "changes": "0",
        "conflicts": "0",
        "unreadable-changes": "1",
        "unreadable-conflicts": "0",
    }
    [note] = loaded.having("data-studio-unreadable")
    assert text_of(note) == "1 change / 0 conflicts on items you can't read"
    assert not [e for e in loaded.elements if "studio-push-change" in e.classes()]
    # Read the source's components: the change shows, nothing is counted.
    grant(user, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "alice")
    loaded = load_review(box)
    assert counts(preview_of(loaded))["unreadable-changes"] == "0"
    assert not loaded.having("data-studio-unreadable")


@pytest.mark.django_db
@test_context
def test_promote_to_ancestor_beyond_parent(studio_layer, admin_login):
    Given.http.force_authenticate(admin_login)
    main_row = make_component("row", "<b>main</b>")
    make_wip(admin_login, "team")
    make_wip(admin_login, "mine", parent="team")
    create(
        "ui-components",
        component(
            name="row", template="<i>mine</i>", branch="mine", rbac_context="alice"
        ),
    )
    mine = stored_branch("mine")
    page = get_page(workbench_url(mine))
    # One panel per entry: the parent first, then the ancestors.
    boxes = promote_boxes(page)
    assert list(boxes) == ["team", "main"]
    openers = [text_of(e) for e in page.having("data-studio-promote-open")]
    assert openers == ["Promote to team…", "Promote to main…"]
    ids = [e.attrs["id"] for e in page.having("data-studio-promote-panel")]
    assert len(set(ids)) == 2
    for target, box in boxes.items():
        assert box.attrs["data-studio-promote-src"] == reverse(
            "ui-push-previews-detail", args=["mine"]
        )
        for cls, state in (("studio-apply", "applied"), ("studio-propose", "proposed")):
            body = json.loads(box_button(page, box, cls).attrs["data-scrud-body"])
            assert body == {
                "source": "mine",
                "target": target,
                "state": state,
                "rbac_context": "" if target == "main" else "alice",
            }
    found = preview_of(load_review(boxes["main"]))
    assert found.attrs["data-studio-target"] == "main"
    assert counts(found)["changes"] == "1" and counts(found)["conflicts"] == "0"
    # Straight to main: main changes, team does not, mine is emptied.
    press(box_button(page, boxes["main"], "studio-apply"), 201)
    main_row.refresh_from_db()
    assert main_row.content["template"] == "<i>mine</i>"
    assert own_rows(UI_COMPONENT_TYPE_URI, "team") == []
    assert own_rows(UI_COMPONENT_TYPE_URI, "mine") == []


@pytest.mark.django_db
@test_context
def test_published_T_has_no_promote(studio_layer, admin_login):
    _main_row, _override, _wip = wip_with_changes(admin_login)
    shop = make_branch("shop", "", parent=None, prefix="/shop/")
    page = get_page(workbench_url(shop))
    assert page.one("data-studio-workbench").attrs["data-studio-published"] == "true"
    assert not page.having("data-studio-promote")
    assert not [b for b in page.buttons() if b.startswith(PROMOTE)]
    # The only write control is Task 8's "New branch" (a branch under a
    # published one is normal); it pushes nothing.
    [new_branch] = page.writes()
    assert "data-studio-new-branch" in new_branch.attrs
    assert new_branch.attrs["hx-post"] == reverse("ui-branches-list")
