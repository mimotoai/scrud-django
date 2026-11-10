"""Pushing a branch into another (``ui-pushes``): a push moved into
``applied`` writes the source's overrides into the target as one version and
empties the source, or is refused (409 on a conflict, 403 without the grant a
direct write of each item into the target needs) and writes nothing. A
restore (``restore: N``) brings back the target's overrides of version N."""

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django import collection_type_uri_for
from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)
from scrud_django.ui.branches import (
    ITEM_KINDS,
    UI_BRANCH_TYPE_URI,
    UI_BRANCH_VERSION_TYPE_URI,
    UI_PUSH_TYPE_URI,
)
from scrud_django.ui.branches.versions import snapshot
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import assign_ui_role
from .steps import *  # noqa: F403, F401
from .test_ui_branch_types import component, problem, stylesheet
from .test_ui_branch_versions import versions_of
from .test_ui_http import grant, make_component, make_stylesheet

WIP = {"branch": "wip", "rbac_context": "alice"}
UI_TYPES = (
    *ITEM_KINDS.values(),
    UI_BRANCH_TYPE_URI,
    UI_BRANCH_VERSION_TYPE_URI,
    UI_PUSH_TYPE_URI,
)


def quoted(etag):
    return f'"{etag}"'


def stored_state():
    """Every stored UI item, branch, version and push, as ``(type URI, pk,
    ETag)``: equal before and after a request that writes nothing."""
    return sorted(
        Resource.objects.filter(resource_type__type_uri__in=UI_TYPES).values_list(
            "resource_type__type_uri", "pk", "etag"
        )
    )


def own_rows(type_uri, branch):
    """The content of every stored item of `type_uri` on `branch`, lowest pk
    first (items without a ``branch`` are main's)."""
    return [
        content
        for content in Resource.objects.filter(resource_type__type_uri=type_uri)
        .order_by("pk")
        .values_list("content", flat=True)
        if content.get("branch", "main") == branch
    ]


def stored_branch(name):
    return Resource.objects.get(
        resource_type__type_uri=UI_BRANCH_TYPE_URI, content__name=name
    )


def created():
    """The resource the last response's ``Location`` names."""
    return Resource.objects.get(
        pk=int(Http.response["Location"].rstrip("/").rsplit("/", 1)[1])
    )


def create(slug, body):
    When.http.post(reverse(f"{slug}-list"), data=body, format="json")
    Then.http.status_code_is(201)
    return created()


def make_wip(admin_login, name="wip", parent="main", context="alice"):
    """Branch `name` (in `context`, over `parent`) created through the API by
    the superuser."""
    Given.http.force_authenticate(admin_login)
    return create(
        "ui-branches", {"name": name, "rbac_context": context, "parent": parent}
    )


def wip_overrides_row(admin_login):
    """main has ``row``; ``wip`` (over main) overrides it. Returns main's row
    and wip's override."""
    Given.http.force_authenticate(admin_login)
    main_row = make_component("row", "<b>main</b>")
    make_wip(admin_login)
    override = create(
        "ui-components", component(name="row", template="<i>wip</i>", **WIP)
    )
    return main_row, override


def push(target="main", source="wip", state="applied", context="", **fields):
    When.http.post(
        reverse("ui-pushes-list"),
        data={
            "source": source,
            "target": target,
            "restore": None,
            "state": state,
            "rbac_context": context,
            **fields,
        },
        format="json",
    )


def put_push(stored, **fields):
    When.http.put(
        reverse("ui-pushes-detail", args=[stored.pk]),
        data={**stored.content, **fields},
        format="json",
        HTTP_IF_MATCH=quoted(stored.etag),
    )


def pusher(**item_grants):
    """A user who may write pushes in "" and GET and PUT branches in
    "alice", plus `item_grants` (``{context: {method: [type URI, ...]}}``)."""
    user = assign_ui_role((UI_PUSH_TYPE_URI,), "")
    grant(
        user,
        {
            method: [UI_BRANCH_TYPE_URI, collection_type_uri_for(UI_BRANCH_TYPE_URI)]
            for method in ("http.GET", "http.PUT")
        },
        "alice",
    )
    for context, definition in item_grants.items():
        grant(user, definition, context)
    return user


def component_writer():
    return pusher(**{"": {"http.PUT": [UI_COMPONENT_TYPE_URI]}})


@pytest.mark.django_db
@test_context
def test_push_applies_atomically_and_empties_source(admin_login):
    main_row, override = wip_overrides_row(admin_login)
    main_versions = versions_of("main")
    Given.http.force_authenticate(component_writer())
    push()
    Then.http.status_code_is(201)
    body = Http.response.json()
    main_row.refresh_from_db()
    assert main_row.content["template"] == "<i>wip</i>"
    assert main_row.content["branch"] == "main"
    assert main_row.content["rbac_context"] == ""
    # main has no parent: its row keeps its base (none).
    assert main_row.content.get("base") is None
    assert own_rows(UI_COMPONENT_TYPE_URI, "wip") == []
    assert versions_of("main") == [*main_versions, main_versions[-1] + 1]
    assert body["result"] == {"version": versions_of("main")[-1]}
    assert body["state"] == "applied"
    stored = created()
    assert stored.content["result"] == body["result"]
    assert Http.response["ETag"] == quoted(stored.etag)
    # The version holds the pushed item.
    (item,) = snapshot("main", body["result"]["version"])["items"]["component"]
    assert item["template"] == "<i>wip</i>"
    # The source's version (rows emptied) holds no item, and its branching
    # point moved to main's new version, as a pull would.
    wip = stored_branch("wip").content
    assert wip["branching_point"] == {"main": body["result"]["version"]}
    assert snapshot("wip", versions_of("wip")[-1])["items"]["component"] == []


@pytest.mark.django_db
@test_context
def test_push_creates_target_rows_with_their_base(admin_login):
    # A push into a branch with a parent: a new row's base is the parent's
    # current ETag for the key; an existing row keeps its own base.
    Given.http.force_authenticate(admin_login)
    main_row = make_component("row", "<b>main</b>")
    make_wip(admin_login, "team", context="alice")
    team_row = create(
        "ui-components",
        component(name="other", template="<u/>", branch="team", rbac_context="alice"),
    )
    make_wip(admin_login, "mine", parent="team", context="alice")
    on_mine = {"branch": "mine", "rbac_context": "alice"}
    create("ui-components", component(name="row", template="<i/>", **on_mine))
    create("ui-components", component(name="other", template="<s/>", **on_mine))
    push(target="team", source="mine", context="alice")
    Then.http.status_code_is(201)
    rows = {row["name"]: row for row in own_rows(UI_COMPONENT_TYPE_URI, "team")}
    assert rows["row"]["template"] == "<i/>"
    assert rows["row"]["base"] == main_row.etag
    assert rows["row"]["rbac_context"] == "alice"
    assert rows["other"]["template"] == "<s/>"
    assert rows["other"]["base"] is None
    assert Resource.objects.get(pk=team_row.pk).content["template"] == "<s/>"


@pytest.mark.django_db
@test_context
def test_push_conflict_writes_nothing(admin_login):
    main_row, override = wip_overrides_row(admin_login)
    ResourceRegistration.update(
        {**main_row.content, "template": "<b>changed</b>"}, "ui-components", main_row.pk
    )
    main_row.refresh_from_db()
    before = stored_state()
    push()
    Then.http.status_code_is(409)
    body = problem()
    assert body["type"].endswith("/ui-push-conflict")
    assert body["conflicts"] == [
        {
            "kind": "component",
            "key": ["row"],
            "base": override.content["base"],
            "current": main_row.etag,
        }
    ]
    assert "'row'" in body["detail"]
    assert stored_state() == before


@pytest.mark.django_db
@test_context
def test_push_needs_per_item_grant(admin_login):
    _main_row, _override = wip_overrides_row(admin_login)
    create(
        "ui-stylesheets",
        stylesheet(name="acme-theme", applies_to="acme", **WIP),
    )
    before = stored_state()
    # May PUT components in "" (the overridden row), but holds no grant on
    # stylesheets in "acme", where an acme stylesheet on main is authorized.
    Given.http.force_authenticate(component_writer())
    push()
    Then.http.status_code_is(403)
    body = problem()
    assert body["type"].endswith("/ui-push-forbidden")
    assert "acme-theme" in body["detail"]
    assert stored_state() == before
    # With the grant in "acme", the same push applies.
    user = pusher(
        **{
            "": {"http.PUT": [UI_COMPONENT_TYPE_URI]},
            "acme": {"http.POST": [ITEM_KINDS["stylesheet"]]},
        }
    )
    Given.http.force_authenticate(user)
    push()
    Then.http.status_code_is(201)
    (theme,) = [
        row
        for row in own_rows(ITEM_KINDS["stylesheet"], "main")
        if row["name"] == "acme-theme"
    ]
    assert (theme["rbac_context"], theme["applies_to"]) == ("acme", "acme")


@pytest.mark.django_db
@test_context
def test_push_needs_the_grant_on_the_targets_stored_row(admin_login):
    # main's row is stored in "secret" (not the "" a component on main is
    # authorized in now): a direct PUT of it needs a grant in "secret", and
    # so does a push that writes over it.
    Given.http.force_authenticate(admin_login)
    reverse("ui-components-list")
    main_row = ResourceRegistration.register(
        component(name="row", template="<b>main</b>", rbac_context="secret"),
        "ui-components",
    )
    make_wip(admin_login)
    create("ui-components", component(name="row", template="<i>wip</i>", **WIP))
    user = pusher(**{"": {"http.PUT": [UI_COMPONENT_TYPE_URI]}})
    grant(user, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "secret")
    Given.http.force_authenticate(user)
    When.http.put(
        reverse("ui-components-detail", args=[main_row.pk]),
        data=component(name="row", template="<i>wip</i>", rbac_context=""),
        format="json",
        HTTP_IF_MATCH=quoted(main_row.etag),
    )
    Then.http.status_code_is(403)
    before = stored_state()
    push()
    Then.http.status_code_is(403)
    assert problem()["type"].endswith("/ui-push-forbidden")
    assert stored_state() == before
    # Removing it (a deletion marker) needs DELETE in "secret" too.
    wip_row = Resource.objects.get(
        resource_type__type_uri=UI_COMPONENT_TYPE_URI, content__branch="wip"
    )
    ResourceRegistration.update(
        {"name": "row", "deleted": True, **WIP, "base": main_row.etag},
        "ui-components",
        wip_row.pk,
    )
    grant(user, {"http.DELETE": [UI_COMPONENT_TYPE_URI]}, "")
    before = stored_state()
    push()
    Then.http.status_code_is(403)
    assert stored_state() == before
    grant(user, {"http.DELETE": [UI_COMPONENT_TYPE_URI]}, "secret")
    push()
    Then.http.status_code_is(201)
    assert not Resource.objects.filter(pk=main_row.pk).exists()


@pytest.mark.django_db
@test_context
def test_conflicts_on_unreadable_items_are_only_counted(admin_login):
    # Ruling R20: the 409 describes only conflicts on items the pusher may
    # read in the target.
    main_row, override = wip_overrides_row(admin_login)
    theme = make_stylesheet("acme-theme", [("array", "row")], context="acme")
    create("ui-stylesheets", stylesheet(name="acme-theme", applies_to="acme", **WIP))
    for stored, field, value in (
        (main_row, "template", "<b>changed</b>"),
        (theme, "order", 1),
    ):
        ResourceRegistration.update(
            {**stored.content, field: value}, stored.resource_type.slug, stored.pk
        )
    user = pusher(**{"": {"http.GET": [UI_COMPONENT_TYPE_URI]}})
    Given.http.force_authenticate(user)
    before = stored_state()
    push()
    Then.http.status_code_is(409)
    body = problem()
    assert [entry["key"] for entry in body["conflicts"]] == [["row"]]
    assert body["unreadable_conflicts"] == 1
    assert "1 more you may not read" in body["detail"]
    assert "acme" not in str(body)
    assert stored_state() == before
    # A reader of acme's stylesheets is told about both.
    grant(user, {"http.GET": [ITEM_KINDS["stylesheet"]]}, "acme")
    push()
    Then.http.status_code_is(409)
    assert [entry["key"] for entry in problem()["conflicts"]] == [
        ["row"],
        ["acme-theme", "acme"],
    ]
    assert problem()["unreadable_conflicts"] == 0


def conflicts_of(name):
    return stored_branch(name).content.get("conflicts")


@pytest.mark.django_db
@test_context
def test_deleting_a_targets_row_clears_its_conflict(admin_login):
    # Ruling R21: as a direct DELETE of an override does.
    Given.http.force_authenticate(admin_login)
    main_row = make_component("row", "<b>main</b>")
    make_wip(admin_login, "team")
    on_team = {"branch": "team", "rbac_context": "alice"}
    create("ui-components", component(name="row", template="<i/>", **on_team))
    create("ui-components", component(name="title", template="<h1/>", **on_team))
    team = stored_branch("team")
    ResourceRegistration.update(
        {
            **team.content,
            "conflicts": [
                {"kind": "component", "key": ["row"], "base": "x", "current": "y"},
                {"kind": "component", "key": ["title"], "base": None, "current": "z"},
            ],
        },
        "ui-branches",
        team.pk,
    )
    make_wip(admin_login, "mine", parent="team")
    create(
        "ui-components",
        {"name": "row", "deleted": True, "branch": "mine", "rbac_context": "alice"},
    )
    push(target="team", source="mine", context="alice")
    Then.http.status_code_is(201)
    assert [entry["key"] for entry in conflicts_of("team")] == [["title"]]
    (marker,) = [
        r for r in own_rows(UI_COMPONENT_TYPE_URI, "team") if r["name"] == "row"
    ]
    assert marker["deleted"] is True
    assert marker["base"] == main_row.etag
    # A restore that removes "title" clears its conflict too.
    team_versions = versions_of("team")
    number = next(
        n
        for n in team_versions
        if not any(
            i["name"] == "title" for i in snapshot("team", n)["items"]["component"]
        )
    )
    push(source=None, restore=number, target="team", context="alice")
    Then.http.status_code_is(201)
    assert conflicts_of("team") == []


@pytest.mark.django_db
@test_context
def test_push_needs_put_on_source(admin_login):
    wip_overrides_row(admin_login)
    before = stored_state()
    user = assign_ui_role((UI_PUSH_TYPE_URI,), "")
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    grant(user, {"http.PUT": [UI_COMPONENT_TYPE_URI]}, "")
    Given.http.force_authenticate(user)
    push()
    Then.http.status_code_is(403)
    assert stored_state() == before
    # Nor without GET on the source: a source the pusher may not see is
    # answered as an unknown branch, a 400 (ruling R36), before applying.
    user = assign_ui_role((UI_PUSH_TYPE_URI,), "")
    grant(user, {"http.PUT": [UI_BRANCH_TYPE_URI, UI_COMPONENT_TYPE_URI]}, "alice")
    grant(user, {"http.PUT": [UI_COMPONENT_TYPE_URI]}, "")
    Given.http.force_authenticate(user)
    push()
    Then.http.status_code_is(400)
    assert Http.response.json()["type"].endswith("/ui-branch-invalid")
    assert Http.response.json()["detail"] == "There is no UI branch named 'wip'."
    assert stored_state() == before


@pytest.mark.django_db
@test_context
def test_deletion_marker_pushes_as_delete(admin_login):
    Given.http.force_authenticate(admin_login)
    main_row = make_component("row", "<b>main</b>")
    make_wip(admin_login)
    create("ui-components", {"name": "row", "deleted": True, **WIP})
    # Removing main's own row needs DELETE there.
    Given.http.force_authenticate(component_writer())
    push()
    Then.http.status_code_is(403)
    assert Resource.objects.filter(pk=main_row.pk).exists()
    Given.http.force_authenticate(
        pusher(**{"": {"http.DELETE": [UI_COMPONENT_TYPE_URI]}})
    )
    push()
    Then.http.status_code_is(201)
    assert not Resource.objects.filter(pk=main_row.pk).exists()
    # main has no parent: nothing left to hide, so no marker.
    assert own_rows(UI_COMPONENT_TYPE_URI, "main") == []
    assert own_rows(UI_COMPONENT_TYPE_URI, "wip") == []


@pytest.mark.django_db
@test_context
def test_deletion_marker_hides_the_parents_item_on_the_target(admin_login):
    Given.http.force_authenticate(admin_login)
    main_row = make_component("row", "<b>main</b>")
    make_wip(admin_login, "team")
    make_wip(admin_login, "mine", parent="team")
    create(
        "ui-components",
        {"name": "row", "deleted": True, "branch": "mine", "rbac_context": "alice"},
    )
    push(target="team", source="mine", context="alice")
    Then.http.status_code_is(201)
    (marker,) = own_rows(UI_COMPONENT_TYPE_URI, "team")
    assert marker["deleted"] is True
    assert marker["base"] == main_row.etag
    assert Resource.objects.filter(pk=main_row.pk).exists()


@pytest.mark.django_db
@test_context
def test_push_into_descendant_or_self_is_refused(admin_login):
    wip_overrides_row(admin_login)
    make_wip(admin_login, "sub", parent="wip")
    before = stored_state()
    push(target="sub", source="wip", context="alice")
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-branch-invalid")
    assert "ancestors" in problem()["detail"]
    push(target="wip", source="wip", context="alice")
    Then.http.status_code_is(400)
    # The push is in the target's context.
    push(target="main", source="wip", context="alice")
    Then.http.status_code_is(400)
    # A source or a restore, not both, nor neither.
    push(restore=1)
    Then.http.status_code_is(400)
    push(source=None)
    Then.http.status_code_is(400)
    push(source="nope")
    Then.http.status_code_is(400)
    assert stored_state() == before


@pytest.mark.django_db
@test_context
def test_proposed_then_applied(admin_login):
    main_row, override = wip_overrides_row(admin_login)
    before = stored_state()
    push(state="proposed", result={"version": 99})
    Then.http.status_code_is(201)
    assert "result" not in Http.response.json()
    proposed = created()
    assert [row for row in stored_state() if row[0] != UI_PUSH_TYPE_URI] == [
        row for row in before if row[0] != UI_PUSH_TYPE_URI
    ]
    put_push(proposed, state="applied")
    Then.http.status_code_is(200)
    version = versions_of("main")[-1]
    assert Http.response.json()["result"] == {"version": version}
    main_row.refresh_from_db()
    assert main_row.content["template"] == "<i>wip</i>"
    assert not Resource.objects.filter(pk=override.pk).exists()
    assert versions_of("main")[-1] == version


@pytest.mark.django_db
@test_context
def test_an_applied_push_is_immutable(admin_login):
    # Ruling R18: an applied push is the record of what it changed. Moving it
    # back to proposed (then applied again) would push again past an
    # approval step: pushing again takes a new push.
    wip_overrides_row(admin_login)
    push()
    Then.http.status_code_is(201)
    applied = created()
    before = stored_state()
    for fields in ({"state": "proposed"}, {"label": "renamed"}, {}):
        put_push(applied, **fields)
        Then.http.status_code_is(400)
        assert problem()["type"].endswith("/ui-branch-invalid")
    When.http.delete(
        reverse("ui-pushes-detail", args=[applied.pk]),
        HTTP_IF_MATCH=quoted(applied.etag),
    )
    Then.http.status_code_is(409)
    assert problem()["type"].endswith("/ui-push-applied")
    assert stored_state() == before
    # A push that is not applied may be deleted.
    push(state="proposed")
    Then.http.status_code_is(201)
    proposed = created()
    When.http.delete(
        reverse("ui-pushes-detail", args=[proposed.pk]),
        HTTP_IF_MATCH=quoted(proposed.etag),
    )
    Then.http.status_code_is(204)


@pytest.mark.django_db
@test_context
def test_a_branch_without_parent_cannot_be_pushed(admin_login):
    # Ruling R19: pushing a root (main) would delete its items: the live
    # site's.
    wip_overrides_row(admin_login)
    make_wip(admin_login, "solo", parent=None, context="alice")
    before = stored_state()
    push(target="solo", source="main", context="alice")
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-branch-invalid")
    assert "no parent" in problem()["detail"]
    push(target="wip", source="solo", context="alice")
    Then.http.status_code_is(400)
    assert stored_state() == before


@pytest.mark.django_db
@test_context
def test_refused_apply_on_update_leaves_the_push_proposed(admin_login):
    main_row, _override = wip_overrides_row(admin_login)
    push(state="proposed")
    Then.http.status_code_is(201)
    proposed = created()
    ResourceRegistration.update(
        {**main_row.content, "template": "<b>changed</b>"}, "ui-components", main_row.pk
    )
    before = stored_state()
    put_push(proposed, state="applied")
    Then.http.status_code_is(409)
    assert stored_state() == before
    assert Resource.objects.get(pk=proposed.pk).content["state"] == "proposed"


APPROVAL_POLICY = {
    "resource_type": UI_PUSH_TYPE_URI,
    "rbac_context": "",
    "initial_states": {"proposed": 'state == "proposed"'},
    "states": {
        "approved": 'state == "approved"',
        "applied": 'state == "applied"',
    },
    "transitions": {
        "approve": {"from": "proposed", "to": "approved"},
        "apply": {"from": "approved", "to": "applied"},
    },
}


@pytest.mark.django_db
@test_context
def test_workflow_can_require_approval(admin_login):
    main_row, _override = wip_overrides_row(admin_login)
    workflow_policy_resource_type()
    ResourceRegistration.register(APPROVAL_POLICY, "workflow-policies")
    Given.http.force_authenticate(component_writer())
    push()
    Then.http.status_code_is(400)
    push(state="proposed")
    Then.http.status_code_is(201)
    proposed = created()
    before = stored_state()
    put_push(proposed, state="applied")
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/workflow-transition-invalid")
    assert stored_state() == before
    put_push(proposed, state="approved")
    Then.http.status_code_is(200)
    main_row.refresh_from_db()
    assert main_row.content["template"] == "<b>main</b>"
    put_push(Resource.objects.get(pk=proposed.pk), state="applied")
    Then.http.status_code_is(200)
    main_row.refresh_from_db()
    assert main_row.content["template"] == "<i>wip</i>"
    assert Http.response.json()["result"] == {"version": versions_of("main")[-1]}


@pytest.mark.django_db
@test_context
def test_restore_brings_back_overrides_and_keeps_base_mode(admin_login):
    _main_row, override = wip_overrides_row(admin_login)
    number = versions_of("wip")[-1]
    kept = snapshot("wip", number)["items"]
    When.http.put(
        reverse("ui-components-detail", args=[override.pk]),
        data=component(name="row", template="<i>later</i>", **WIP),
        format="json",
        HTTP_IF_MATCH=quoted(override.etag),
    )
    Then.http.status_code_is(200)
    create("ui-components", component(name="extra", template="<u/>", **WIP))
    create("ui-stylesheets", stylesheet(name="theme", **WIP))
    wip_before = stored_branch("wip").content
    versions = versions_of("wip")
    push(source=None, restore=number, target="wip", context="alice")
    Then.http.status_code_is(201)
    assert versions_of("wip") == [*versions, versions[-1] + 1]
    assert Http.response.json()["result"] == {"version": versions[-1] + 1}
    for kind, type_uri in ITEM_KINDS.items():
        assert own_rows(type_uri, "wip") == kept[kind], kind
    wip = stored_branch("wip").content
    assert wip["base"] == "live"
    assert wip == wip_before
    # A missing version is a 404, and writes nothing.
    before = stored_state()
    push(source=None, restore=999, target="wip", context="alice")
    Then.http.status_code_is(404)
    assert stored_state() == before


@pytest.mark.django_db
@test_context
def test_restore_needs_per_item_grant(admin_login):
    _main_row, _override = wip_overrides_row(admin_login)
    number = versions_of("wip")[-1]
    create("ui-components", component(name="extra", template="<u/>", **WIP))
    before = stored_state()
    # Restoring removes wip's "extra": DELETE on components in "alice".
    user = assign_ui_role((UI_PUSH_TYPE_URI,), "alice")
    Given.http.force_authenticate(user)
    push(source=None, restore=number, target="wip", context="alice")
    Then.http.status_code_is(403)
    assert problem()["type"].endswith("/ui-push-forbidden")
    assert stored_state() == before
    grant(user, {"http.DELETE": [UI_COMPONENT_TYPE_URI]}, "alice")
    push(source=None, restore=number, target="wip", context="alice")
    Then.http.status_code_is(201)
    assert [row["name"] for row in own_rows(UI_COMPONENT_TYPE_URI, "wip")] == ["row"]


@pytest.mark.django_db
@test_context
def test_pushes_are_listed(admin_login):
    wip_overrides_row(admin_login)
    push(state="proposed")
    Then.http.status_code_is(201)
    When.http.get(reverse("ui-pushes-list"))
    Then.http.status_code_is(200)
    (listed,) = Http.response.json()["content"]
    assert listed["content"]["state"] == "proposed"
