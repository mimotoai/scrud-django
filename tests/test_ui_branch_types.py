"""The UI branch resource type, and UI components and stylesheets on
branches: who may write them, and in which context."""

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.registration import ResourceRegistration
from scrud_django.ui.branches import ITEM_KINDS, MAIN
from scrud_django.ui.branches.model import (
    MAIN_BRANCH,
    Branch,
    applies_to,
    authorizing_context,
    branch_of,
    item_key,
    kind_of,
)
from scrud_django.ui.branches.store import (
    BranchChainError,
    ancestors,
    branches,
    ensure_main,
    published_root,
)
from scrud_django.ui.cascade import rules_memo

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_http import PREFIXES, make_component, make_stylesheet


def make_branch(name, context, parent="main", **fields):
    """A branch stored as is (not through the API)."""
    reverse("ui-branches-list")
    return ResourceRegistration.register(
        {"name": name, "rbac_context": context, "parent": parent, **fields},
        "ui-branches",
    )


def component(**overrides):
    return {
        "name": "todo-row",
        "engine": "django",
        "mode": "view",
        "template": "<b>{{ value }}</b>",
        **overrides,
    }


def stylesheet(**overrides):
    return {
        "name": "theme",
        "rules": [{"select": "array", "component": "todo-row"}],
        **overrides,
    }


def create(slug, body):
    When.http.post(reverse(f"{slug}-list"), data=body, format="json")


def problem():
    return Http.response.json()


@pytest.mark.django_db
@test_context
def test_branch_create_needs_grant_in_its_context(ui_alice_login, ui_bob_login):
    body = {"name": "alice-wip", "rbac_context": "alice", "parent": "main"}
    Given.http.force_authenticate(ui_alice_login)
    create("ui-branches", body)
    Then.http.status_code_is(201)
    created = Http.response.json()
    assert created["base"] == "live"
    # main's current version when alice-wip was created: its first, recorded
    # then (main had none).
    assert created["branching_point"] == {"main": 1}
    create("ui-branches", {**body, "name": "bob-wip", "rbac_context": "bob"})
    Then.http.status_code_is(403)
    Given.http.force_authenticate(ui_bob_login)
    create("ui-branches", {**body, "name": "alice-wip-2"})
    Then.http.status_code_is(403)


@pytest.mark.django_db
@test_context
def test_branch_refusals(ui_alice_login):
    a = make_branch("a", "alice")
    make_branch("b", "alice", parent="a")
    make_branch("pub", "alice", parent=None, prefix="/admin/")
    Given.http.force_authenticate(ui_alice_login)
    refused = [
        {"name": "a", "rbac_context": "alice", "parent": "main"},
        {"name": "x", "rbac_context": "alice", "parent": "nope"},
        {"name": "Main!", "rbac_context": "alice", "parent": "main"},
        {"name": "pub-2", "rbac_context": "alice", "parent": None, "prefix": "/admin/"},
        {"name": "pub-3", "rbac_context": "alice", "parent": None, "prefix": "/admin"},
        {"name": "pinned", "rbac_context": "alice", "parent": None, "base": "pinned"},
    ]
    details = []
    for body in refused:
        create("ui-branches", body)
        Then.http.status_code_is(400)
        assert problem()["type"].endswith("/ui-branch-invalid"), body
        details.append(problem()["detail"])
    # re-parenting a under its child b
    When.http.put(
        reverse("ui-branches-detail", args=[a.pk]),
        data={"name": "a", "rbac_context": "alice", "parent": "b"},
        format="json",
        HTTP_IF_MATCH=f'"{a.etag}"',
    )
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-branch-invalid")
    details.append(problem()["detail"])
    assert len(set(details)) == 7, details


@pytest.mark.django_db
@test_context
def test_main_is_reserved(admin_login):
    """`main` is the published branch at the API URLs: context "", prefix "",
    no parent (ruling R1)."""
    Given.http.force_authenticate(admin_login)
    main = {"name": "main", "rbac_context": "", "parent": None, "prefix": ""}
    for wrong in (
        {"rbac_context": "alice"},
        {"prefix": "/main/"},
        {"prefix": None},
        {"parent": "other"},
    ):
        create("ui-branches", {**main, **wrong})
        Then.http.status_code_is(400)
        assert problem()["type"].endswith("/ui-branch-invalid"), wrong
        assert "main" in problem()["detail"]
    create("ui-branches", main)
    Then.http.status_code_is(201)


@pytest.mark.django_db
@test_context
def test_item_branch_defaults_to_main_and_phase1_bodies_still_work(
    ui_author_login, ui_acme_stylesheet_login
):
    # Stored by phase 1's helpers, without a branch.
    assert make_component("raw-row", "<i>{{ value }}</i>").content.get("branch") is None
    assert make_stylesheet("raw-theme", [("array", "raw-row")]).pk
    # The bodies those helpers store, written through the API.
    Given.http.force_authenticate(ui_author_login)
    create("ui-components", component(rbac_context=""))
    Then.http.status_code_is(201)
    When.http.get(Http.response["Location"])
    Then.http.status_code_is(200)
    assert Http.response.json()["branch"] == "main"
    Given.http.force_authenticate(ui_acme_stylesheet_login)
    sheet = {
        "name": "theme",
        "prefixes": PREFIXES,
        "rules": [{"select": "array", "component": "todo-row"}],
        "rbac_context": "acme",
    }
    create("ui-stylesheets", sheet)
    Then.http.status_code_is(201)
    When.http.get(Http.response["Location"])
    Then.http.status_code_is(200)
    stored = Http.response.json()
    assert stored["branch"] == "main"
    assert stored["applies_to"] == "acme"
    assert stored["rbac_context"] == "acme"


@pytest.mark.django_db
@test_context
def test_item_rbac_context_must_be_the_authorizing_context(
    ui_author_login, ui_alice_login
):
    make_branch("alice-wip", "alice")
    # The author may write components in "": past the grant check, refused by
    # the branch check.
    Given.http.force_authenticate(ui_author_login)
    create("ui-components", component(branch="alice-wip", rbac_context=""))
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-branch-context")
    assert "'alice'" in problem()["detail"]
    Given.http.force_authenticate(ui_alice_login)
    create("ui-components", component(branch="alice-wip", rbac_context="alice"))
    Then.http.status_code_is(201)
    assert Http.response.json()["branch"] == "alice-wip"
    # A stylesheet on a working branch applies everywhere unless it says.
    create("ui-stylesheets", stylesheet(branch="alice-wip", rbac_context="alice"))
    Then.http.status_code_is(201)
    assert Http.response.json()["applies_to"] == ""


@pytest.mark.django_db
@test_context
def test_published_stylesheet_authorized_in_applies_to(
    ui_acme_stylesheet_login, ui_author_login
):
    Given.http.force_authenticate(ui_acme_stylesheet_login)
    create("ui-stylesheets", stylesheet(applies_to="acme", rbac_context="acme"))
    Then.http.status_code_is(201)
    Given.http.force_authenticate(ui_author_login)
    create(
        "ui-stylesheets", stylesheet(name="other", applies_to="acme", rbac_context="")
    )
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-branch-context")
    assert "'acme'" in problem()["detail"]


@pytest.mark.django_db
@test_context
def test_unknown_branch_is_400(ui_author_login):
    Given.http.force_authenticate(ui_author_login)
    create("ui-components", component(branch="nope", rbac_context=""))
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-branch-invalid")
    assert "'nope'" in problem()["detail"]


@pytest.mark.django_db
@test_context
def test_names_are_unique_per_branch(ui_author_login, ui_alice_login):
    make_branch("alice-wip", "alice")
    Given.http.force_authenticate(ui_author_login)
    create("ui-components", component())
    Then.http.status_code_is(201)
    create("ui-stylesheets", stylesheet())
    Then.http.status_code_is(201)
    Given.http.force_authenticate(ui_alice_login)
    on_branch = {"branch": "alice-wip", "rbac_context": "alice"}
    create("ui-components", component(**on_branch))
    Then.http.status_code_is(201)
    create("ui-stylesheets", stylesheet(**on_branch))
    Then.http.status_code_is(201)
    create("ui-components", component(template="<i>2</i>", **on_branch))
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-component-name-taken")
    create("ui-stylesheets", stylesheet(**on_branch))
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-stylesheet-name-taken")


@pytest.mark.django_db
@test_context
def test_deletion_marker_needs_only_name(ui_alice_login):
    make_branch("alice-wip", "alice")
    Given.http.force_authenticate(ui_alice_login)
    create(
        "ui-components",
        {
            "name": "todo-row",
            "branch": "alice-wip",
            "deleted": True,
            "rbac_context": "alice",
        },
    )
    Then.http.status_code_is(201)
    create(
        "ui-components",
        {"name": "other", "branch": "alice-wip", "rbac_context": "alice"}
        | {"engine": "django", "mode": "view"},
    )
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/json-invalid")
    assert "template" in problem()["detail"]
    create(
        "ui-stylesheets",
        {
            "name": "gone",
            "branch": "alice-wip",
            "deleted": True,
            "rbac_context": "alice",
        },
    )
    Then.http.status_code_is(201)
    create(
        "ui-stylesheets",
        {"name": "theme", "branch": "alice-wip", "rbac_context": "alice"},
    )
    Then.http.status_code_is(400)
    assert "rules" in problem()["detail"]


def branch(name, parent=None, prefix=None, context=""):
    return Branch(name, context, parent, "live", {}, prefix, None)


def test_ancestors_nearest_first_and_published_root():
    chain = {
        b.name: b
        for b in (
            MAIN_BRANCH,
            branch("testing", parent="main"),
            branch("admin", parent="testing", prefix="/admin/"),
            branch("alice", parent="admin"),
        )
    }
    assert [b.name for b in ancestors("alice", chain)] == ["admin", "testing", "main"]
    assert ancestors("main", chain) == []
    assert published_root("alice", chain).name == "admin"
    assert published_root("admin", chain).name == "admin"
    assert published_root("testing", chain) is MAIN_BRANCH


@pytest.mark.parametrize(
    "chain",
    [
        {"a": branch("a", parent="gone")},
        {"a": branch("a", parent="b"), "b": branch("b", parent="a")},
        {"a": branch("a", parent="a")},
    ],
)
def test_ancestors_refuse_a_missing_parent_or_a_cycle(chain):
    with pytest.raises(BranchChainError):
        ancestors("a", chain)


def test_items_are_keyed_and_authorized_on_main_without_a_query():
    """Everything an item on main needs comes from `MAIN_BRANCH`."""
    assert kind_of(ITEM_KINDS["stylesheet"]) == "stylesheet"
    assert kind_of("http://example.org/Other") is None
    assert branch_of({"name": "x"}) == MAIN
    acme = {"name": "theme", "rbac_context": "acme"}
    assert applies_to(acme, MAIN_BRANCH) == "acme"
    assert item_key("stylesheet", acme, MAIN_BRANCH) == ("theme", "acme")
    assert authorizing_context("stylesheet", acme, MAIN_BRANCH) == "acme"
    assert authorizing_context("component", acme, MAIN_BRANCH) == ""
    assert item_key("component", acme, MAIN_BRANCH) == ("theme",)
    wip = branch("alice-wip", parent="main", context="alice")
    assert applies_to(acme, wip) == ""
    assert authorizing_context("stylesheet", acme, wip) == "alice"
    assert authorizing_context("stylesheet", {**acme, "applies_to": "acme"}, wip) == (
        "alice"
    )


@pytest.mark.django_db
@test_context
def test_main_is_virtual_until_an_item_is_written_to_it(ui_author_login):
    reverse("ui-branches-list")
    assert branches()[MAIN] == MAIN_BRANCH
    make_branch("alice-wip", "alice")
    Given.http.force_authenticate(ui_author_login)
    create("ui-components", component())
    Then.http.status_code_is(201)
    stored = branches()
    assert stored[MAIN].pk is not None
    assert stored[MAIN] == Branch(MAIN, "", None, "live", {}, "", stored[MAIN].pk)
    assert stored["alice-wip"].parent == MAIN
    create("ui-components", component(name="second"))
    Then.http.status_code_is(201)
    assert branches()[MAIN].pk == stored[MAIN].pk
    assert ensure_main(None).pk == stored[MAIN].pk


@pytest.mark.django_db
def test_branches_are_read_once_in_a_rules_memo(django_assert_num_queries):
    reverse("ui-branches-list")
    with rules_memo():
        with django_assert_num_queries(1):
            assert branches() is branches()


@pytest.mark.django_db
@test_context
def test_an_item_stays_on_its_branch(ui_alice_login):
    """A PUT that names no branch keeps the item's; one naming another is
    refused (moving items between branches is a push)."""
    make_branch("alice-wip", "alice")
    make_branch("alice-2", "alice")
    Given.http.force_authenticate(ui_alice_login)
    create("ui-components", component(branch="alice-wip", rbac_context="alice"))
    Then.http.status_code_is(201)
    url, etag = Http.response["Location"], Http.response["ETag"]
    When.http.put(
        url,
        data=component(template="<i>2</i>", rbac_context="alice"),
        format="json",
        HTTP_IF_MATCH=etag,
    )
    Then.http.status_code_is(200)
    assert Http.response.json()["branch"] == "alice-wip"
    When.http.put(
        url,
        data=component(branch="alice-2", rbac_context="alice"),
        format="json",
        HTTP_IF_MATCH=Http.response["ETag"],
    )
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-branch-invalid")


@pytest.mark.django_db
@test_context
def test_a_branch_keeps_its_name(ui_alice_login):
    wip = make_branch("alice-wip", "alice")
    Given.http.force_authenticate(ui_alice_login)
    When.http.put(
        reverse("ui-branches-detail", args=[wip.pk]),
        data={"name": "alice-new", "rbac_context": "alice", "parent": "main"},
        format="json",
        HTTP_IF_MATCH=f'"{wip.etag}"',
    )
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-branch-invalid")
    When.http.put(
        reverse("ui-branches-detail", args=[wip.pk]),
        data={"name": "alice-wip", "rbac_context": "alice", "parent": "main"}
        | {"label": "Alice's work"},
        format="json",
        HTTP_IF_MATCH=f'"{wip.etag}"',
    )
    Then.http.status_code_is(200)
    assert Http.response.json()["branching_point"] == {}
