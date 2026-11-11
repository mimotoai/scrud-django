"""The plain app (no stored or packaged presentation in use, no JavaScript
item, no scope, no requested mode) renders as it did before the studio's
runtime features (develop at f43f1e8): the files in ``plain_output/`` are
that commit's output for the same requests, normalized
(`normalized`). The suite installs the ``layertest:`` layer
(`tests.ui_layer`), inert by design: it changes none of this. The intended
differences (the ``Vary`` header's new names, the array edit controls in
edit mode) are asserted on their own."""

import re
from pathlib import Path

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401

GOLDEN = Path(__file__).parent / "plain_output"
HTML = "text/html"
# The Vary header on develop, and the names added since: the mode and the
# safe mode a request may ask for in a header.
VARY_ON_DEVELOP = (
    "Accept, HX-Request, Scrud-UI-Scope, Scrud-UI-Pointer, Scrud-UI-Branch, Cookie"
)
VARY = (
    "Accept, HX-Request, Scrud-UI-Scope, Scrud-UI-Pointer, Scrud-UI-Mode,"
    " Scrud-UI-Branch, Scrud-UI-Safe, Cookie"
)
ARRAY_OPS = re.compile(r'<span class="scrud-array-ops">.*?</span>', re.S)


def normalized(text):
    """`text` without what differs from one run to the next: the CSRF
    token, ETags, the per-render input ids, and the to-do lists' keys
    (numbered in order of appearance)."""
    text = re.sub(r'name="csrf-token" content="[^"]*"', 'name="csrf-token"', text)
    text = re.sub(r"[0-9a-f]{32}", "ETAG", text)
    text = re.sub(r"scrud-[0-9a-f]{8}-", "scrud-ID-", text)
    keys = {}

    def key(match):
        number = keys.setdefault(match.group(1), len(keys))
        return f"/todo-lists/PK{number}/"

    return re.sub(r"/todo-lists/(\d+)/", key, text)


def golden(name):
    return (GOLDEN / f"{name}.html").read_text(encoding="utf-8")


def get(url, **headers):
    When.http.get(url, HTTP_ACCEPT=HTML, **headers)
    Then.http.status_code_is(200)
    assert Http.response["Content-Type"] == "text/html; charset=utf-8"
    return Http.response


@pytest.mark.django_db
@test_context
def test_plain_app_renders_as_on_develop(todo_list, todo_reader_login):
    url = reverse("todo-lists-detail", args=[todo_list.pk])
    Given.http.force_authenticate(todo_reader_login)
    for name, response in (
        ("page", get(url)),
        ("fragment", get(url, HTTP_HX_REQUEST="true")),
        ("list", get(reverse("todo-lists-list"))),
    ):
        assert normalized(response.content.decode()) == golden(name), name


@pytest.mark.django_db
@test_context
def test_plain_app_intended_differences(todo_list, todo_reader_login, todo_owner_login):
    url = reverse("todo-lists-detail", args=[todo_list.pk])
    Given.http.force_authenticate(todo_reader_login)
    for response in (
        get(url),
        get(url, HTTP_HX_REQUEST="true"),
        get(reverse("todo-lists-list")),
    ):
        assert response["Vary"] == VARY
    assert VARY.replace(" Scrud-UI-Mode,", "").replace(" Scrud-UI-Safe,", "") == (
        VARY_ON_DEVELOP
    )
    # In edit mode, each array item has its Remove and Move controls: the
    # one difference from develop's edit form.
    Given.http.force_authenticate(todo_owner_login)
    edit = normalized(get(url, HTTP_HX_REQUEST="true").content.decode())
    controls = ARRAY_OPS.findall(edit)
    assert controls == [
        '<span class="scrud-array-ops"><button type="button"'
        ' data-scrud-array-op="remove" data-scrud-pointer="/items/0">Remove</button>'
        '<button type="button" data-scrud-array-op="move"'
        ' data-scrud-pointer="/items/0" data-scrud-to="1">Move down</button></span>',
        '<span class="scrud-array-ops"><button type="button"'
        ' data-scrud-array-op="remove" data-scrud-pointer="/items/1">Remove</button>'
        '<button type="button" data-scrud-array-op="move"'
        ' data-scrud-pointer="/items/1" data-scrud-to="0">Move up</button></span>',
    ]
    assert ARRAY_OPS.sub("", edit) == golden("owner_fragment")
    # A reader's view mode has none.
    Given.http.force_authenticate(todo_reader_login)
    assert "data-scrud-array-op" not in get(url).content.decode()
