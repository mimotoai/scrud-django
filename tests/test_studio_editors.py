"""The self-hosted studio's item editors: a ``UIComponent``, ``UICss`` or
``UIJs`` resource rendered in the scope ``studio studio-editor``, which the
workbench's script loads into its Editor pane from the item's (mounted)
``href``. In edit mode the editor is the item's own frame ``<form>``: Save is
its submit (the runtime's PUT with ``If-Match``) and the frame's Delete
button is the one control that deletes the branch's own row ("Revert to
inherited"). In view mode (a published T, an item T does not own, a user
who may not PUT) it is read-only. These tests serve the API at ``/api/``
and the bootstrap studio at ``/studio/`` (``tests.mounted_urls``), with the
studio layer installed (the ``studio_layer`` fixture), and S, the studio
root, at `STUDIO_ROOT`."""

import json
from urllib.parse import urlencode

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.ui.branches import UI_CSS_TYPE_URI, UI_JS_TYPE_URI
from scrud_django.ui.layers import layer_item
from scrud_django.ui.renderer import STALE_MESSAGE
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_studio_live_preview import errors_of, grant_items
from .test_studio_page import store_item
from .test_studio_workbench import STUDIO_ROOT, Page, setup_studio, workbench_url
from .test_ui_branch_css import make_css
from .test_ui_branch_js import make_js
from .test_ui_branch_types import make_branch
from .test_ui_http import HTML, grant, html

JSON = "application/json"
EDITOR_SCOPE = "studio studio-editor"
OT = "http://api.openteams.com/json-ld/"
EDITORS = {
    "UIComponent": ("studio:component-editor", "studio:component-view"),
    "UICss": ("studio:text-editor", "studio:text-view"),
    "UIJs": ("studio:text-editor", "studio:text-view"),
}
REVERT = "Delete"  # the frame's own button; studio.js relabels it


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def setup_editors(user):
    """`setup_studio`, plus main's jinja2 component ``card`` (inherited by
    alice-wip), alice-wip's own JS ``tools`` and own Django-engine component
    ``legacy``; `user` may also write CSS and JS in "alice"."""
    found = setup_studio(user)
    store_item(
        "ui-components",
        "main",
        "",
        name="card",
        engine="jinja2",
        mode="view",
        template="<p>{{ value }}</p>",
    )
    make_js("tools", "console.log('<tools>')", branch="alice-wip", context="alice")
    store_item(
        "ui-components",
        "alice-wip",
        "alice",
        name="legacy",
        engine="django",
        mode="view",
        template="<b>{{ value }}</b>",
    )
    grant_items(user, "alice", UI_CSS_TYPE_URI, UI_JS_TYPE_URI)
    return found


def workbench_entries(branch, user=None):
    """``{(kind, name): (classes, open, li, page)}`` of `branch`'s workbench
    at the studio root."""
    page = Page(get_page(workbench_url(branch, STUDIO_ROOT)))
    return {k: (*v, page) for k, v in page.entries().items()}


def open_editor(href, view=False, status=200):
    """What studio.js loads for an item: its `href` as an htmx fragment in
    the scope ``studio studio-editor``, in view mode when `view`."""
    headers = {"HTTP_HX_REQUEST": "true", "HTTP_SCRUD_UI_SCOPE": EDITOR_SCOPE}
    if view:
        headers["HTTP_SCRUD_UI_MODE"] = "view"
    return get_page(href, status=status, **headers)


def get_page(url, status=200, **headers):
    When.http.get(url, HTTP_ACCEPT=HTML, **headers)
    Then.http.status_code_is(status)
    return html()


def root_frame(page, href):
    [frame] = [
        e
        for e in page.having("data-scrud-href")
        if e.attrs["data-scrud-href"] == href and "scrud-resource" in e.classes()
    ]
    return frame


def content_of(page, frame):
    """The frame's own ``scrud-content`` JSON."""
    [script] = [
        e
        for e in page.elements
        if e.tag == "script" and "scrud-content" in e.classes() and e.parent is frame
    ]
    return json.loads(script.text)


def no_inline_code(page):
    for e in page.elements:
        assert not [a for a in e.attrs if a.startswith("on")], e.attrs
        if e.tag == "script":
            assert e.attrs.get("type") == JSON and "scrud-content" in e.classes()


def textarea_value(element):
    """A textarea's value as a browser reads it: one leading newline is
    dropped."""
    return element.text[1:] if element.text.startswith("\n") else element.text


def put_item(href, body, etag=None, html_mode=True, **headers):
    if etag is not None:
        headers["HTTP_IF_MATCH"] = etag
    if html_mode:
        headers.update(
            HTTP_ACCEPT=HTML,
            HTTP_HX_REQUEST="true",
            HTTP_SCRUD_UI_SCOPE=EDITOR_SCOPE,
            HTTP_SCRUD_UI_POINTER="",
        )
    else:
        headers.setdefault("HTTP_ACCEPT", JSON)
    When.http.put(href, data=json.dumps(body), content_type=JSON, **headers)
    return Http.response


def resource(kind_uri, name, branch):
    return [
        r
        for r in Resource.objects.filter(resource_type__type_uri=kind_uri)
        if r.content.get("name") == name and r.content.get("branch", "main") == branch
    ][0]


@pytest.mark.django_db
def test_editor_rules_and_components(studio_layer):
    _etag, sheet = layer_item("stylesheet", ("studio:studio", ""))
    rules = [(r["select"], r["component"]) for r in sheet["rules"]]
    for type_name, (edit, view) in EDITORS.items():
        select = f"studio studio-editor {OT}{type_name}"
        assert (select, edit) in rules and (select, view) in rules, type_name
    for name, mode in (
        ("studio:component-editor", "edit"),
        ("studio:component-view", "view"),
        ("studio:text-editor", "edit"),
        ("studio:text-view", "view"),
    ):
        _etag, component = layer_item("component", (name,))
        assert component["engine"] == "jinja2" and component["mode"] == mode, name


@pytest.mark.django_db
@test_context
def test_own_component_editor_has_save_and_revert(studio_layer, ui_alice_login):
    _studio, alice_wip, _bobs = setup_editors(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    entries = workbench_entries(alice_wip)
    for key, field, kind in (
        (("component", "row"), "/template", "component"),
        (("css", "site"), "/css", "css"),
        (("js", "tools"), "/js", "js"),
    ):
        classes, opened, _li, _page = entries[key]
        assert "studio-own" in classes, key
        href = opened["href"]
        assert href.startswith(STUDIO_ROOT), key
        page = Page(open_editor(href))
        frame = root_frame(page, href)
        # Edit mode: the item's own frame is the form; Save is its submit,
        # with the stored ETag as If-Match.
        assert frame.tag == "form", key
        assert frame.attrs["hx-put"] == href
        assert frame.attrs["data-scrud-scope"] == EDITOR_SCOPE
        When.http.get(href, HTTP_ACCEPT=JSON)
        assert frame.attrs["data-scrud-etag"] == Http.response["ETag"]
        stored = Http.response.json()
        content = content_of(page, frame)
        assert content == stored
        [editor] = page.having("data-studio-editor-item", within=frame)
        assert editor.attrs["data-studio-kind"] == kind
        assert editor.attrs["data-studio-mode"] == "edit"
        # One monospace textarea named by the field's pointer, with its text.
        named = [e for e in page.elements if "name" in e.attrs and e.within(frame)]
        assert [(e.tag, e.attrs["name"]) for e in named] == [("textarea", field)]
        [code] = named
        assert "studio-code" in code.classes() and "data-studio-code" in code.attrs
        assert textarea_value(code) == stored[field[1:]]
        submits = [
            e
            for e in page.elements
            if e.tag == "button" and e.attrs.get("type") == "submit"
        ]
        assert [s.text for s in submits] == ["Save"] and submits[0].within(frame)
        # Exactly one control deletes the own row (reverts to the inherited
        # item): the frame's Delete, with its confirmation, to the item.
        deletes = page.having("hx-delete")
        assert [d.attrs["hx-delete"] for d in deletes] == [href], key
        assert deletes[0].within(frame) and deletes[0].text == REVERT
        assert deletes[0].attrs["hx-confirm"]
        # No other write attribute; no inline code.
        writes = [e for e in page.writes() if e is not frame and e is not deletes[0]]
        assert writes == [], key
        no_inline_code(page)


@pytest.mark.django_db
@test_context
def test_inherited_item_view_mode_with_override_button(studio_layer, admin_login):
    """An item T does not own is opened in view mode (studio.js asks for
    it): read-only even for a user who may PUT and DELETE it; the editor
    pane offers the items tree's "Override on this branch" instead."""
    _studio, alice_wip, _bobs = setup_editors(admin_login)
    Given.http.force_authenticate(admin_login)
    entries = workbench_entries(alice_wip)
    classes, opened, li, tree = entries[("component", "card")]
    assert "studio-inherited" in classes and opened["href"]
    # The tree's override button: what the editor pane shows (a copy).
    [override] = [e for e in tree.writes() if e.within(li)]
    assert override.text == "Override on this branch"
    action_href = opened["override"]["href"]
    assert override.attrs["hx-post"] == STUDIO_ROOT + action_href[len("/api/") :]
    # The superuser would get edit mode; view mode is asked for.
    page = Page(open_editor(opened["href"]))
    assert root_frame(page, opened["href"]).tag == "form"
    page = Page(open_editor(opened["href"], view=True))
    frame = root_frame(page, opened["href"])
    assert frame.tag == "div"
    assert page.writes() == []
    assert not [e for e in page.elements if "name" in e.attrs]
    [editor] = page.having("data-studio-editor-item", within=frame)
    assert editor.attrs["data-studio-kind"] == "component"
    assert editor.attrs["data-studio-mode"] == "view"
    [code] = [e for e in page.elements if "studio-code" in e.classes()]
    assert "readonly" in code.attrs and "data-studio-code" not in code.attrs
    assert textarea_value(code) == "<p>{{ value }}</p>"
    assert "Save" not in [e.text for e in page.elements if e.tag == "button"]
    no_inline_code(page)
    # The CSS and JS view components.
    for key, text in ((("css", "site"), "p { color: green }"), (("js", "tools"), None)):
        _c, opened, _li, _p = entries[key]
        page = Page(open_editor(opened["href"], view=True))
        assert page.writes() == []
        [editor] = page.having("data-studio-editor-item")
        assert editor.attrs["data-studio-kind"] == key[0]
        [code] = [e for e in page.elements if "studio-code" in e.classes()]
        if text is not None:
            assert textarea_value(code) == text
        assert "name" not in code.attrs


@pytest.mark.django_db
@test_context
def test_override_then_save_round_trip(studio_layer, ui_alice_login):
    """The override is the tree's fixed body, sent unchanged: the server sets
    its ``base``. The new own row then opens in edit mode, and its save is
    the frame's PUT with the frame's ETag as ``If-Match``."""
    _studio, alice_wip, _bobs = setup_editors(ui_alice_login)
    card = resource(UI_COMPONENT_TYPE_URI, "card", "main")
    Given.http.force_authenticate(ui_alice_login)
    _classes, opened, li, tree = workbench_entries(alice_wip)[("component", "card")]
    [button] = [e for e in tree.writes() if e.within(li)]
    body = button.attrs["data-scrud-body"]
    assert "base" not in json.loads(body)
    # As scrud-json sends it: the body byte for byte, no If-Match (empty
    # data-scrud-etag), HTML accepted.
    assert button.attrs["data-scrud-etag"] == ""
    When.http.post(
        button.attrs["hx-post"],
        data=body,
        content_type=JSON,
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(201)
    own = resource(UI_COMPONENT_TYPE_URI, "card", "alice-wip")
    assert own.content["base"] == card.etag
    assert own.content["template"] == card.content["template"]
    # The refreshed tree: card is alice-wip's own, opened in edit mode.
    classes, opened, _li, _tree = workbench_entries(alice_wip)[("component", "card")]
    assert "studio-own" in classes and opened["override"] is None
    href = opened["href"]
    assert href == STUDIO_ROOT + f"ui-components/{own.pk}/"
    page = Page(open_editor(href))
    frame = root_frame(page, href)
    assert frame.tag == "form" and frame.attrs["hx-put"] == href
    etag = frame.attrs["data-scrud-etag"]
    assert etag == f'"{own.etag}"' or etag == own.etag
    # The frame's PUT: its content with the textarea applied, If-Match from
    # the frame. Without it the API asks for one (428).
    saved = {**content_of(page, frame), "template": "<p>saved {{ value }}</p>"}
    put_item(href, saved)
    assert Http.response.status_code == 428
    put_item(href, saved, etag=etag)
    assert Http.response.status_code == 200, Http.response.content
    own.refresh_from_db()
    assert own.content["template"] == "<p>saved {{ value }}</p>"
    assert own.content["base"] == card.etag
    # The response is the editor again, at the new ETag.
    page = Page(Http.response.content.decode())
    frame = root_frame(page, href)
    assert frame.tag == "form" and frame.attrs["data-scrud-etag"] != etag
    [code] = page.having("data-studio-code")
    assert textarea_value(code) == "<p>saved {{ value }}</p>"


def crafted_put(href, body, etag):
    """A PUT as JSON with `etag`: ``(status, body)``."""
    response = put_item(href, body, etag=etag, html_mode=False)
    return response.status_code, response.json()


@pytest.mark.django_db
@test_context
def test_published_T_editor_view_only(studio_layer, admin_login, ui_alice_login):
    """Review Focus 2: on a published T every editor is read-only, with no
    write control; a crafted save of an item on the published branch gets
    exactly what the API answers that user."""
    studio, _alice_wip, _bobs = setup_editors(ui_alice_login)
    main = make_branch("main", "", parent=None)
    Given.http.force_authenticate(admin_login)
    for branch in (studio, main):
        entries = workbench_entries(branch)
        hrefs = [o["href"] for _c, o, _li, _p in entries.values() if o["href"]]
        assert hrefs, branch
        page = next(iter(entries.values()))[3]
        workbench = page.one("data-studio-workbench")
        assert workbench.attrs["data-studio-published"] == "true"
        # studio.js opens every item of a published T in view mode: the
        # superuser, who may PUT and DELETE each, gets no write control.
        for href in hrefs:
            opened = Page(open_editor(href, view=True))
            assert opened.writes() == [], href
            assert not [e for e in opened.elements if "name" in e.attrs], href
            assert not [e for e in opened.elements if e.tag == "form"], href
            assert "Save" not in opened.buttons(), href
    # main's own row "row", read-only.
    entries = workbench_entries(main)
    _classes, row, _li, page = entries[("component", "row")]
    assert page.one("data-studio-workbench").attrs["data-studio-published"] == "true"
    opened = Page(open_editor(row["href"], view=True))
    assert opened.writes() == [] and "Save" not in opened.buttons()
    # A crafted save of main's row through the studio's mount: the API's own
    # answer, for a user who may not PUT it and for one who may.
    stored = resource(UI_COMPONENT_TYPE_URI, "row", "main")
    api_href = reverse("ui-components-detail", args=[stored.pk])
    assert row["href"] == STUDIO_ROOT + api_href[len("/api/") :]
    body = {**stored.content, "template": "<b>crafted</b>"}
    etag = f'"{stored.etag}"'
    grant(ui_alice_login, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "")
    Given.http.force_authenticate(ui_alice_login)
    refused = crafted_put(row["href"], body, etag)
    assert refused[0] == 403
    assert crafted_put(api_href, body, etag) == refused
    # As the editor's form would send it (HTML): refused alike.
    put_item(row["href"], body, etag=etag)
    assert Http.response.status_code == 403
    assert "crafted" not in Http.response.content.decode()
    Given.http.force_authenticate(admin_login)
    stale = crafted_put(row["href"], body, '"stale"')
    assert stale[0] == 412 and crafted_put(api_href, body, '"stale"') == stale
    assert resource(UI_COMPONENT_TYPE_URI, "row", "main").etag == stored.etag
    # One the API accepts is accepted: the server's checks decide.
    status, saved = crafted_put(row["href"], body, etag)
    assert status == 200 and saved["template"] == "<b>crafted</b>"
    stored = resource(UI_COMPONENT_TYPE_URI, "row", "main")
    again = {**stored.content, "template": "<b>again</b>"}
    status, saved = crafted_put(api_href, again, f'"{stored.etag}"')
    assert status == 200 and saved["template"] == "<b>again</b>"


@pytest.mark.django_db
@test_context
def test_django_engine_component_read_only(studio_layer, ui_alice_login):
    _studio, alice_wip, _bobs = setup_editors(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    entries = workbench_entries(alice_wip)
    classes, opened, _li, _page = entries[("component", "legacy")]
    assert "studio-own" in classes
    for view in (False, True):
        page = Page(open_editor(opened["href"], view=view))
        frame = root_frame(page, opened["href"])
        # alice may PUT it: edit mode unless view is asked for.
        assert frame.tag == ("div" if view else "form")
        [editor] = page.having("data-studio-editor-item", within=frame)
        assert editor.attrs["data-studio-mode"] == ("view" if view else "edit")
        [note] = [e for e in page.elements if "studio-note" in e.classes()]
        assert "Django" in note.text and "read-only" in note.text
        # The bootstrap studio shows them read-only too: only the API edits.
        assert "bootstrap studio" not in note.text and "the API" in note.text
        [code] = [e for e in page.elements if "studio-code" in e.classes()]
        assert code.tag == "textarea" and "readonly" in code.attrs
        assert "name" not in code.attrs and "data-studio-code" not in code.attrs
        assert textarea_value(code) == "<b>{{ value }}</b>"
        assert not [e for e in page.elements if "name" in e.attrs]
        assert "Save" not in page.buttons()
    # The inherited Django-engine component (main's head) alike.
    _classes, head, _li, _page = entries[("component", "head")]
    page = Page(open_editor(head["href"], view=True))
    [note] = [e for e in page.elements if "studio-note" in e.classes()]
    assert "Django" in note.text
    assert not [e for e in page.elements if "name" in e.attrs]


@pytest.mark.django_db
@test_context
def test_second_tab_save_gets_412_reload(studio_layer, ui_alice_login):
    """Review Focus 3: two tabs open the same own item; the first save wins,
    and the second, at the ETag it opened, gets the runtime's 412: a notice
    and a frame that reloads the current state in the editor's scope. A
    live draft is never stored."""
    _studio, alice_wip, _bobs = setup_editors(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    _classes, opened, _li, _page = workbench_entries(alice_wip)[("component", "row")]
    href = opened["href"]
    tabs = []
    for _tab in range(2):
        page = Page(open_editor(href))
        frame = root_frame(page, href)
        tabs.append((frame.attrs["data-scrud-etag"], content_of(page, frame)))
    assert tabs[0] == tabs[1]
    etag, content = tabs[0]
    row = resource(UI_COMPONENT_TYPE_URI, "row", "alice-wip")
    # A live draft of the item (what typing sends) renders and stores nothing.
    When.http.post(
        reverse("scrud-studio:live-preview"),
        data=urlencode(
            {
                "request": json.dumps(
                    {
                        "type": "ui-components",
                        "branch": "alice-wip",
                        "drafts": [
                            {
                                "kind": "component",
                                "content": {**content, "template": "<i>draft</i>"},
                            }
                        ],
                    }
                )
            }
        ),
        content_type="application/x-www-form-urlencoded",
    )
    Then.http.status_code_is(200)
    assert errors_of(html()) == []  # accepted as a draft, and shown
    row.refresh_from_db()
    assert row.content["template"] == content["template"]
    first = put_item(href, {**content, "template": "<i>first</i>"}, etag=etag)
    assert first.status_code == 200
    second = put_item(href, {**content, "template": "<i>second</i>"}, etag=etag)
    assert second.status_code == 412
    text = second.content.decode()
    page = Page(text)
    notices = [e for e in page.elements if "scrud-notice" in e.classes()]
    assert [" ".join(n.text.split()) for n in notices] == [STALE_MESSAGE]
    [reload] = [e for e in page.elements if "scrud-reload" in e.classes()]
    assert reload.attrs["hx-get"] == href and reload.attrs["hx-trigger"] == "load"
    assert reload.attrs["hx-swap"] == "outerHTML"
    headers = json.loads(reload.attrs["hx-headers"])
    assert headers["Scrud-UI-Scope"] == EDITOR_SCOPE
    assert headers["Scrud-UI-Pointer"] == ""
    assert "second" not in text and "scrud-content" not in text
    row.refresh_from_db()
    assert row.content["template"] == "<i>first</i>"
    # The reload: the editor with the first tab's save, at its ETag.
    extra = {
        "HTTP_" + k.upper().replace("-", "_"): v
        for k, v in headers.items()
        if k != "Scrud-UI-Pointer"
    }
    page = Page(get_page(href, HTTP_HX_REQUEST="true", **extra))
    frame = root_frame(page, href)
    assert frame.tag == "form" and frame.attrs["data-scrud-etag"] != etag
    [code] = page.having("data-studio-code")
    assert textarea_value(code) == "<i>first</i>"


@pytest.mark.django_db
@test_context
def test_revert_offered_only_over_something(studio_layer, ui_alice_login, admin_login):
    """An own item's ``reverts_to`` says what deleting T's row shows instead:
    ``inherited`` (an ancestor's item), ``packaged`` (a layer's), or
    ``null`` when T added the item, or what lies beneath is one the user
    may not read. The editor labels its delete control from it."""
    _studio, alice_wip, _bobs = setup_editors(ui_alice_login)
    # main's CSS "hidden" is in "", which alice may not read; alice-wip has
    # its own.
    make_css("hidden", "p { color: red }")
    make_css("hidden", "p { color: blue }", branch="alice-wip", context="alice")
    expected = {
        ("component", "row"): "inherited",
        ("component", "studio:page"): "packaged",
        ("component", "legacy"): None,
        ("css", "site"): None,
        ("js", "tools"): None,
        ("css", "hidden"): None,
        # Not own: nothing to revert.
        ("component", "head"): None,
        ("component", "card"): None,
        ("component", "studio:workbench"): None,
    }
    for user, changes in (
        (ui_alice_login, {}),
        (admin_login, {("css", "hidden"): "inherited"}),
    ):
        Given.http.force_authenticate(user)
        When.http.get(
            reverse("ui-branch-items-detail", args=["alice-wip"]), HTTP_ACCEPT=JSON
        )
        Then.http.status_code_is(200)
        listed = {(i["kind"], i["name"]): i for i in Http.response.json()["items"]}
        tree = workbench_entries(alice_wip)
        for key, reverts_to in {**expected, **changes}.items():
            assert listed[key]["reverts_to"] == reverts_to, (user, key)
            # The tree's entry carries it, for the editor.
            _classes, opened, _li, _page = tree[key]
            assert opened["reverts_to"] == reverts_to, (user, key)


@pytest.mark.django_db
@test_context
def test_component_header_labels_its_mode_field(studio_layer, ui_alice_login):
    """The header's ``<engine> · <mode>`` is the component's own ``mode``
    field, not the editor's state: an editable component of mode ``view``
    read ``jinja2 · view`` above a live textarea. The label names the
    field, in edit and in view mode alike."""
    _studio, alice_wip, _bobs = setup_editors(ui_alice_login)
    Given.http.force_authenticate(ui_alice_login)
    entries = workbench_entries(alice_wip)
    _classes, opened, _li, _page = entries[("component", "card")]
    for view in (False, True):
        page = Page(open_editor(opened["href"], view=view))
        [meta] = [e for e in page.elements if "studio-editor-meta" in e.classes()]
        assert meta.text.strip() == "jinja2 · mode: view", view
