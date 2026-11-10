"""Pulling a branch: its branching point moves to its ancestors' current
versions, and the overrides whose item changed in the parent are listed as
conflicts until each is resolved (keep yours with ``rebase``, or drop it)."""

import re

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.registration import write_db
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI
from scrud_django.ui.branches.versions import current_version

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_render import (
    may_select_branches,
    render_with,
    setup_main_and_wip,
)
from .test_ui_branch_types import component, problem
from .test_ui_branch_versions import versions_of
from .test_ui_http import make_component

WIP = {"branch": "wip", "rbac_context": "alice"}


def rendered(todo_list, branch=None):
    """The HTML of the to-do list on `branch`, but for its per-response CSRF
    token."""
    page = render_with(todo_list, branch)
    return re.sub(r'(name="csrf-token"\s+content=")[^"]*', r"\1", page)


def quoted(etag):
    return f'"{etag}"'


def stored_branch(name):
    return Resource.objects.get(
        resource_type__type_uri=UI_BRANCH_TYPE_URI, content__name=name
    )


def pull_body(**fields):
    return {
        "name": "wip",
        "rbac_context": "alice",
        "parent": "main",
        "pull": True,
        **fields,
    }


def put_branch(body):
    stored = stored_branch("wip")
    When.http.put(
        reverse("ui-branches-detail", args=[stored.pk]),
        data=body,
        format="json",
        HTTP_IF_MATCH=quoted(stored.etag),
    )


def put_item(resource, body):
    When.http.put(
        reverse("ui-components-detail", args=[resource.pk]),
        data=body,
        format="json",
        HTTP_IF_MATCH=quoted(resource.etag),
    )


def conflicted(admin_login):
    """`wip` overrides `row`, then main edits `row` and `wip` is pulled.
    Returns the main row's first and current ETags, and wip's override."""
    Given.http.force_authenticate(admin_login)
    main_row = make_component("row", "<b>1</b>")
    first_etag = main_row.etag
    When.http.post(
        reverse("ui-branches-list"),
        data={"name": "wip", "rbac_context": "alice", "parent": "main"},
        format="json",
    )
    Then.http.status_code_is(201)
    When.http.post(
        reverse("ui-components-list"),
        data=component(name="row", template="<i>mine</i>", **WIP),
        format="json",
    )
    Then.http.status_code_is(201)
    override = Resource.objects.get(
        resource_type__slug="ui-components", content__branch="wip"
    )
    assert override.content["base"] == first_etag
    put_item(main_row, component(name="row", template="<b>2</b>", rbac_context=""))
    Then.http.status_code_is(200)
    main_row.refresh_from_db()
    put_branch(pull_body())
    Then.http.status_code_is(200)
    return first_etag, main_row.etag, override


@pytest.mark.django_db
@test_context
def test_pull_moves_branching_point_and_lists_conflicts(admin_login):
    first, current, _override = conflicted(admin_login)
    body = Http.response.json()
    assert "pull" not in body
    assert body["branching_point"] == {"main": 2}
    assert body["conflicts"] == [
        {"kind": "component", "key": ["row"], "base": first, "current": current}
    ]
    assert stored_branch("wip").content["branching_point"] == {"main": 2}
    # A pull is a version of the branch.
    assert versions_of("wip") == [1, 2]


@pytest.mark.django_db
@test_context
def test_pull_without_conflicts_lists_none(admin_login):
    Given.http.force_authenticate(admin_login)
    make_component("row", "<b/>")
    When.http.post(
        reverse("ui-branches-list"),
        data={"name": "wip", "rbac_context": "alice", "parent": "main"},
        format="json",
    )
    Then.http.status_code_is(201)
    make_component("title", "<h1/>")
    put_branch(pull_body())
    Then.http.status_code_is(200)
    assert Http.response.json()["conflicts"] == []
    assert Http.response.json()["branching_point"] == {"main": 2}


@pytest.mark.django_db
@test_context
def test_pull_needs_put_on_branch(todo_reader_login):
    setup_main_and_wip()
    stored = stored_branch("wip")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    put_branch(pull_body(rbac_context="project-a"))
    Then.http.status_code_is(403)
    assert stored_branch("wip").etag == stored.etag
    assert "pull" not in stored_branch("wip").content


@pytest.mark.django_db
@test_context
def test_clients_cannot_set_conflicts(admin_login):
    first, current, _override = conflicted(admin_login)
    put_branch({**pull_body(), "pull": False, "conflicts": []})
    Then.http.status_code_is(200)
    assert [c["key"] for c in Http.response.json()["conflicts"]] == [["row"]]
    # And a create starts with none.
    When.http.post(
        reverse("ui-branches-list"),
        data={
            "name": "other",
            "rbac_context": "alice",
            "parent": "main",
            "conflicts": [{"kind": "component"}],
            "pull": True,
        },
        format="json",
    )
    Then.http.status_code_is(201)
    assert Http.response.json().get("conflicts", []) == []
    assert "pull" not in Http.response.json()


@pytest.mark.django_db
@test_context
def test_rebase_resolves_conflict(admin_login):
    _first, current, override = conflicted(admin_login)
    mine = component(name="row", template="<i>still mine</i>", **WIP)
    # An ordinary edit does not resolve the conflict.
    put_item(override, mine)
    Then.http.status_code_is(200)
    assert [c["key"] for c in stored_branch("wip").content["conflicts"]] == [["row"]]
    override.refresh_from_db()
    put_item(override, {**mine, "rebase": True})
    Then.http.status_code_is(200)
    assert "rebase" not in Http.response.json()
    assert Http.response.json()["base"] == current
    override.refresh_from_db()
    assert "rebase" not in override.content
    assert override.content["base"] == current
    assert stored_branch("wip").content["conflicts"] == []


@pytest.mark.django_db
@test_context
def test_dropping_override_resolves_conflict(admin_login):
    _first, _current, override = conflicted(admin_login)
    When.http.delete(
        reverse("ui-components-detail", args=[override.pk]),
        HTTP_IF_MATCH=quoted(override.etag),
    )
    Then.http.status_code_is(204)
    assert stored_branch("wip").content["conflicts"] == []


@pytest.mark.django_db
@test_context
def test_rebase_is_refused_where_there_is_nothing_to_keep(admin_login):
    Given.http.force_authenticate(admin_login)
    main_row = make_component("row", "<b/>")
    # On main (no parent).
    put_item(main_row, {**component(name="row", rbac_context=""), "rebase": True})
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-branch-invalid")
    # On create.
    When.http.post(
        reverse("ui-branches-list"),
        data={"name": "wip", "rbac_context": "alice", "parent": "main"},
        format="json",
    )
    Then.http.status_code_is(201)
    When.http.post(
        reverse("ui-components-list"),
        data={**component(name="row", **WIP), "rebase": True},
        format="json",
    )
    Then.http.status_code_is(400)
    main_row.refresh_from_db()
    assert "rebase" not in main_row.content


@pytest.mark.django_db
@test_context
def test_pull_of_live_branch_changes_no_rendering(
    todo_list, todo_reader_login, admin_login
):
    setup_main_and_wip()
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    before = rendered(todo_list, "wip")
    main_before = rendered(todo_list)
    make_component("extra", "<u/>")
    Given.http.force_authenticate(admin_login)
    put_branch(pull_body(rbac_context="project-a"))
    Then.http.status_code_is(200)
    assert Http.response.json()["branching_point"] == {
        "main": current_version("main", write_db())
    }
    Given.http.force_authenticate(todo_reader_login)
    assert rendered(todo_list, "wip") == before
    assert rendered(todo_list) == main_before
