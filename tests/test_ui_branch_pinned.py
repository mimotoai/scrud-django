"""Pinned bases: a branch pinned (by its `base`, or for one view with
`<name>@pinned`) shows its ancestors as of its branching point."""

import logging
import re
from html import unescape
from unittest import mock

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.ui.branches import effective
from scrud_django.ui.branches.versions import record_version, snapshot
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_push import WIP, push
from .test_ui_branch_push import create as push_create
from .test_ui_branch_render import (
    TITLE,
    may_select_branches,
    put_item,
    render_with,
)
from .test_ui_branch_types import component, make_branch, problem
from .test_ui_branch_versions import versions_of
from .test_ui_http import make_component, make_stylesheet, put_json


def setup_main():
    make_component("row", "<b>{{ value.text }}</b>")
    make_component("title", TITLE.format("h1"))
    make_stylesheet("s", [("ex:TodoItem", "row"), ("ex:TodoList", "title")])


def edit_main_title(tag):
    """Edit main's `title` and record main's next version."""
    title = Resource.objects.get(content__name="title")
    title.content = {**title.content, "template": TITLE.format(tag)}
    title.save()
    record_version("main", "default")


@pytest.mark.django_db
@test_context
def test_pinned_branch_ignores_later_parent_changes(todo_list, todo_reader_login):
    setup_main()
    make_branch(
        "wip",
        "project-a",
        base="pinned",
        branching_point={"main": versions_of("main")[-1]},
    )
    put_item("ui-components", "row", "<i>{{ value.text }}</i>", "wip")
    edit_main_title("h2")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    page = render_with(todo_list, "wip")
    assert "<h1>Groceries" in page and "<h2>Groceries" not in page
    assert "<i>Buy milk</i>" in page  # its own override stays live
    assert "<h2>Groceries" in render_with(todo_list)


@pytest.mark.django_db
@test_context
def test_pinned_view_of_live_branch(todo_list, todo_reader_login):
    setup_main()
    make_branch("wip", "project-a", branching_point={"main": versions_of("main")[-1]})
    put_item("ui-components", "row", "<i>{{ value.text }}</i>", "wip")
    edit_main_title("h2")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    live = render_with(todo_list, "wip")
    assert "<h2>Groceries" in live and "<i>Buy milk</i>" in live
    pinned = render_with(todo_list, "wip@pinned")
    assert "<h1>Groceries" in pinned and "<h2>Groceries" not in pinned
    assert "<i>Buy milk</i>" in pinned


@pytest.mark.django_db
@test_context
def test_pinned_view_by_query_parameter(todo_list, todo_reader_login):
    from .test_ui_http import HTML, detail_url, html

    setup_main()
    make_branch("wip", "project-a", branching_point={"main": versions_of("main")[-1]})
    edit_main_title("h2")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    # A link (the parameter) asks before it switches branch; confirmed, it
    # shows the pinned view.
    url = detail_url(todo_list) + "?ui-branch=wip@pinned"
    When.http.get(url, HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    assert "You're about to browse branch wip" in html()
    found = re.search(r'class="scrud-branch-continue" href="([^"]*)"', html())
    When.http.get(unescape(found.group(1)), HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    assert "<h1>Groceries" in html() and "<h2>Groceries" not in html()


@pytest.mark.django_db
@test_context
def test_pinned_chain_uses_every_ancestor_version(todo_list, todo_reader_login):
    setup_main()
    make_branch("testing", "project-a")
    put_item("ui-components", "row", "<i>{{ value.text }}</i>", "testing")
    make_branch(
        "alice",
        "project-a",
        parent="testing",
        branching_point={
            "testing": versions_of("testing")[-1],
            "main": versions_of("main")[-1],
        },
    )
    # main moves on; testing does not change.
    edit_main_title("h2")
    assert versions_of("testing") == [1]
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    pinned = render_with(todo_list, "alice@pinned")
    assert "<h1>Groceries" in pinned and "<h2>Groceries" not in pinned
    assert "<i>Buy milk</i>" in pinned  # testing's override, from its snapshot
    assert "<h2>Groceries" in render_with(todo_list, "alice")


@pytest.mark.django_db
@test_context
def test_missing_snapshot_renders_main(todo_list, todo_reader_login, caplog):
    setup_main()
    make_branch("wip", "project-a", base="pinned", branching_point={"main": 99})
    put_item("ui-components", "row", "<i>{{ value.text }}</i>", "wip")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    seen = []
    real = effective.effective_rows

    def spy(*args, **kwargs):
        seen.append(effective.active_selection())
        return real(*args, **kwargs)

    with (
        caplog.at_level(logging.WARNING),
        mock.patch.object(effective, "effective_rows", spy),
    ):
        page = render_with(todo_list, "wip")
    assert "<b>Buy milk</b>" in page and "<i>Buy milk</i>" not in page
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "wip" in warnings[0].getMessage()
    assert seen and all(selection == effective.Selection() for selection in seen)


@pytest.mark.django_db
@test_context
def test_effective_rows_survive_a_missing_snapshot(caplog):
    setup_main()
    make_branch("wip", "project-a", base="pinned", branching_point={"main": 99})
    with caplog.at_level(logging.WARNING):
        rows = effective.effective_rows(
            UI_COMPONENT_TYPE_URI, effective.Selection("wip")
        )
    assert {content["name"] for _pk, _etag, content in rows} == {"row", "title"}
    assert not caplog.records


@pytest.mark.django_db
@test_context
def test_an_override_on_a_pinned_branch_is_based_on_the_pinned_item(admin_login):
    """The reviewer's probe (I1): main's ``row`` is v1 when ``wip`` is
    pinned; main then edits it (v2); an override made on ``wip`` replaces
    v1, the item ``wip`` shows, so its ``base`` is v1's ETag (ruling R38),
    and pushing it into main is a conflict, not a silent overwrite of v2."""
    Given.http.force_authenticate(admin_login)
    v1 = push_create("ui-components", component(name="row", template="<b>1</b>"))
    push_create(
        "ui-branches",
        {"name": "wip", "rbac_context": "alice", "parent": "main", "base": "pinned"},
    )
    put_json(
        reverse("ui-components-detail", args=[v1.pk]),
        {**v1.content, "template": "<b>2</b>"},
        HTTP_IF_MATCH=f'"{v1.etag}"',
    )
    Then.http.status_code_is(200)
    v2 = Resource.objects.get(pk=v1.pk)
    assert v2.etag != v1.etag
    override = push_create(
        "ui-components", component(name="row", template="<i>wip</i>", **WIP)
    )
    assert override.content["base"] == v1.etag
    push()
    Then.http.status_code_is(409)
    assert Resource.objects.get(pk=v1.pk).content["template"] == "<b>2</b>"
    assert problem()["conflicts"] == [
        {"kind": "component", "key": ["row"], "base": v1.etag, "current": v2.etag}
    ]


@pytest.mark.django_db
@test_context
def test_a_version_without_etags_bases_overrides_on_its_own_key(admin_login):
    """A version recorded before versions kept ETags gives its items an ETag
    of its own (``v<number>:<branch>:<key>``), which no live item has: an
    override based on it conflicts with the parent's item, never silently
    overwrites it (ruling R38)."""
    Given.http.force_authenticate(admin_login)
    push_create("ui-components", component(name="row", template="<b>1</b>"))
    (number,) = versions_of("main")
    legacy = dict(snapshot("main", number))
    legacy.pop("etags")
    Resource.objects.filter(content__branch="main", content__number=number).update(
        content=legacy
    )
    push_create(
        "ui-branches",
        {"name": "wip", "rbac_context": "alice", "parent": "main", "base": "pinned"},
    )
    override = push_create(
        "ui-components", component(name="row", template="<i>wip</i>", **WIP)
    )
    assert override.content["base"] == f"v{number}:main:row"
    push()
    Then.http.status_code_is(409)
