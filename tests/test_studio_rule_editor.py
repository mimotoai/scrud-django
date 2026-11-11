"""The self-hosted studio's rule editor: a ``UIStylesheet`` resource rendered
in the scope ``studio studio-editor`` shows its fields (``name`` read-only,
``applies_to``, ``order``, ``prefixes``) and an editable list of selector ->
component rows, each with the runtime's array operations (Remove, Move up,
Move down) and its rule index, plus an append row. Every field is an input
named by its JSON Pointer, so the frame's PUT saves it; a selector the server
refuses is shown beside its row. The live preview marks a rule's matches
(the workbench's focus on a row asks for them), and its markers agree with
the inspector. These tests serve the API at ``/api/`` and the bootstrap
studio at ``/studio/`` (``tests.mounted_urls``), with the studio layer
installed (the ``studio_layer`` fixture) where they render the editor."""

import re

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.ui.layers import layer_item
from scrud_django.ui.types import UI_STYLESHEET_TYPE_URI

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_studio_editors import (
    EDITOR_SCOPE,
    content_of,
    no_inline_code,
    open_editor,
    put_item,
    resource,
    root_frame,
    workbench_entries,
)
from .test_studio_live_preview import draft, live, nodes
from .test_studio_page import store_item
from .test_studio_workbench import Page, setup_studio
from .test_ui_branch_types import make_branch
from .test_ui_http import PREFIXES, make_component, make_stylesheet

OT = "http://api.openteams.com/json-ld/"
SHEET_RULES = {
    f"studio studio-editor {OT}UIStylesheet": (
        "studio:stylesheet-editor",
        "studio:stylesheet-view",
    ),
    f"studio studio-editor studio-rules {OT}UIStylesheetRules": (
        "studio:rules-editor",
        "studio:rules-view",
    ),
    "studio studio-editor studio-rule object": (
        "studio:rule-row",
        "studio:rule-row-view",
    ),
}
FIELD_RULES = (
    "studio studio-editor studio-field string",
    "studio studio-editor studio-field integer",
)
THEME_PREFIXES = {"ex": "http://example.org/todo#", "a/b~c": "urn:x:"}
THEME_RULES = [
    {"select": "ex:TodoItem", "component": "row"},
    {"select": "compact ex:TodoItem", "component": "card"},
    {"select": "string", "component": "scrud:string"},
]


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def setup_rule_editor(user):
    """`setup_studio`, plus alice-wip's own stylesheet ``theme`` (in
    "alice"), with two prefixes (one that needs escaping in a pointer) and
    three rules. Returns the workbench's entry for it: ``(classes, open)``."""
    _studio, alice_wip, _bobs = setup_studio(user)
    store_item(
        "ui-stylesheets",
        "alice-wip",
        "alice",
        name="theme",
        applies_to="",
        order=2,
        prefixes=THEME_PREFIXES,
        rules=THEME_RULES,
    )
    Given.http.force_authenticate(user)
    classes, opened, _li, _page = workbench_entries(alice_wip)[("stylesheet", "theme")]
    return classes, opened


def named(page, within=None):
    return [
        e
        for e in page.elements
        if "name" in e.attrs and (within is None or e.within(within))
    ]


def rows(page):
    return page.having("data-studio-rule-index")


def text_of(element):
    return " ".join(element.text.split())


@pytest.mark.django_db
def test_rule_editor_layer_rules_and_components(studio_layer):
    _etag, sheet = layer_item("stylesheet", ("studio:studio", ""))
    rules = [(r["select"], r["component"]) for r in sheet["rules"]]
    for select, (edit, view) in SHEET_RULES.items():
        assert (select, edit) in rules and (select, view) in rules, select
    for select in FIELD_RULES:
        assert (select, "studio:field") in rules, select
    for name, mode, scopes in (
        (
            "studio:stylesheet-editor",
            "edit",
            {
                "applies_to": "studio-field",
                "order": "studio-field",
                "rules": "studio-rules",
            },
        ),
        ("studio:stylesheet-view", "view", {"rules": "studio-rules"}),
        ("studio:rules-editor", "edit", {"*": "studio-rule"}),
        ("studio:rules-view", "view", {"*": "studio-rule"}),
        (
            "studio:rule-row",
            "edit",
            {"select": "studio-field", "component": "studio-field"},
        ),
        ("studio:rule-row-view", "view", None),
        ("studio:field", "edit", None),
    ):
        _etag, component = layer_item("component", (name,))
        assert component["engine"] == "jinja2" and component["mode"] == mode, name
        assert component.get("scopes") == scopes, name


@pytest.mark.django_db
@test_context
def test_stylesheet_editor_fields_and_rows(studio_layer, ui_alice_login):
    classes, opened = setup_rule_editor(ui_alice_login)
    assert "studio-own" in classes
    href = opened["href"]
    page = Page(open_editor(href))
    frame = root_frame(page, href)
    # Edit mode: the stylesheet's own frame is the form its Save submits.
    assert frame.tag == "form" and frame.attrs["hx-put"] == href
    assert frame.attrs["data-scrud-scope"] == EDITOR_SCOPE
    stored = content_of(page, frame)
    assert stored["rules"] == THEME_RULES
    [editor] = page.having("data-studio-editor-item", within=frame)
    assert editor.attrs["data-studio-kind"] == "stylesheet"
    assert editor.attrs["data-studio-mode"] == "edit"
    assert editor.attrs["data-studio-stylesheet"] == "theme"
    assert editor.attrs["data-studio-applies-to"] == ""
    # The name is shown, never an input.
    [name] = [e for e in page.elements if "studio-sheet-name" in e.classes()]
    assert "theme" in text_of(name)
    # Every field is an input named by its JSON Pointer, in this order.
    inputs = named(page, within=frame)
    assert [(e.tag, e.attrs["name"]) for e in inputs] == [
        ("input", "/applies_to"),
        ("input", "/order"),
        ("input", "/prefixes/a~1b~0c"),
        ("input", "/prefixes/ex"),
        ("input", "/rules/0/select"),
        ("input", "/rules/0/component"),
        ("input", "/rules/1/select"),
        ("input", "/rules/1/component"),
        ("input", "/rules/2/select"),
        ("input", "/rules/2/component"),
        ("input", "/rules/-/select"),
        ("input", "/rules/-/component"),
    ]
    values = {e.attrs["name"]: e.attrs.get("value") for e in inputs}
    assert values == {
        "/applies_to": "",
        "/order": "2",
        "/prefixes/a~1b~0c": "urn:x:",
        "/prefixes/ex": "http://example.org/todo#",
        "/rules/0/select": "ex:TodoItem",
        "/rules/0/component": "row",
        "/rules/1/select": "compact ex:TodoItem",
        "/rules/1/component": "card",
        "/rules/2/select": "string",
        "/rules/2/component": "scrud:string",
        "/rules/-/select": None,
        "/rules/-/component": None,
    }
    types = {e.attrs["name"]: e.attrs["type"] for e in inputs}
    assert types["/order"] == "number"
    assert {t for n, t in types.items() if n != "/order"} == {"text"}
    # The script stops their change (no save on blur): each is a field.
    assert all("data-studio-field" in e.attrs for e in inputs)
    # Each prefix is a key/value row: the key as text, the IRI an input.
    keys = [text_of(e) for e in page.elements if "studio-prefix-key" in e.classes()]
    assert keys == ["a/b~c", "ex"]
    # A new prefix: a key input and an IRI input the script names.
    [key] = page.having("data-studio-prefix-key", within=frame)
    [iri] = page.having("data-studio-prefix-iri", within=frame)
    assert "name" not in key.attrs and "name" not in iri.attrs
    assert "data-studio-field" in key.attrs and "data-studio-field" in iri.attrs
    # Each input has a label.
    labels = {e.attrs["for"] for e in page.elements if e.tag == "label"}
    for element in inputs:
        assert element.attrs.get("id") in labels or element.attrs.get("aria-label")
    # Save is the form's submit; adding a rule is one too.
    submits = [
        text_of(e)
        for e in page.elements
        if e.tag == "button" and e.attrs.get("type") == "submit" and e.within(frame)
    ]
    assert submits == ["Add rule", "Save"]
    no_inline_code(page)
    # The frame's PUT (its content with the inputs applied) saves every
    # field, and answers with the editor again.
    etag = frame.attrs["data-scrud-etag"]
    changed = {
        **stored,
        "applies_to": "",
        "order": 3,
        "prefixes": {**THEME_PREFIXES, "ex": "http://example.org/other#"},
        "rules": [
            {"select": "ex:TodoItem", "component": "row-b"},
            *THEME_RULES[1:],
            {"select": "ex:TodoList", "component": "card"},
        ],
    }
    response = put_item(href, changed, etag=etag)
    assert response.status_code == 200, response.content
    saved = resource(UI_STYLESHEET_TYPE_URI, "theme", "alice-wip").content
    assert saved["order"] == 3 and saved["rules"] == changed["rules"]
    assert saved["prefixes"]["ex"] == "http://example.org/other#"
    page = Page(response.content.decode())
    values = {e.attrs["name"]: e.attrs.get("value") for e in named(page)}
    assert values["/rules/0/component"] == "row-b"
    assert values["/rules/3/select"] == "ex:TodoList"
    assert values["/order"] == "3"


@pytest.mark.django_db
@test_context
def test_rule_rows_have_array_controls_and_index(studio_layer, ui_alice_login):
    _classes, opened = setup_rule_editor(ui_alice_login)
    href = opened["href"]
    page = Page(open_editor(href))
    frame = root_frame(page, href)
    found = rows(page)
    assert [r.attrs["data-studio-rule-index"] for r in found] == ["0", "1", "2"]
    last = len(found) - 1
    for index, row in enumerate(found):
        assert row.tag == "li" and "studio-rule" in row.classes()
        assert [e.attrs["name"] for e in named(page, within=row)] == [
            f"/rules/{index}/select",
            f"/rules/{index}/component",
        ]
        ops = [
            (
                text_of(e),
                e.attrs["type"],
                e.attrs["data-scrud-array-op"],
                e.attrs["data-scrud-pointer"],
                e.attrs.get("data-scrud-to"),
            )
            for e in page.having("data-scrud-array-op", within=row)
        ]
        expected = [("Remove", "button", "remove", f"/rules/{index}", None)]
        if index > 0:
            expected.append(
                ("Move up", "button", "move", f"/rules/{index}", str(index - 1))
            )
        if index < last:
            expected.append(
                ("Move down", "button", "move", f"/rules/{index}", str(index + 1))
            )
        assert ops == expected, index
    # Every array operation is in a row.
    assert len(page.having("data-scrud-array-op")) == sum(
        len(page.having("data-scrud-array-op", within=r)) for r in found
    )
    # The append row (R6): inputs that add one rule through the frame's Save.
    [append] = [e for e in page.elements if "studio-rule-append" in e.classes()]
    assert [e.attrs["name"] for e in named(page, within=append)] == [
        "/rules/-/select",
        "/rules/-/component",
    ]
    assert not append.within(found[-1])
    # The counts the preview's marks fill, under the rows.
    [counts] = page.having("data-studio-rule-counts")
    assert "hidden" in counts.attrs and counts.attrs["role"] == "status"
    assert counts.within(frame) and counts.text.strip() == ""
    # What the runtime sends for Move down of rule 0 (the content with the
    # operation applied) and for the append row: each saves at once.
    stored = content_of(page, frame)
    moved = {**stored, "rules": [THEME_RULES[1], THEME_RULES[0], THEME_RULES[2]]}
    response = put_item(href, moved, etag=frame.attrs["data-scrud-etag"])
    assert response.status_code == 200, response.content
    page = Page(response.content.decode())
    frame = root_frame(page, href)
    values = [e.attrs.get("value") for e in named(page) if "/select" in e.attrs["name"]]
    assert values == ["compact ex:TodoItem", "ex:TodoItem", "string", None]
    added = {
        **content_of(page, frame),
        "rules": [*moved["rules"], {"select": "ex:TodoList", "component": "row"}],
    }
    response = put_item(href, added, etag=frame.attrs["data-scrud-etag"])
    assert response.status_code == 200, response.content
    saved = resource(UI_STYLESHEET_TYPE_URI, "theme", "alice-wip").content
    assert saved["rules"] == added["rules"]
    page = Page(response.content.decode())
    assert [r.attrs["data-studio-rule-index"] for r in rows(page)] == [
        "0",
        "1",
        "2",
        "3",
    ]


@pytest.mark.django_db
@test_context
def test_bad_selector_error_on_its_row(studio_layer, ui_alice_login):
    _classes, opened = setup_rule_editor(ui_alice_login)
    href = opened["href"]
    page = Page(open_editor(href))
    frame = root_frame(page, href)
    stored = content_of(page, frame)
    bad = "not a selector!"
    body = {
        **stored,
        "rules": [THEME_RULES[0], {**THEME_RULES[1], "select": bad}, THEME_RULES[2]],
    }
    response = put_item(href, body, etag=frame.attrs["data-scrud-etag"])
    assert response.status_code == 400
    page = Page(response.content.decode())
    found = rows(page)
    assert [r.attrs["data-studio-rule-index"] for r in found] == ["0", "1", "2"]
    # The message is beside rule 1's selector, whose input keeps the
    # submitted text, and nowhere else.
    errors = [e for e in page.elements if "scrud-errors" in e.classes()]
    assert len(errors) == 1
    [error] = errors
    assert error.within(found[1]) and error.attrs["role"] == "alert"
    assert "selector" in error.text.lower() or bad in error.text
    [select] = [e for e in named(page) if e.attrs["name"] == "/rules/1/select"]
    assert select.attrs["value"] == bad
    assert select.attrs["aria-invalid"] == "true"
    assert select.attrs["aria-describedby"] == error.attrs["id"]
    for other in (found[0], found[2]):
        assert not [e for e in named(page, within=other) if "aria-invalid" in e.attrs]
    # Nothing was saved.
    saved = resource(UI_STYLESHEET_TYPE_URI, "theme", "alice-wip").content
    assert saved["rules"] == THEME_RULES


@pytest.mark.django_db
@test_context
def test_view_mode_twin_has_no_inputs(studio_layer, ui_alice_login):
    _classes, opened = setup_rule_editor(ui_alice_login)
    href = opened["href"]
    page = Page(open_editor(href, view=True))
    frame = root_frame(page, href)
    assert frame.tag == "div"
    [editor] = page.having("data-studio-editor-item", within=frame)
    assert editor.attrs["data-studio-kind"] == "stylesheet"
    assert editor.attrs["data-studio-mode"] == "view"
    assert editor.attrs["data-studio-stylesheet"] == "theme"
    assert editor.attrs["data-studio-applies-to"] == ""
    # No input, no write, no array operation, no Save.
    assert not [e for e in page.elements if e.tag in ("input", "textarea", "select")]
    assert named(page) == [] and page.writes() == []
    assert page.having("data-scrud-array-op") == []
    assert "Save" not in page.buttons() and "Add rule" not in page.buttons()
    # The fields as text, and each rule a row that can take focus (to mark
    # its matches), with its index.
    text = text_of(editor)
    for shown in ("theme", "urn:x:", "a/b~c", "http://example.org/todo#"):
        assert shown in text, shown
    found = rows(page)
    assert [r.attrs["data-studio-rule-index"] for r in found] == ["0", "1", "2"]
    for row, rule in zip(found, THEME_RULES, strict=True):
        assert row.tag == "li" and row.attrs["tabindex"] == "0"
        [select] = [
            e
            for e in page.elements
            if "studio-rule-select" in e.classes() and e.within(row)
        ]
        [component] = [
            e
            for e in page.elements
            if "studio-rule-component" in e.classes() and e.within(row)
        ]
        assert select.text == rule["select"] and component.text == rule["component"]
    [counts] = page.having("data-studio-rule-counts")
    assert "hidden" in counts.attrs and counts.attrs["role"] == "status"
    no_inline_code(page)


def explained_marks(pointer, mark, branch, scope):
    """The marker `explain` implies for the node at `pointer` of the sample
    on `branch`, with `mark`."""
    When.http.get(
        reverse("scrud-studio:explain", args=["todo-lists"]),
        {
            "branch": branch,
            "mode": "view",
            "scope": " ".join(scope),
            "pointer": pointer,
        },
    )
    Then.http.status_code_is(200)
    explained = Http.response.json()
    marked = [
        index
        for index, rule in enumerate(explained["rules"])
        if rule.get("stylesheet") == mark["stylesheet"]
        and rule.get("applies_to") == mark["applies_to"]
        and rule["rank"][-1] == mark["index"]
    ]
    if not marked:
        return (None, None)
    if explained["winner"] in marked:
        return ("wins", "wins")
    return ("loses", explained["rules"][marked[0]]["outcome"])


def rule(select, component):
    return {"select": select, "component": component}


@pytest.mark.django_db
@test_context
def test_marks_agree_with_explain_for_layer_and_stored_sheets(admin_login):
    """The live preview marks a draft's rules as the draft's save would rank
    them: with a draft of ``s`` whose rules are reordered, each marker
    agrees with what `explain` says once that draft is saved, for the
    stored stylesheet's rules and for the packaged layer's
    (``layertest:sheet``). Mark indexes are rule indexes in the stylesheet
    (follow-up rule-index-not-rank-tail)."""
    make_component("row", "<b>{{ value.text }}</b>")
    make_component("row-b", "<i>{{ value.text }}</i>")
    make_stylesheet(
        "s",
        [
            ("ex:TodoItem", "row"),
            ("ex:TodoItem", "row-b"),
            ("layertest ex:TodoList", "gone"),
        ],
    )
    reverse("todo-lists-list")
    make_branch("wip", "alice")
    Given.http.force_authenticate(admin_login)
    reordered = [
        rule("ex:TodoItem", "row-b"),
        rule("layertest ex:TodoList", "gone"),
        rule("ex:TodoItem", "row"),
    ]
    drafts = [
        draft("stylesheet", name="s", applies_to="", prefixes=PREFIXES, rules=reordered)
    ]
    # (mark, scope): the stored sheet's rules without a scope; its TodoList
    # rule and the layer's (which renders the list as JSON) in "layertest".
    cases = [
        ({"stylesheet": "s", "applies_to": "", "index": index}, scope)
        for index in range(3)
        for scope in ([], ["layertest"])
    ] + [
        ({"stylesheet": "layertest:sheet", "applies_to": "", "index": 0}, ["layertest"])
    ]
    previewed = {}
    for mark, scope in cases:
        page = live(branch="wip", scope=scope, drafts=drafts, mark=mark)
        previewed[mark["stylesheet"], mark["index"], tuple(scope)] = nodes(page)
    items = [p for p in previewed["s", 0, ()] if re.fullmatch(r"/items/\d+", p)]
    assert items
    # What the draft's order means: row (now index 2) wins over row-b
    # (index 0); the draft's TodoList rule (index 1) names no component, so
    # the layer's rule wins on the root.
    for pointer in items:
        assert previewed["s", 0, ()][pointer] == ("loses", "lower cascade rank")
        assert previewed["s", 1, ()][pointer] == (None, None)
        assert previewed["s", 2, ()][pointer] == ("wins", "wins")
    assert previewed["s", 1, ("layertest",)][""] == ("loses", "missing component")
    assert previewed["layertest:sheet", 0, ("layertest",)][""] == ("wins", "wins")
    # Before the draft is saved, explain ranks the stored order: index 2 is
    # the TodoList rule, which matches no item.
    assert explained_marks(items[0], cases[4][0], "wip", []) == (None, None)
    # Saved as wip's own row, as the draft's save would store it.
    store_item(
        "ui-stylesheets",
        "wip",
        "alice",
        name="s",
        applies_to="",
        prefixes=PREFIXES,
        rules=reordered,
    )
    for mark, scope in cases:
        found = previewed[mark["stylesheet"], mark["index"], tuple(scope)]
        assert found, (mark, scope)
        for pointer, marker in found.items():
            assert marker == explained_marks(pointer, mark, "wip", scope), (
                mark,
                scope,
                pointer,
            )
        # The saved sheet previews alike, without a draft.
        assert nodes(live(branch="wip", scope=scope, mark=mark)) == found, mark
