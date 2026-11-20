"""The scope chain in the URL (``?ui-scope=``) and a requested view mode
(``Scrud-UI-Mode`` / ``?ui-mode=view``): presentation only, never more than
the viewer may do."""

import pytest
from safetydance_django.test import *  # noqa: F403, F401

from .fixtures import *  # noqa
from .fixtures import TYPE_URI_TODO_LIST, assign_role
from .steps import *  # noqa: F403, F401
from .test_ui_http import (
    assert_html,
    html,
    make_component,
    make_stylesheet,
    ui_detail_url,
    ui_list_url,
)
from .ui import UI_URLCONF


@pytest.fixture(autouse=True)
def ui_urlconf(settings):
    settings.ROOT_URLCONF = UI_URLCONF


NAV = '<nav class="sidebar">Groceries</nav>'


def sidebar():
    make_component("sidebar-list", '<nav class="sidebar">{{ value.title }}</nav>')
    make_stylesheet("s", [("sidebar ex:TodoList", "sidebar-list")])


@pytest.mark.django_db
@test_context
def test_url_scope_equals_header(todo_list, todo_reader_login):
    sidebar()
    url = ui_detail_url(todo_list)
    Given.http.force_login(todo_reader_login)
    When.http.get(url, HTTP_HX_REQUEST="true", HTTP_SCRUD_UI_SCOPE="sidebar")
    by_header = html()
    When.http.get(url + "?ui-scope=sidebar", HTTP_HX_REQUEST="true")
    Then.http.status_code_is(200)
    assert NAV in html()
    assert html() == by_header
    When.http.get(url + "?ui-scope=sidebar,other", HTTP_HX_REQUEST="true")
    assert 'data-scrud-scope="sidebar other"' in html()


@pytest.mark.django_db
@test_context
def test_header_scope_wins(todo_list, todo_reader_login):
    sidebar()
    url = ui_detail_url(todo_list)
    Given.http.force_login(todo_reader_login)
    When.http.get(
        url + "?ui-scope=sidebar",
        HTTP_HX_REQUEST="true",
        HTTP_SCRUD_UI_SCOPE="other",
    )
    Then.http.status_code_is(200)
    assert NAV not in html()
    assert 'data-scrud-scope="other"' in html()


@pytest.mark.django_db
@test_context
def test_invalid_url_scope_ignored_like_header(todo_list, todo_reader_login):
    sidebar()
    url = ui_detail_url(todo_list)
    Given.http.force_login(todo_reader_login)
    for scope in ("sidebar 1bad", "sidebar,<x>", ",".join(["sidebar"] * 17)):
        When.http.get(url, {"ui-scope": scope}, HTTP_HX_REQUEST="true")
        Then.http.status_code_is(200)
        assert 'data-scrud-scope=""' in html(), scope
        assert NAV not in html(), scope
    When.http.get(
        url,
        {"ui-scope": " ".join(["sidebar"] * 16)},
        HTTP_HX_REQUEST="true",
    )
    assert NAV in html()


@pytest.mark.django_db
@test_context
def test_view_mode_downgrades_a_writer(todo_list, todo_owner_login):
    url = ui_detail_url(todo_list)
    Given.http.force_login(todo_owner_login)
    When.http.get(url, HTTP_HX_REQUEST="true")
    assert "hx-put" in html() and html().startswith("<form")
    for kwargs in (
        {"path": url + "?ui-mode=view"},
        {"path": url, "HTTP_SCRUD_UI_MODE": "view"},
        {"path": url + "?ui-mode=bogus", "HTTP_SCRUD_UI_MODE": "view"},
    ):
        When.http.get(kwargs.pop("path"), HTTP_HX_REQUEST="true", **kwargs)
        Then.http.status_code_is(200)
        assert "Buy milk" in html()
        assert "hx-put" not in html()
        assert "<form" not in html()


DELETE_BUTTON = ">Delete</button>"


@pytest.mark.django_db
@test_context
def test_requested_view_mode_omits_delete(todo_list, todo_owner_login):
    """A requested view mode (the parameter or the header) is a view: the
    frame has no Delete button, though the user may DELETE."""
    url = ui_detail_url(todo_list)
    Given.http.force_login(todo_owner_login)
    When.http.get(url, HTTP_HX_REQUEST="true")
    assert f'hx-delete="{url}"' in html() and DELETE_BUTTON in html()
    for kwargs in (
        {"path": url + "?ui-mode=view"},
        {"path": url, "HTTP_SCRUD_UI_MODE": "view"},
    ):
        When.http.get(kwargs.pop("path"), HTTP_HX_REQUEST="true", **kwargs)
        Then.http.status_code_is(200)
        assert "Buy milk" in html()
        assert "hx-delete" not in html() and DELETE_BUTTON not in html()
    # A list's items too.
    When.http.get(ui_list_url() + "?ui-mode=view", HTTP_HX_REQUEST="true")
    Then.http.status_code_is(200)
    assert "Buy milk" in html() and "hx-delete" not in html()


@pytest.mark.django_db
@test_context
def test_natural_view_mode_keeps_delete(todo_list):
    """A user in view mode because they may not PUT, who may DELETE, keeps
    the Delete button: only a requested view mode leaves it out."""
    deleter = assign_role(
        {"http.GET": [TYPE_URI_TODO_LIST], "http.DELETE": [TYPE_URI_TODO_LIST]},
        "project-a",
    )
    url = ui_detail_url(todo_list)
    Given.http.force_login(deleter)
    When.http.get(url, HTTP_HX_REQUEST="true")
    Then.http.status_code_is(200)
    assert "hx-put" not in html() and "<form" not in html()
    assert f'hx-delete="{url}"' in html() and DELETE_BUTTON in html()


@pytest.mark.django_db
@test_context
def test_view_mode_never_upgrades_a_reader(todo_list, todo_reader_login):
    url = ui_detail_url(todo_list)
    Given.http.force_login(todo_reader_login)
    When.http.get(url, HTTP_HX_REQUEST="true")
    plain = html()
    When.http.get(url + "?ui-mode=edit", HTTP_HX_REQUEST="true")
    Then.http.status_code_is(400)
    When.http.get(url, HTTP_SCRUD_UI_MODE="edit")
    Then.http.status_code_is(400)
    When.http.get(url + "?ui-mode=view", HTTP_HX_REQUEST="true")
    Then.http.status_code_is(200)
    assert html() == plain
    assert "hx-put" not in plain


@pytest.mark.django_db
@test_context
def test_view_mode_applies_to_list_items(todo_list, todo_owner_login):
    Given.http.force_login(todo_owner_login)
    When.http.get(ui_list_url(), HTTP_HX_REQUEST="true")
    Then.http.status_code_is(200)
    assert "hx-put" in html()
    When.http.get(ui_list_url() + "?ui-mode=view", HTTP_HX_REQUEST="true")
    Then.http.status_code_is(200)
    assert "Buy milk" in html()
    assert "hx-put" not in html()
    assert "<form" not in html()


@pytest.mark.django_db
@test_context
def test_mode_header_in_vary(todo_list, todo_owner_login):
    Given.http.force_login(todo_owner_login)
    When.http.get(ui_detail_url(todo_list))
    Then.http.status_code_is(200)
    assert_html()
    vary = {v.strip().lower() for v in Http.response["Vary"].split(",")}
    assert "scrud-ui-mode" in vary
