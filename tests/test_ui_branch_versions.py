"""Branch versions: every write to a branch's own items records its next
version; a branch records its ancestors' versions when it is created; and a
branch's context and kind (published or working) are fixed once items depend
on them."""

import logging
import time

import pytest
from django.db import connections, transaction
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.exceptions import UIBranchVersionMissingError
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration, write_db
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI, UI_BRANCH_VERSION_TYPE_URI
from scrud_django.ui.branches.versions import (
    batched_versions,
    current_version,
    record_version,
    snapshot,
)

from .fixtures import *  # noqa
from .fixtures import assign_ui_role
from .steps import *  # noqa: F403, F401
from .test_delete_locking import TWO_WRITERS, run_together
from .test_ui_branch_types import component, make_branch, problem, stylesheet
from .test_ui_http import make_component

VERSIONS = "ui-branch-versions"


def store(slug, body):
    """An item stored as is (not through the API)."""
    reverse(f"{slug}-list")
    return ResourceRegistration.register(body, slug)


def on(branch, context, **fields):
    """A component on `branch`, stored as is."""
    return store(
        "ui-components",
        component(branch=branch, rbac_context=context, **fields),
    )


def versions_of(name):
    """The numbers of branch `name`'s stored versions, in order."""
    return [
        content["number"]
        for content in Resource.objects.filter(
            resource_type__type_uri=UI_BRANCH_VERSION_TYPE_URI, content__branch=name
        )
        .order_by("pk")
        .values_list("content", flat=True)
    ]


def put(url, body, etag):
    When.http.put(url, data=body, format="json", HTTP_IF_MATCH=etag)


@pytest.mark.django_db
@test_context
def test_every_item_write_creates_a_version(ui_alice_login):
    make_branch("wip", "alice")
    on_wip = {"branch": "wip", "rbac_context": "alice"}
    Given.http.force_authenticate(ui_alice_login)
    When.http.post(
        reverse("ui-components-list"), data=component(**on_wip), format="json"
    )
    Then.http.status_code_is(201)
    url, etag = Http.response["Location"], Http.response["ETag"]
    created = Http.response.json()
    put(url, component(template="<i>2</i>", **on_wip), etag)
    Then.http.status_code_is(200)
    updated = Http.response.json()
    When.http.delete(url, HTTP_IF_MATCH=Http.response["ETag"])
    Then.http.status_code_is(204)

    assert versions_of("wip") == [1, 2, 3]
    expected = {1: [created], 2: [updated], 3: []}
    for number, components in expected.items():
        version = snapshot("wip", number)
        assert version["branch"] == "wip"
        assert version["number"] == number
        assert version["rbac_context"] == "alice"
        assert version["ancestors"] == {}
        assert version["items"] == {"component": components, "stylesheet": []}
    assert current_version("wip", write_db()) == 3
    # The items of main are not the branch's: main has no version.
    assert versions_of("main") == []


@pytest.mark.django_db
@test_context
def test_main_versions_hold_items_stored_before_branches(ui_author_login):
    old = make_component("old-row", "<i>{{ value }}</i>")
    assert "branch" not in old.content
    assert versions_of("main") == [1]
    Given.http.force_authenticate(ui_author_login)
    When.http.post(
        reverse("ui-stylesheets-list"),
        data=stylesheet(rbac_context=""),
        format="json",
    )
    Then.http.status_code_is(201)
    sheet = Http.response.json()
    assert versions_of("main") == [1, 2]
    version = snapshot("main", 2)
    assert version["rbac_context"] == ""
    assert version["items"] == {"component": [old.content], "stylesheet": [sheet]}


@pytest.mark.django_db
@test_context
def test_branching_point_records_every_ancestor(admin_login):
    Given.http.force_authenticate(admin_login)
    make_component("row", "<b/>")
    make_component("title", "<h1/>")
    assert versions_of("main") == [1, 2]

    def create_branch(name, parent):
        When.http.post(
            reverse("ui-branches-list"),
            data={"name": name, "rbac_context": "alice", "parent": parent},
            format="json",
        )
        Then.http.status_code_is(201)
        return Http.response.json()

    testing = create_branch("testing", "main")
    assert testing["branching_point"] == {"main": 2}
    # testing has no version yet: creating alice records its first.
    make_component("footer", "<p/>")
    alice = create_branch("alice", "testing")
    assert alice["branching_point"] == {"testing": 1, "main": 3}
    assert snapshot("testing", 1)["items"] == {"component": [], "stylesheet": []}
    # A PUT keeps it, whatever the body says.
    stored = Resource.objects.get(
        resource_type__type_uri=UI_BRANCH_TYPE_URI, content__name="alice"
    )
    put(
        reverse("ui-branches-detail", args=[stored.pk]),
        {"name": "alice", "rbac_context": "alice", "parent": "testing"}
        | {"branching_point": {"main": 1}, "label": "Alice"},
        f'"{stored.etag}"',
    )
    Then.http.status_code_is(200)
    assert Http.response.json()["branching_point"] == {"testing": 1, "main": 3}


@pytest.mark.django_db
@test_context
def test_versions_are_read_only(admin_login):
    make_component("row", "<b/>")
    version = Resource.objects.get(resource_type__type_uri=UI_BRANCH_VERSION_TYPE_URI)
    detail = reverse(f"{VERSIONS}-detail", args=[version.pk])
    Given.http.force_authenticate(admin_login)
    When.http.get(detail)
    Then.http.status_code_is(200)
    etag = Http.response["ETag"]
    body = {**version.content, "number": 7}
    When.http.post(reverse(f"{VERSIONS}-list"), data=body, format="json")
    Then.http.status_code_is(405)
    put(detail, body, etag)
    Then.http.status_code_is(405)
    When.http.delete(detail, HTTP_IF_MATCH=etag)
    Then.http.status_code_is(405)
    assert versions_of("main") == [1]
    assert Resource.objects.get(pk=version.pk).content == version.content


@pytest.mark.django_db
@test_context
def test_versions_are_read_in_the_branchs_context():
    make_branch("wip", "alice")
    make_component("row", "<b/>")
    on("wip", "alice")
    reader = assign_ui_role((UI_BRANCH_VERSION_TYPE_URI,), "alice")
    Given.http.force_authenticate(reader)
    When.http.get(reverse(f"{VERSIONS}-list"))
    Then.http.status_code_is(200)
    listed = [entry["content"] for entry in Http.response.json()["content"]]
    assert [(v["branch"], v["number"]) for v in listed] == [("wip", 1)]
    main_version = Resource.objects.get(
        resource_type__type_uri=UI_BRANCH_VERSION_TYPE_URI, content__branch="main"
    )
    When.http.get(reverse(f"{VERSIONS}-detail", args=[main_version.pk]))
    Then.http.status_code_is(404)


@pytest.mark.django_db
def test_snapshot_of_a_missing_version_is_404():
    make_branch("wip", "alice")
    with pytest.raises(UIBranchVersionMissingError) as raised:
        snapshot("wip", 1)
    assert raised.value.status_code == 404
    assert current_version("wip", write_db()) == 1
    assert snapshot("wip", 1)["number"] == 1
    with pytest.raises(UIBranchVersionMissingError):
        snapshot("wip", 2)


@pytest.mark.django_db
def test_a_batch_records_one_version_per_branch():
    make_branch("wip", "alice")
    with transaction.atomic(), batched_versions():
        on("wip", "alice", name="row")
        on("wip", "alice", name="title")
        make_component("main-row", "<b/>")
        assert versions_of("wip") == versions_of("main") == []
    assert versions_of("wip") == [1]
    assert versions_of("main") == [1]
    names = [c["name"] for c in snapshot("wip", 1)["items"]["component"]]
    assert names == ["row", "title"]


@pytest.mark.django_db
def test_an_item_on_a_missing_branch_records_no_version(caplog):
    with caplog.at_level(logging.WARNING, logger="scrud_django.ui.branches"):
        on("gone", "alice")
    assert versions_of("gone") == []
    assert "'gone'" in caplog.text


# Ruling R7: a branch's context, and whether it is published, decide the
# authorizing context of its items.


def branch_url(stored):
    return reverse("ui-branches-detail", args=[stored.pk])


@pytest.mark.django_db
@test_context
def test_a_branchs_context_is_fixed(admin_login):
    wip = make_branch("wip", "alice")
    Given.http.force_authenticate(admin_login)
    put(
        branch_url(wip),
        {"name": "wip", "rbac_context": "bob", "parent": "main"},
        f'"{wip.etag}"',
    )
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-branch-invalid")
    assert "rbac_context" in problem()["detail"]
    wip.refresh_from_db()
    assert wip.content["rbac_context"] == "alice"


@pytest.mark.django_db
@test_context
def test_a_published_branch_may_change_its_prefix(admin_login):
    admin = make_branch("admin", "alice", parent=None, prefix="/admin/")
    on("admin", "alice")
    store(
        "ui-stylesheets",
        stylesheet(branch="admin", applies_to="acme", rbac_context="acme"),
    )
    Given.http.force_authenticate(admin_login)
    put(
        branch_url(admin),
        {"name": "admin", "rbac_context": "alice", "parent": None, "prefix": "/ops/"},
        f'"{admin.etag}"',
    )
    Then.http.status_code_is(200)
    assert Http.response.json()["prefix"] == "/ops/"


@pytest.mark.parametrize("published", [True, False])
@pytest.mark.django_db
@test_context
def test_publishing_is_fixed_while_stylesheets_apply_elsewhere(admin_login, published):
    before, after = ("/wip/", None) if published else (None, "/wip/")
    wip = make_branch("wip", "alice", prefix=before)
    # A stylesheet that applies everywhere is authorized in the branch's
    # context either way.
    store(
        "ui-stylesheets",
        stylesheet(
            name="everywhere", branch="wip", applies_to="", rbac_context="alice"
        ),
    )
    Given.http.force_authenticate(admin_login)
    body = {"name": "wip", "rbac_context": "alice", "parent": "main"}
    put(branch_url(wip), {**body, "prefix": after}, f'"{wip.etag}"')
    Then.http.status_code_is(200)
    switched_back = Http.response["ETag"]
    put(branch_url(wip), {**body, "prefix": before}, switched_back)
    Then.http.status_code_is(200)
    # One that applies to acme is authorized in acme only on a published
    # branch: switching would change who may write it.
    store(
        "ui-stylesheets",
        stylesheet(
            name="acme-only",
            branch="wip",
            applies_to="acme",
            rbac_context="acme" if published else "alice",
        ),
    )
    put(branch_url(wip), {**body, "prefix": after}, Http.response["ETag"])
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-branch-invalid")
    assert "'acme-only'" in problem()["detail"]


# Review Focus 2: two writes to one branch at once.


@TWO_WRITERS
@pytest.mark.django_db(transaction=True)
def test_concurrent_writes_get_distinct_numbers(committed_types):
    make_branch("wip", "alice")
    db = write_db()
    connections.close_all()

    def write():
        with transaction.atomic(using=db):
            number = record_version("wip", db)
            # Hold the branch's lock: the other write waits for it.
            time.sleep(0.4)
        return number

    first, second = run_together(write, write, delay=0.1)
    assert not isinstance(first, Exception), repr(first)
    assert not isinstance(second, Exception), repr(second)
    assert sorted([first, second]) == [1, 2]
    assert versions_of("wip") == [1, 2]
