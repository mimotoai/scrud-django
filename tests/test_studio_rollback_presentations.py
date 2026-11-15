"""Roll back and presentations in the self-hosted studio: the strip's version
history (each version linked to its page, ``studio:version``, opened in a
new tab, with "Restore to version N" from ``ui-branch-status``'s ``restore``
action), the workbench's "Branch settings" panel (T's own ``ui-branches``
resource embedded in the scope ``studio studio-branch``: ``studio:branch-form``
for a user who may PUT it, else ``studio:branch-view``) and its "New branch"
form. Every write goes through the API with the exact body the page renders
(a fixed body as it is; a frame's PUT: its ``scrud-content`` with the inputs
it names applied; a ``data-scrud-json="form"`` element: `form_body`, as
scrud-ui.js builds it). These tests serve the API at ``/api/``
(``tests.mounted_urls``), with the studio layer installed (the
``studio_layer`` fixture)."""

import json

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.ui.branches import (
    UI_BRANCH_TYPE_URI,
    UI_BRANCH_VERSION_TYPE_URI,
    UI_PUSH_TYPE_URI,
)
from scrud_django.ui.layers import layer_item
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import assign_ui_role
from .steps import *  # noqa: F403, F401
from .test_studio_editors import content_of
from .test_studio_promote import get_page, press, text_of
from .test_studio_workbench import Page, workbench_url
from .test_ui_branch_push import (
    created,
    make_wip,
    own_rows,
    stored_branch,
    wip_overrides_row,
)
from .test_ui_branch_types import make_branch
from .test_ui_branch_versions import versions_of
from .test_ui_http import HTML, grant, make_component

JSON = "application/json"
UIBRANCH = "http://api.openteams.com/json-ld/UIBranch"
UIBRANCH_VERSION = "http://api.openteams.com/json-ld/UIBranchVersion"
BRANCH_SCOPE = "studio studio-branch"
PUBLISH_SCOPE = "studio studio-branch studio-publish"
PUBLISHER_MESSAGE = (
    "Setting, changing or removing a branch's prefix (or deleting a published "
    "branch) changes the site's URLs: it needs PUT on UI branches in the "
    'context "".'
)


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def branch_href(branch):
    return reverse("ui-branches-detail", args=[branch.pk])


def load_settings(branch, scope=BRANCH_SCOPE, view=False, status=200):
    """The branch settings pane as the workbench (re)loads it: an htmx GET
    of T's own resource in `scope` (view mode with `view`)."""
    headers = {"HTTP_HX_REQUEST": "true", "HTTP_SCRUD_UI_SCOPE": scope}
    if view:
        headers["HTTP_SCRUD_UI_MODE"] = "view"
    return get_page(branch_href(branch), status=status, **headers)


def settings_frame(page, href):
    """The frame of the branch at `href` that its settings render: on a
    workbench, the one embedded in the settings pane (the workbench is the
    same resource's frame too); in the pane's own answer, the only one."""
    frames = [
        e
        for e in page.having("data-scrud-href")
        if e.attrs["data-scrud-href"] == href and "scrud-resource" in e.classes()
    ]
    panes = [
        e
        for e in page.having("data-studio-embed")
        if e.attrs["data-studio-embed"] == "branch"
    ]
    if panes:
        frames = [f for f in frames if f.within(panes[0])]
    [frame] = frames
    return frame


def named(page, frame):
    """``{name: element}`` of the inputs, selects and textareas named by a
    JSON Pointer whose own frame is `frame` (what its PUT applies)."""
    found = {}
    for e in page.elements:
        name = e.attrs.get("name", "")
        if e.tag in ("input", "select", "textarea") and name.startswith("/"):
            frames = [f for f in page.having("data-scrud-href") if e.within(f)]
            if frames and frames[-1] is frame:
                found[name] = e
    return found


def frame_put(page, href, values, status):
    """The frame's PUT as scrud-ui.js sends it: its ``scrud-content`` with
    `values` applied at the pointers of inputs the frame renders, with its
    ``If-Match``, scope and pointer."""
    frame = settings_frame(page, href)
    inputs = named(page, frame)
    body = content_of(page, frame)
    for name, value in values.items():
        assert name in inputs, (name, sorted(inputs))
        body[name.lstrip("/")] = value
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
    Then.http.status_code_is(status)
    return get_page_text()


def get_page_text():
    return Page(Http.response.content.decode())


def no_inline_script(page):
    """No inline handler, and no script but the page's files and the
    frames' JSON content (a full page)."""
    for e in page.elements:
        assert not [a for a in e.attrs if a.startswith("on")], e.attrs
        if e.tag == "script" and "src" not in e.attrs:
            assert e.attrs.get("type") == JSON and "scrud-content" in e.classes()


# scrud-ui.js's text-like input types (TEXT_LIKE).
TEXT_LIKE = ("text", "textarea", "url", "date", "number")


def form_inputs(page, form):
    """The JSON-Pointer-named inputs of `form` (a ``data-scrud-json="form"``
    element), as scrud-ui.js's jsonFormFields reads them: ``{name: (type,
    value)}``, the type being ``data-scrud-type`` when set."""
    return {
        e.attrs["name"]: (
            e.attrs.get("data-scrud-type") or e.attrs.get("type", "text"),
            e.attrs.get("value", ""),
        )
        for e in page.elements
        if e.tag == "input"
        and e.within(form)
        and e.attrs.get("name", "").startswith("/")
        and "disabled" not in e.attrs
    }


def form_body(inputs, **typed):
    """The body scrud-ui.js's buildBody builds from `inputs` (`form_inputs`)
    onto ``{}``, with `typed` (``{property: value}``) typed into the text
    inputs: a text-like input left empty is over a missing value, so it is
    "untouched" and left out; any other input sends its value (only text
    values here)."""
    body = {}
    for name, (kind, value) in inputs.items():
        value = typed.get(name[1:], value)
        if kind in TEXT_LIKE and value == "":
            continue
        body[name[1:]] = value
    return body


def version_links(page):
    status = page.one("data-studio-status")
    return [
        e
        for e in page.having("href", within=status)
        if "studio-version-link" in e.classes()
    ]


def publisher(context="alice"):
    """May read and write branches in `context` and publish them: PUT on
    branches in "" (the site's grant)."""
    user = assign_ui_role((UI_BRANCH_TYPE_URI,), context)
    grant(user, {"http.PUT": [UI_BRANCH_TYPE_URI]}, "")
    return user


@pytest.mark.django_db
def test_rollback_rules_and_components(studio_layer):
    _etag, sheet = layer_item("stylesheet", ("studio:studio", ""))
    rules = [(r["select"], r["component"]) for r in sheet["rules"]]
    assert (f"studio {UIBRANCH_VERSION}", "studio:version") in rules
    # The settings: edit first, then the view twin (which wins in view mode).
    edit = rules.index((f"studio studio-branch {UIBRANCH}", "studio:branch-form"))
    view = rules.index((f"studio studio-branch {UIBRANCH}", "studio:branch-view"))
    assert edit < view
    assert (
        "studio studio-branch studio-branch-prefix string",
        "studio:branch-prefix",
    ) in rules
    assert (
        f"studio studio-new-branch {UIBRANCH}",
        "studio:branch-created",
    ) in rules
    for name, mode in (
        ("studio:version", "view"),
        ("studio:branch-form", "edit"),
        ("studio:branch-view", "view"),
        ("studio:branch-prefix", "edit"),
        ("studio:branch-created", "view"),
    ):
        _etag, found = layer_item("component", (name,))
        assert found["engine"] == "jinja2" and found["mode"] == mode, name
    _etag, form = layer_item("component", ("studio:branch-form",))
    assert form["scopes"] == {"prefix": "studio-branch-prefix"}


@pytest.mark.django_db
@test_context
def test_restore_from_strip_round_trip(studio_layer, admin_login):
    _main_row, override = wip_overrides_row(admin_login)
    wip = stored_branch("wip")
    kept = versions_of("wip")[-1]  # wip's override of row is "<i>wip</i>"
    When.http.put(
        reverse("ui-components-detail", args=[override.pk]),
        data={**override.content, "template": "<i>bad</i>"},
        format="json",
        HTTP_IF_MATCH=f'"{override.etag}"',
    )
    Then.http.status_code_is(200)
    assert versions_of("wip")[-1] == kept + 1
    # The strip's history: the latest versions, newest first, each linked to
    # its page in the scope studio, in view mode, with its restore.
    page = get_page(workbench_url(wip))
    entries = [e for e in page.elements if "studio-version-entry" in e.classes()]
    assert [e.attrs["data-studio-version-number"] for e in entries[:2]] == [
        str(kept + 1),
        str(kept),
    ]
    links = version_links(page)
    assert [text_of(link) for link in links][:2] == [
        f"Version {kept + 1}",
        f"Version {kept}",
    ]
    [kept_version] = [
        r
        for r in Resource.objects.filter(
            resource_type__type_uri=UI_BRANCH_VERSION_TYPE_URI,
            content__branch="wip",
        )
        if r.content["number"] == kept
    ]
    href = reverse("ui-branch-versions-detail", args=[kept_version.pk])
    assert links[1].attrs["href"] == href + "?ui-scope=studio&ui-mode=view"
    # Opened in a new tab: the workbench stays open.
    for link in links:
        assert link.attrs["target"] == "_blank" and link.attrs["rel"] == "noopener"
    [button] = restore_buttons(page, entries[1])
    assert text_of(button) == f"Restore to version {kept}"
    assert button.attrs["hx-post"] == reverse("ui-pushes-list")
    assert button.attrs["hx-ext"] == "scrud-json"
    assert button.attrs["hx-swap"] == "none"
    assert button.attrs["data-scrud-json"] == "fixed"
    assert button.attrs["data-scrud-etag"] == ""
    assert button.attrs["hx-confirm"].startswith(f"Restore wip to version {kept}?")
    assert json.loads(button.attrs["data-scrud-body"]) == {
        "source": None,
        "target": "wip",
        "restore": kept,
        "state": "applied",
        "rbac_context": "alice",
    }
    # The version's page: its items, as text, and no write control.
    version_page = get_page(links[1].attrs["href"])
    root = version_page.one("data-studio-version")
    assert root.attrs["data-studio-branch"] == "wip"
    assert root.attrs["data-studio-number"] == str(kept)
    assert text_of(version_page.one("data-studio-version-title")) == (
        f"Version {kept} of wip"
    )
    [item] = [
        e
        for e in version_page.having("data-studio-version-item")
        if e.attrs["data-studio-version-item"] == "row"
    ]
    assert "<i>wip</i>" in item.text and "<i>bad</i>" not in item.text
    assert version_page.writes() == []
    no_inline_script(version_page)
    # Sent as the strip sends it: a restore push, applied.
    press(button, 201)
    restore = created()
    assert restore.content["source"] is None
    assert restore.content["restore"] == kept
    assert restore.content["result"] == {"version": kept + 2}
    # wip's own items are those of the version again; history is kept.
    [row] = own_rows(UI_COMPONENT_TYPE_URI, "wip")
    assert row["template"] == "<i>wip</i>"
    assert versions_of("wip")[-1] == kept + 2
    # The strip, fetched again, lists the new version and the restore.
    page = get_page(workbench_url(wip))
    assert text_of(version_links(page)[0]) == f"Version {kept + 2}"
    pushes = [e for e in page.elements if "studio-push-link" in e.classes()]
    assert [text_of(p) for p in pushes] == ["restore → wip"]


def restore_buttons(page, within=None):
    return [
        e
        for e in page.writes()
        if "studio-restore" in e.classes() and (within is None or e.within(within))
    ]


@pytest.mark.django_db
@test_context
def test_restore_needs_the_post_grant(studio_layer, admin_login):
    """A version's restore is offered only to a user who may POST it: one
    who may write wip (in "alice") but not create pushes there sees the
    history without it; with the grant, it is offered and works."""
    _main_row, override = wip_overrides_row(admin_login)
    wip = stored_branch("wip")
    kept = versions_of("wip")[-1]
    ResourceRegistration.update(
        {**override.content, "template": "<i>bad</i>"}, "ui-components", override.pk
    )
    writer = assign_ui_role(
        (UI_BRANCH_TYPE_URI, UI_BRANCH_VERSION_TYPE_URI, UI_COMPONENT_TYPE_URI),
        "alice",
    )
    Given.http.force_authenticate(writer)
    page = get_page(workbench_url(wip))
    assert version_links(page)
    assert restore_buttons(page) == []
    grant(writer, {"http.POST": [UI_PUSH_TYPE_URI]}, "alice")
    page = get_page(workbench_url(wip))
    [button] = [
        b
        for b in restore_buttons(page)
        if json.loads(b.attrs["data-scrud-body"])["restore"] == kept
    ]
    press(button, 201)
    [row] = own_rows(UI_COMPONENT_TYPE_URI, "wip")
    assert row["template"] == "<i>wip</i>"


@pytest.mark.django_db
@test_context
def test_published_T_offers_restore_to_a_user_with_the_grant(studio_layer, admin_login):
    """Rolling back a publish is what a restore is for: a published T's
    history offers it (it writes a push record, as a promote does), only to
    a user who may POST it in T's context."""
    main_row = make_component("row", "<b>first</b>")
    first = versions_of("main")[-1]
    ResourceRegistration.update(
        {**main_row.content, "template": "<b>broken</b>"}, "ui-components", main_row.pk
    )
    assert versions_of("main")[-1] == first + 1
    main = make_branch("main", "", parent=None)
    # A reader of main's versions: the history, no restore.
    reader = assign_ui_role((), "")
    grant(reader, {"http.GET": [UI_BRANCH_TYPE_URI, UI_BRANCH_VERSION_TYPE_URI]}, "")
    Given.http.force_authenticate(reader)
    page = get_page(workbench_url(main))
    assert page.one("data-studio-workbench").attrs["data-studio-published"] == "true"
    assert version_links(page) and restore_buttons(page) == []
    # A user who may push into main (in ""), with the item grants a restore
    # needs.
    user = assign_ui_role(
        (UI_PUSH_TYPE_URI, UI_BRANCH_VERSION_TYPE_URI, UI_COMPONENT_TYPE_URI), ""
    )
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI]}, "")
    Given.http.force_authenticate(user)
    page = get_page(workbench_url(main))
    [button] = [
        b
        for b in restore_buttons(page)
        if json.loads(b.attrs["data-scrud-body"])["restore"] == first
    ]
    assert json.loads(button.attrs["data-scrud-body"]) == {
        "source": None,
        "target": "main",
        "restore": first,
        "state": "applied",
        "rbac_context": "",
    }
    press(button, 201)
    main_row.refresh_from_db()
    assert main_row.content["template"] == "<b>first</b>"
    assert created().content["result"] == {"version": first + 2}


@pytest.mark.django_db
@test_context
def test_branch_settings_panel_saves_fields(studio_layer, admin_login):
    make_wip(admin_login)
    wip = stored_branch("wip")
    page = get_page(workbench_url(wip))
    # The workbench embeds T itself in the scope studio studio-branch.
    [pane] = [
        e
        for e in page.having("data-studio-embed")
        if e.attrs["data-studio-embed"] == "branch"
    ]
    assert pane.attrs["data-studio-src"] == branch_href(wip)
    assert pane.attrs["data-studio-scope"] == BRANCH_SCOPE
    assert "data-studio-mode" not in pane.attrs
    assert pane.within(page.one("data-studio-branch-panel"))
    frame = settings_frame(page, branch_href(wip))
    assert frame.within(pane) and frame.tag == "form"
    assert frame.attrs["data-scrud-scope"] == BRANCH_SCOPE
    [settings] = page.having("data-studio-branch-settings")
    assert settings.attrs["data-studio-mode"] == "edit"
    inputs = named(page, frame)
    # Label, description and base; no prefix without the site grant's scope.
    assert sorted(inputs) == ["/base", "/description", "/label"]
    assert inputs["/base"].tag == "select"
    assert inputs["/description"].tag == "textarea"
    assert text_of(page.one("data-studio-branch-prefix-text")) == (
        "None: a working branch"
    )
    saves = [
        e
        for e in page.elements
        if e.tag == "button" and e.attrs.get("type") == "submit" and e.within(frame)
    ]
    assert [text_of(s) for s in saves] == ["Save"]
    no_inline_script(page)
    answer = frame_put(
        page,
        branch_href(wip),
        {
            "/label": "Work in progress",
            "/description": "Try a theme",
            "/base": "pinned",
        },
        200,
    )
    wip.refresh_from_db()
    assert wip.content["label"] == "Work in progress"
    assert wip.content["description"] == "Try a theme"
    assert wip.content["base"] == "pinned"
    assert "prefix" not in wip.content
    # The answer is the settings again, in their scope.
    assert answer.one("data-studio-branch-settings")
    # A user who may only GET T gets the view twin: text, no form.
    reader = assign_ui_role((), "")
    grant(reader, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    Given.http.force_authenticate(reader)
    page = load_settings(wip)
    assert page.one("data-studio-branch-settings").attrs["data-studio-mode"] == "view"
    assert page.writes() == []
    assert not [e for e in page.elements if "name" in e.attrs]
    assert "Work in progress" in page.one("data-studio-branch-settings").text


def branch_facts(page):
    """``{term: text}`` of the branch settings' facts list."""
    [facts] = [e for e in page.elements if "studio-branch-facts" in e.classes()]
    shown = [e for e in page.elements if e.tag in ("dt", "dd") and e.within(facts)]
    return {
        text_of(term): text_of(value)
        for term, value in zip(shown[::2], shown[1::2], strict=True)
    }


@pytest.mark.django_db
@test_context
def test_branch_settings_without_optional_fields_show_a_dash(studio_layer, admin_login):
    """A branch's unset parent, label and description showed the text
    "None" in its settings: they show an em dash, in the view twin and in
    the form's text of a published branch (main, which has no parent)."""
    make_wip(admin_login)
    wip = stored_branch("wip")
    assert not wip.content.get("label") and not wip.content.get("description")
    reader = assign_ui_role((), "")
    grant(reader, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    grant(reader, {"http.GET": [UI_BRANCH_TYPE_URI]}, "")
    Given.http.force_authenticate(reader)
    page = load_settings(wip)
    assert page.one("data-studio-branch-settings").attrs["data-studio-mode"] == "view"
    facts = branch_facts(page)
    assert facts["Parent"] == "main"
    assert facts["Label"] == "\u2014" and facts["Description"] == "\u2014"
    main = stored_branch("main")
    assert not main.content.get("parent")
    page = load_settings(main)
    facts = branch_facts(page)
    assert facts["Parent"] == "\u2014" and facts["Label"] == "\u2014"
    assert facts["Description"] == "\u2014"
    Given.http.force_authenticate(admin_login)
    page = load_settings(main)
    assert page.one("data-studio-branch-settings").attrs["data-studio-mode"] == "edit"
    facts = branch_facts(page)
    assert facts["Parent"] == "\u2014" and facts["Label"] == "\u2014"
    assert facts["Description"] == "\u2014"
    assert "None" not in facts.values()


@pytest.mark.django_db
@test_context
def test_prefix_input_only_with_site_grant(studio_layer, ui_alice_login):
    reverse("todo-lists-list")  # the URLconf registers the types
    own = make_branch("own", "alice")
    # alice may PUT her branch but not publish: the strip says so, and the
    # settings (as embedded) have no prefix input.
    Given.http.force_authenticate(ui_alice_login)
    page = get_page(workbench_url(own))
    status = page.one("data-studio-status")
    assert status.attrs["data-studio-may-publish"] == "false"
    assert status.attrs["data-studio-published"] == "false"
    frame = settings_frame(page, branch_href(own))
    assert "/prefix" not in named(page, frame)
    assert page.having("data-studio-branch-prefix-text")
    # A crafted request in the publisher's scope renders the input (a
    # scope is presentation only), but the save is refused with the API's
    # answer and nothing is written.
    page = load_settings(own, PUBLISH_SCOPE)
    frame = settings_frame(page, branch_href(own))
    assert "/prefix" in named(page, frame)
    refused = frame_put(page, branch_href(own), {"/prefix": "/own/"}, 403)
    assert any(PUBLISHER_MESSAGE in e.text for e in refused.elements)
    own.refresh_from_db()
    assert "prefix" not in own.content
    # The same PUT as JSON: the API's own answer.
    When.http.put(
        branch_href(own),
        data={**own.content, "prefix": "/own/"},
        format="json",
        HTTP_IF_MATCH=f'"{own.etag}"',
    )
    Then.http.status_code_is(403)
    assert Http.response.json()["detail"] == PUBLISHER_MESSAGE
    # A publisher: the strip says so, the settings in the publisher's scope
    # have the input, and the save publishes the branch.
    Given.http.force_authenticate(publisher())
    page = get_page(workbench_url(own))
    assert page.one("data-studio-status").attrs["data-studio-may-publish"] == "true"
    page = load_settings(own, PUBLISH_SCOPE)
    frame = settings_frame(page, branch_href(own))
    assert frame.attrs["data-scrud-scope"] == PUBLISH_SCOPE
    prefix = named(page, frame)["/prefix"]
    assert prefix.tag == "input" and prefix.attrs["value"] == ""
    frame_put(page, branch_href(own), {"/prefix": "/own/"}, 200)
    own.refresh_from_db()
    assert own.content["prefix"] == "/own/"
    # Published now: the workbench embeds its settings in view mode (the
    # script asks again in the publisher's scope for a publisher), where
    # the prefix is changed or removed; label, description and base are
    # text (a published T is read-only in the studio).
    page = get_page(workbench_url(own))
    [pane] = [
        e
        for e in page.having("data-studio-embed")
        if e.attrs["data-studio-embed"] == "branch"
    ]
    assert pane.attrs["data-studio-mode"] == "view"
    assert settings_frame(page, branch_href(own)).tag == "div"
    page = load_settings(own, PUBLISH_SCOPE)
    frame = settings_frame(page, branch_href(own))
    assert sorted(named(page, frame)) == ["/prefix"]
    assert named(page, frame)["/prefix"].attrs["value"] == "/own/"
    [remove] = [w for w in page.writes() if "studio-unpublish" in w.classes()]
    assert text_of(remove) == "Remove the prefix"
    assert remove.attrs["hx-put"] == branch_href(own)
    assert remove.attrs["hx-swap"] == "none"
    assert json.loads(remove.attrs["data-scrud-body"]) == {
        **content_of(page, frame),
        "prefix": None,
    }
    # Sent with the frame's ETag (the request goes to the frame's URL).
    When.http.put(
        remove.attrs["hx-put"],
        data=remove.attrs["data-scrud-body"],
        content_type=JSON,
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
        HTTP_IF_MATCH=frame.attrs["data-scrud-etag"],
    )
    Then.http.status_code_is(200)
    own.refresh_from_db()
    assert own.content.get("prefix") is None
    # Without the grant, a published branch's prefix is text.
    shop = make_branch("shop", "alice", parent=None, prefix="/shop/")
    Given.http.force_authenticate(ui_alice_login)
    page = load_settings(shop)
    frame = settings_frame(page, branch_href(shop))
    assert named(page, frame) == {}
    assert text_of(page.one("data-studio-branch-prefix-text")) == "/shop/"
    assert not [w for w in page.writes() if "studio-unpublish" in w.classes()]


@pytest.mark.django_db
@test_context
def test_overlapping_prefix_error_beside_field(studio_layer, admin_login):
    reverse("todo-lists-list")  # the URLconf registers the types
    make_branch("shop", "", parent=None, prefix="/shop/")
    own = make_branch("own", "alice")
    Given.http.force_authenticate(admin_login)
    page = load_settings(own, PUBLISH_SCOPE)
    refused = frame_put(
        page, branch_href(own), {"/prefix": "/shop/todo-lists/", "/label": "Mine"}, 400
    )
    own.refresh_from_db()
    assert "prefix" not in own.content and "label" not in own.content
    # The submitted body again, in the publisher's scope, with the message
    # beside the prefix field.
    frame = settings_frame(refused, branch_href(own))
    assert frame.attrs["data-scrud-scope"] == PUBLISH_SCOPE
    prefix = named(refused, frame)["/prefix"]
    assert prefix.attrs["value"] == "/shop/todo-lists/"
    assert prefix.attrs["aria-invalid"] == "true"
    [errors] = [
        e
        for e in refused.elements
        if e.attrs.get("id") == prefix.attrs["aria-describedby"]
    ]
    assert "studio-field-errors" in errors.classes()
    assert "/shop/" in errors.text and "shop" in errors.text
    # The API's answer names the field.
    When.http.put(
        branch_href(own),
        data={**own.content, "prefix": "/shop/todo-lists/"},
        format="json",
        HTTP_IF_MATCH=f'"{own.etag}"',
    )
    Then.http.status_code_is(400)
    problem = Http.response.json()
    assert problem["name"] == "prefix"
    assert problem["detail"] in errors.text
    # A taken prefix too.
    When.http.put(
        branch_href(own),
        data={**own.content, "prefix": "/shop/"},
        format="json",
        HTTP_IF_MATCH=f'"{own.etag}"',
    )
    Then.http.status_code_is(400)
    assert Http.response.json()["name"] == "prefix"


@pytest.mark.django_db
@test_context
def test_new_branch_form_creates_working_branch(studio_layer, admin_login):
    make_wip(admin_login)
    wip = stored_branch("wip")
    page = get_page(workbench_url(wip))
    [form] = page.having("data-studio-new-branch")
    assert form.tag == "form"
    assert form.attrs["hx-post"] == reverse("ui-branches-list")
    assert form.attrs["hx-ext"] == "scrud-json"
    assert form.attrs["data-scrud-json"] == "form"
    assert form.attrs["data-scrud-etag"] == ""
    assert form.attrs["hx-target"] == "next [data-studio-new-branch-result]"
    headers = json.loads(form.attrs["hx-headers"])
    assert headers == {
        "Scrud-UI-Scope": "studio studio-new-branch",
        "Scrud-UI-Mode": "view",
    }
    # Outside the settings' frame: its inputs are not the branch's.
    assert not form.within(settings_frame(page, branch_href(wip)))
    inputs = form_inputs(page, form)
    # The context is sent even when it is "" (main's, the site's): a
    # non-text-like type, so an empty value is no "untouched" field.
    assert inputs == {
        "/name": ("text", ""),
        "/parent": ("text", "wip"),
        "/rbac_context": ("string", "alice"),
    }
    # The body scrud-json builds from them, onto {}.
    body = form_body(inputs, name="wip-2")
    assert body == {"name": "wip-2", "parent": "wip", "rbac_context": "alice"}
    request_headers = {
        "HTTP_" + k.upper().replace("-", "_"): v for k, v in headers.items()
    }
    When.http.post(
        form.attrs["hx-post"],
        data=json.dumps(body),
        content_type=JSON,
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
        **request_headers,
    )
    Then.http.status_code_is(201)
    answer = get_page_text()
    made = stored_branch("wip-2")
    assert made.content["parent"] == "wip"
    assert made.content["rbac_context"] == "alice"
    assert "prefix" not in made.content
    # The answer links the new branch's workbench, which works.
    [link] = [e for e in answer.elements if "studio-created-link" in e.classes()]
    assert link.attrs["href"] == workbench_url(made)
    page = get_page(link.attrs["href"])
    workbench = page.one("data-studio-workbench")
    assert workbench.attrs["data-studio-branch"] == "wip-2"
    assert workbench.attrs["data-studio-published"] == "false"
    # A taken name is refused with the API's answer.
    When.http.post(
        form.attrs["hx-post"],
        data=json.dumps(body),
        content_type=JSON,
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
        **request_headers,
    )
    Then.http.status_code_is(400)
    assert "already exists" in Http.response.content.decode()


@pytest.mark.django_db
@test_context
def test_new_branch_from_main(studio_layer, admin_login):
    """main is in the context "": its New branch form sends that empty
    context (scrud-ui.js would leave an empty text input out, and the
    schema requires ``rbac_context``)."""
    main = make_branch("main", "", parent=None)
    Given.http.force_authenticate(admin_login)
    page = get_page(workbench_url(main))
    [form] = page.having("data-studio-new-branch")
    inputs = form_inputs(page, form)
    assert inputs["/rbac_context"] == ("string", "")
    assert inputs["/parent"] == ("text", "main")
    body = form_body(inputs, name="feature")
    assert body == {"name": "feature", "parent": "main", "rbac_context": ""}
    When.http.post(
        form.attrs["hx-post"],
        data=json.dumps(body),
        content_type=JSON,
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(201)
    made = stored_branch("feature")
    assert made.content["parent"] == "main"
    assert made.content["rbac_context"] == ""


@pytest.mark.django_db
@test_context
def test_new_branch_absent_for_reader(studio_layer, admin_login):
    make_wip(admin_login)
    reader = assign_ui_role((), "")
    grant(reader, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    Given.http.force_authenticate(reader)
    page = get_page(workbench_url(stored_branch("wip")))
    assert not page.having("data-studio-new-branch")
    assert page.writes() == []
