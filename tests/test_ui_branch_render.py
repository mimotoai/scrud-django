"""Rendering on a selected branch: the effective state of components and
stylesheets (overrides over the parent's current state), and who may select."""

import logging

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_types import component, create, make_branch
from .test_ui_http import (
    HTML,
    detail_url,
    grant,
    html,
    make_component,
    make_stylesheet,
)

# The queries of a render with no branch selected: the 10 measured on 48d2844
# (before branches were read at all) less the one saved by reading the stored
# components, stylesheets and CSS items together (`cascade._stored`); a render
# of main must not read branch rows.
MAIN_RENDER_QUERIES = 9

TITLE = "<{0}>{{{{ value.title }}}}{{{{ children.items }}}}</{0}>"
BRANCH_HEADER = "HTTP_SCRUD_UI_BRANCH"


def put_item(slug, name, template, branch, context="project-a", **fields):
    """A component stored as is on `branch`."""
    reverse(f"{slug}-list")  # the URLconf registers the types
    return ResourceRegistration.register(
        {
            "name": name,
            "engine": "django",
            "mode": "view",
            "template": template,
            "rbac_context": context,
            "branch": branch,
            **fields,
        },
        slug,
    )


def render_with(todo_list, branch=None):
    headers = {} if branch is None else {BRANCH_HEADER: branch}
    When.http.get(detail_url(todo_list), HTTP_ACCEPT=HTML, **headers)
    Then.http.status_code_is(200)
    return html()


def setup_main_and_wip():
    make_component("row", "<b>{{ value.text }}</b>")
    make_component("title", TITLE.format("h1"))
    make_stylesheet("s", [("ex:TodoItem", "row"), ("ex:TodoList", "title")])
    make_branch("wip", "project-a")
    put_item("ui-components", "row", "<i>{{ value.text }}</i>", "wip")


def may_select_branches(user):
    """Also let `user` (the to-do reader) GET UI branches in "project-a"."""
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI]}, "project-a")
    return user


@pytest.mark.django_db
@test_context
def test_live_branch_inherits_untouched_and_overrides_touched(
    todo_list, todo_reader_login
):
    setup_main_and_wip()
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    page = render_with(todo_list, "wip")
    assert "<i>Buy milk</i>" in page and "<h1>Groceries" in page
    assert "<b>Buy milk</b>" not in page
    # Main is unchanged without the selection.
    page = render_with(todo_list)
    assert "<b>Buy milk</b>" in page and "<i>Buy milk</i>" not in page
    # A later edit of an untouched item on main shows on the live branch.
    title = Resource.objects.get(content__name="title")
    title.content = {**title.content, "template": TITLE.format("h2")}
    title.save()
    page = render_with(todo_list, "wip")
    assert "<h2>Groceries" in page and "<i>Buy milk</i>" in page


@pytest.mark.django_db
@test_context
def test_deletion_marker_hides_inherited_item(todo_list, todo_reader_login):
    setup_main_and_wip()
    Resource.objects.filter(content__name="row", content__branch="wip").delete()
    put_item("ui-components", "row", "", "wip", deleted=True)
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    page = render_with(todo_list, "wip")
    assert "<b>Buy milk</b>" not in page and "<i>Buy milk</i>" not in page
    assert "scrud-object" in page  # the built-in
    assert "<h1>Groceries" in page


@pytest.mark.django_db
@test_context
def test_unauthorized_or_unknown_selection_renders_main(
    todo_list, todo_reader_login, caplog
):
    setup_main_and_wip()
    with caplog.at_level(logging.WARNING):
        # No GET on the branch.
        Given.http.force_authenticate(todo_reader_login)
        assert "<b>Buy milk</b>" in render_with(todo_list, "wip")
        Given.http.force_authenticate(may_select_branches(todo_reader_login))
        assert "<i>Buy milk</i>" in render_with(todo_list, "wip")
        assert "<b>Buy milk</b>" in render_with(todo_list, "nope")
        assert "<b>Buy milk</b>" in render_with(todo_list, "main")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.django_db
@test_context
def test_broken_chain_renders_main(todo_list, todo_reader_login, caplog):
    setup_main_and_wip()
    make_branch("child", "project-a", parent="wip")
    put_item("ui-components", "row", "<u>{{ value.text }}</u>", "child")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    assert "<u>Buy milk</u>" in render_with(todo_list, "child")
    Resource.objects.filter(
        resource_type__type_uri=UI_BRANCH_TYPE_URI, content__name="wip"
    ).delete()
    with caplog.at_level(logging.WARNING):
        page = render_with(todo_list, "child")
    assert "<b>Buy milk</b>" in page and "<u>Buy milk</u>" not in page
    assert any(
        r.levelno == logging.WARNING and "child" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.django_db
@test_context
def test_base_is_server_managed(ui_alice_login):
    make_branch("wip", "alice")
    parent_row = make_component("row", "<b>{{ value.text }}</b>")
    parent_etag = Resource.objects.get(pk=parent_row.pk).etag
    Given.http.force_authenticate(ui_alice_login)
    create(
        "ui-components",
        component(name="row", rbac_context="alice", branch="wip", base="junk"),
    )
    Then.http.status_code_is(201)
    When.http.get(Http.response["Location"])
    assert Http.response.json()["base"] == parent_etag
    create(
        "ui-components",
        component(name="fresh", rbac_context="alice", branch="wip", base="junk"),
    )
    Then.http.status_code_is(201)
    When.http.get(Http.response["Location"])
    assert Http.response.json()["base"] is None


@pytest.mark.django_db
@test_context
def test_main_without_branches_makes_no_extra_queries(todo_list, todo_reader_login):
    make_component("row", "<b>{{ value.text }}</b>")
    make_stylesheet("s", [("ex:TodoItem", "row")])
    make_branch("wip", "project-a")
    put_item("ui-components", "row", "<i>{{ value.text }}</i>", "wip")
    Given.http.force_authenticate(todo_reader_login)
    render_with(todo_list)
    with CaptureQueriesContext(connection) as queries:
        assert "<b>Buy milk</b>" in render_with(todo_list)
    assert len(queries) == MAIN_RENDER_QUERIES, [q["sql"] for q in queries]
    assert not [q for q in queries if "UIBranch" in q["sql"]]


@pytest.mark.django_db
@test_context
def test_rows_without_a_string_name_are_skipped_and_logged(
    todo_list, todo_reader_login, caplog
):
    make_component("row", "<b>{{ value.text }}</b>")
    make_stylesheet("s", [("ex:TodoItem", "row")])
    reverse("ui-components-list")
    raw = {"engine": "django", "mode": "view", "template": "", "rbac_context": ""}
    unnamed = ResourceRegistration.register(raw, "ui-components")
    listed = ResourceRegistration.register({**raw, "name": ["a"]}, "ui-components")
    Given.http.force_authenticate(todo_reader_login)
    with caplog.at_level(logging.WARNING):
        assert "<b>Buy milk</b>" in render_with(todo_list)
    found = [r.getMessage() for r in caplog.records if "no name" in r.getMessage()]
    assert len(found) == 2, found
    assert sum(f"component {unnamed.pk} " in m for m in found) == 1
    assert sum(f"component {listed.pk} " in m for m in found) == 1


@pytest.mark.django_db
@test_context
def test_component_in_another_context_on_main_is_skipped(
    todo_list, todo_reader_login, caplog
):
    """A stored row whose rbac_context is not the context its write is
    authorized in on its branch (here a component on main in "project-a",
    stored around the API) is skipped by the cascade and logged: no write
    through the API could have stored it, so whoever could write in
    "project-a" must not control main's markup (ruling R41, minor 3; it was
    rendered before)."""
    stored = make_component("row", "<b>{{ value.text }}</b>")
    Resource.objects.filter(pk=stored.pk).update(
        content={**stored.content, "rbac_context": "project-a"}
    )
    make_stylesheet("s", [("ex:TodoItem", "row")])
    Given.http.force_authenticate(todo_reader_login)
    with caplog.at_level(logging.WARNING):
        page = render_with(todo_list)
    assert "<b>Buy milk</b>" not in page
    assert "scrud-object" in page  # the built-in
    assert any(
        f"component {stored.pk} " in r.getMessage() and "'project-a'" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.django_db
@test_context
def test_an_unauthorized_override_lets_the_parent_item_show(
    todo_list, todo_reader_login, caplog
):
    """On a branch, a row in a context other than the branch's is skipped
    as if absent: the parent's item shows through (ruling R41, minor 3)."""
    setup_main_and_wip()
    put_item("ui-components", "row", "<u>{{ value.text }}</u>", "wip", context="bob")
    Resource.objects.filter(
        content__branch="wip", content__name="row", content__rbac_context="project-a"
    ).delete()
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    with caplog.at_level(logging.WARNING):
        page = render_with(todo_list, "wip")
    assert "<b>Buy milk</b>" in page and "<u>Buy milk</u>" not in page
    assert any("'bob'" in r.getMessage() for r in caplog.records)
