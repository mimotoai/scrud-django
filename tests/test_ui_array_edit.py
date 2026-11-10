"""Remove and reorder controls of the built-in ``array-edit``, and a bad
stylesheet selector landing on its own rule."""

import re

import pytest
from django.urls import reverse
from django.utils.safestring import mark_safe
from safetydance_django.test import *  # noqa: F403, F401

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_builtins import render
from .test_ui_http import FRAME, HTML, assert_html, html, make_stylesheet, put_json

OPS = re.compile(r'<button type="button" data-scrud-array-op="([a-z]+)"([^>]*)>([^<]*)')


def ops_of(item_html):
    """[(op, pointer, to, label)] of the buttons in `item_html`."""
    found = []
    for op, rest, label in OPS.findall(item_html):
        pointer = re.search(r'data-scrud-pointer="([^"]*)"', rest)
        to = re.search(r'data-scrud-to="([^"]*)"', rest)
        found.append(
            (op, pointer and pointer.group(1), to and to.group(1), label.strip())
        )
    return found


def test_array_items_get_remove_and_move_controls():
    out = render(
        "array-edit",
        pointer="/items",
        value=[1, 2, 3],
        children=[mark_safe(f"<i>c{n}</i>") for n in range(3)],
        schema={"type": "array", "items": {"type": "string"}},
    )
    li = re.findall(r"<li>(.*?)</li>", out, re.S)
    assert len(li) == 3
    assert ops_of(li[0]) == [
        ("remove", "/items/0", None, "Remove"),
        ("move", "/items/0", "1", "Move down"),
    ]
    assert ops_of(li[1]) == [
        ("remove", "/items/1", None, "Remove"),
        ("move", "/items/1", "0", "Move up"),
        ("move", "/items/1", "2", "Move down"),
    ]
    assert ops_of(li[2]) == [
        ("remove", "/items/2", None, "Remove"),
        ("move", "/items/2", "1", "Move up"),
    ]
    assert "<i>c1</i>" in li[1]


def test_single_item_has_only_remove():
    out = render("array-edit", pointer="/x", children=[mark_safe("<i>a</i>")])
    assert [o[0] for o in ops_of(out)] == ["remove"]


def test_root_array_item_pointers():
    out = render("array-edit", pointer="", children=[mark_safe("a"), mark_safe("b")])
    assert [o[1] for o in ops_of(out)] == ["/0", "/0", "/1", "/1"]


@pytest.mark.django_db
@test_context
def test_view_mode_has_no_array_controls(todo_list, todo_owner_login):
    url = reverse("todo-lists-detail", args=[todo_list.pk])
    Given.http.force_authenticate(todo_owner_login)
    When.http.get(url, HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    assert "data-scrud-array-op" in html()
    When.http.get(url + "?ui-mode=view", HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    assert "data-scrud-array-op" not in html()
    assert "hx-put" not in html()


@pytest.mark.django_db
@test_context
def test_bad_selector_error_lands_on_its_rule(admin_login):
    sheet = make_stylesheet("mine", [("null", "scrud:json")])
    url = reverse("ui-stylesheets-detail", args=[sheet.pk])
    body = {
        **sheet.content,
        "rules": [
            {"select": "array", "component": "scrud:json"},
            {"select": "strng", "component": "scrud:json"},
        ],
    }
    Given.http.force_authenticate(admin_login)
    put_json(url, body, HTTP_IF_MATCH=f'"{sheet.etag}"', HTTP_ACCEPT="application/json")
    Then.http.status_code_is(400)
    assert Http.response.json()["name"] == "rules[1].select"
    put_json(
        url,
        body,
        HTTP_IF_MATCH=f'"{sheet.etag}"',
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(400)
    assert_html()
    output = html()
    assert FRAME in output
    # the error is inside the item of rule 1, not rule 0
    items = re.findall(r"<li>(.*?)</li>", output, re.S)
    with_error = [i for i in items if "The stylesheet selector is invalid" in i]
    assert with_error and all('name="/rules/1/' in i for i in with_error)
    assert 'name="/rules/0/select"' in output
