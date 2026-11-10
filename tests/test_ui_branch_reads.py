"""Reading UI items through a branch: the list is the branch's effective state."""

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django import collection_type_uri_for
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_render import BRANCH_HEADER, put_item
from .test_ui_branch_types import make_branch
from .test_ui_http import grant, make_component

LIST = "ui-components-list"
COMPONENTS = collection_type_uri_for(UI_COMPONENT_TYPE_URI)


def listed(**extra):
    When.http.get(reverse(LIST), **extra)
    Then.http.status_code_is(200)
    body = Http.response.json()
    items = [entry["content"] for entry in body["content"]]
    return {(i["name"], i.get("branch", "main")): i for i in items}


def setup_wip():
    make_component("title", "<h1/>")
    make_component("row", "<b/>")
    make_branch("wip", "project-a")
    put_item("ui-components", "row", "<i/>", "wip")


@pytest.mark.django_db
@test_context
def test_list_is_effective_state_of_selected_branch(admin_login):
    setup_wip()
    Given.http.force_authenticate(admin_login)
    assert set(listed(**{BRANCH_HEADER: "wip"})) == {
        ("row", "wip"),
        ("title", "main"),
    }
    # The query parameter selects too, and the header comes first.
    assert set(listed(QUERY_STRING="ui-branch=wip")) == {
        ("row", "wip"),
        ("title", "main"),
    }
    assert set(listed(QUERY_STRING="ui-branch=nope", **{BRANCH_HEADER: "wip"})) == {
        ("row", "wip"),
        ("title", "main"),
    }


@pytest.mark.django_db
@test_context
def test_list_includes_deletion_markers_and_tells_changed_from_added(admin_login):
    setup_wip()
    put_item("ui-components", "title", "", "wip", deleted=True)
    put_item("ui-components", "extra", "<u/>", "wip")
    Given.http.force_authenticate(admin_login)
    items = listed(**{BRANCH_HEADER: "wip"})
    assert set(items) == {("row", "wip"), ("title", "wip"), ("extra", "wip")}
    assert items[("title", "wip")]["deleted"] is True


@pytest.mark.django_db
@test_context
def test_inherited_items_keep_their_read_check(ui_alice_login):
    make_component("title", "<h1/>")
    make_branch("alice-wip", "alice")
    put_item("ui-components", "row", "<i/>", "alice-wip", context="alice")
    Given.http.force_authenticate(ui_alice_login)
    assert set(listed(**{BRANCH_HEADER: "alice-wip"})) == {("row", "alice-wip")}
    # With GET on main's context too, the inherited item shows.
    grant(ui_alice_login, {"http.GET": [UI_COMPONENT_TYPE_URI, COMPONENTS]}, "")
    assert set(listed(**{BRANCH_HEADER: "alice-wip"})) == {
        ("row", "alice-wip"),
        ("title", "main"),
    }


@pytest.mark.django_db
@test_context
def test_unreadable_branch_list_is_refused(todo_reader_login):
    """A JSON list naming a branch the reader may not GET is refused (400),
    not answered with main's items as before ruling R40: a client asking
    for a branch must not take main's items for the branch's."""
    setup_wip()
    # May read components in both contexts, but not the branch itself.
    grant(todo_reader_login, {"http.GET": [UI_COMPONENT_TYPE_URI, COMPONENTS]}, "")
    grant(
        todo_reader_login,
        {"http.GET": [UI_COMPONENT_TYPE_URI, COMPONENTS]},
        "project-a",
    )
    Given.http.force_authenticate(todo_reader_login)
    When.http.get(reverse(LIST), **{BRANCH_HEADER: "wip"})
    Then.http.status_code_is(400)
    body = Http.response.json()
    assert body["type"].endswith("/ui-branch-unavailable")
    assert "content" not in body
    # Without the selection, main's items are listed.
    assert set(listed()) == {("row", "main"), ("title", "main")}


@pytest.mark.django_db
@test_context
def test_list_without_selection_is_main(admin_login):
    setup_wip()
    Given.http.force_authenticate(admin_login)
    assert set(listed()) == {("row", "main"), ("title", "main")}


@pytest.mark.django_db
@test_context
def test_list_of_main_reads_no_branch_rows(admin_login, monkeypatch):
    setup_wip()

    def refuse():
        raise AssertionError("a list of main read the branch rows")

    monkeypatch.setattr("scrud_django.ui.branches.effective.branches", refuse)
    Given.http.force_authenticate(admin_login)
    assert set(listed()) == {("row", "main"), ("title", "main")}
    assert set(listed(**{BRANCH_HEADER: "main"})) == {
        ("row", "main"),
        ("title", "main"),
    }
