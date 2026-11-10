"""What a push will change (spec E3): ``ui-push-previews/<source>/``, a
computed resource listing the writes a push of branch `source` into
``?target=`` (default: its parent) would make, each with the target's item
before and after, and the conflicts the push would hit. It needs GET on both
branches, never writes, and shows nothing the user could not GET as JSON."""

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.ui.branches import ITEM_KINDS, UI_BRANCH_TYPE_URI, UI_CSS_TYPE_URI
from scrud_django.ui.layers import layer_item
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import assign_ui_role
from .steps import *  # noqa: F403, F401
from .test_ui_branch_push import (
    WIP,
    create,
    make_wip,
    own_rows,
    push,
    pusher,
    stored_state,
    wip_overrides_row,
)
from .test_ui_branch_types import component, make_branch, stylesheet
from .test_ui_http import (
    HTML,
    assert_html,
    grant,
    html,
    make_component,
    make_stylesheet,
)
from .test_ui_layer_conflicts import css_body, wip_body
from .test_ui_layers import MORE_APP, installed

JSON = "application/json"
OT = "http://api.openteams.com/json-ld/"
SLUG = "ui-push-previews"


def preview_url(source, **params):
    found = reverse(f"{SLUG}-detail", args=[source])
    if params:
        found += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return found


def preview(source="wip", status=200, **params):
    When.http.get(preview_url(source, **params), HTTP_ACCEPT=JSON)
    Then.http.status_code_is(status)
    return Http.response.json()


def target_row(type_uri, name, branch="main"):
    """The content of `branch`'s own stored item of `type_uri` called `name`,
    or ``None``."""
    found = [
        content for content in own_rows(type_uri, branch) if content["name"] == name
    ]
    assert len(found) <= 1
    return found[0] if found else None


def change(kind, key, action, before, after):
    return {
        "kind": kind,
        "key": list(key),
        "action": action,
        "before": before,
        "after": after,
    }


@pytest.mark.django_db
@test_context
def test_push_preview_matches_what_apply_push_writes(admin_login):
    main_row, override = wip_overrides_row(admin_login)
    gone = make_component("gone", "<s>gone</s>")
    create("ui-components", component(name="fresh", template="<em/>", **WIP))
    create("ui-components", {"name": "gone", "deleted": True, **WIP})
    main_row.refresh_from_db()
    gone.refresh_from_db()
    body = preview()
    assert body["source"] == "wip" and body["target"] == "main"
    assert body["conflicts"] == [] and body["unreadable_conflicts"] == 0
    assert body["unreadable_changes"] == 0
    # An override of main's row is an update of it; a new item a create;
    # a deletion marker deletes main's row (main has no parent: no marker).
    # main's row keeps its base (none: main has no parent).
    after_row = {
        **override.content,
        "branch": "main",
        "rbac_context": "",
        "base": main_row.content.get("base"),
    }
    assert body["changes"][0] == change(
        "component", ["row"], "update", main_row.content, after_row
    )
    assert [(c["key"], c["action"]) for c in body["changes"]] == [
        (["row"], "update"),
        (["fresh"], "create"),
        (["gone"], "delete"),
    ]
    assert body["changes"][2] == change(
        "component", ["gone"], "delete", gone.content, None
    )
    push()
    Then.http.status_code_is(201)
    # The target's rows after the push are the previewed ``after`` values.
    for entry in body["changes"]:
        stored = target_row(UI_COMPONENT_TYPE_URI, entry["key"][0])
        assert stored == entry["after"], entry["key"]


@pytest.mark.django_db
@test_context
def test_push_preview_shows_marker_hiding_the_parents_item(admin_login):
    Given.http.force_authenticate(admin_login)
    main_row = make_component("row", "<b>main</b>")
    make_wip(admin_login, "team")
    make_wip(admin_login, "mine", parent="team")
    team_row = create(
        "ui-components",
        component(
            name="row", template="<i>team</i>", branch="team", rbac_context="alice"
        ),
    )
    main_row.refresh_from_db()
    create(
        "ui-components",
        {
            "name": "row",
            "deleted": True,
            "branch": "mine",
            "rbac_context": "alice",
            "base": team_row.etag,
        },
    )
    body = preview("mine")
    assert body["target"] == "team" and body["conflicts"] == []
    # team's row is deleted, and a marker hides main's item beneath it.
    delete, marker = body["changes"]
    assert delete == change("component", ["row"], "delete", team_row.content, None)
    assert marker["action"] == "create"
    assert marker["before"] == main_row.content
    assert marker["after"]["deleted"] is True
    assert marker["after"]["base"] == main_row.etag
    push(source="mine", target="team", context="alice")
    Then.http.status_code_is(201)
    assert own_rows(UI_COMPONENT_TYPE_URI, "team") == [marker["after"]]


@pytest.mark.django_db
@test_context
def test_push_preview_lists_conflicts_readable_only(admin_login):
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
    main_row.refresh_from_db()
    # May read the source's items, and components alone in the target.
    user = pusher(
        **{
            "": {"http.GET": [UI_COMPONENT_TYPE_URI]},
            "alice": {"http.GET": [UI_COMPONENT_TYPE_URI, ITEM_KINDS["stylesheet"]]},
        }
    )
    Given.http.force_authenticate(user)
    body = preview()
    assert body["conflicts"] == [
        {
            "kind": "component",
            "key": ["row"],
            "base": override.content["base"],
            "current": main_row.etag,
        }
    ]
    assert body["unreadable_conflicts"] == 1
    # The write over the stylesheet the user may not read in acme is only
    # counted.
    [row_change] = body["changes"]
    assert row_change["before"] == main_row.content
    assert row_change["after"]["template"] == "<i>wip</i>"
    assert body["unreadable_changes"] == 1
    assert "acme" not in str(body["changes"])
    # A reader of acme's stylesheets sees both conflicts and the change.
    grant(user, {"http.GET": [ITEM_KINDS["stylesheet"]]}, "acme")
    body = preview()
    assert [entry["key"] for entry in body["conflicts"]] == [
        ["row"],
        ["acme-theme", "acme"],
    ]
    assert body["unreadable_conflicts"] == 0
    assert body["unreadable_changes"] == 0
    theme.refresh_from_db()
    assert body["changes"][1]["key"] == ["acme-theme", "acme"]
    assert body["changes"][1]["before"] == theme.content
    assert body["changes"][1]["after"]["applies_to"] == "acme"
    # The push itself is refused with the same conflicts.
    push()
    Then.http.status_code_is(409)
    assert Http.response.json()["conflicts"] == body["conflicts"]


@pytest.mark.django_db
@test_context
def test_push_preview_hides_items_the_user_may_not_read_in_the_source(
    admin_login,
):
    # A write whose source row the user may not read is only counted,
    # though the target's row is readable: its after is the source's
    # content.
    Given.http.force_authenticate(admin_login)
    reverse("ui-components-list")
    make_component("row", "<b>main</b>")
    make_wip(admin_login)
    create("ui-components", component(name="row", template="<i>wip</i>", **WIP))
    user = assign_ui_role((), "")
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    grant(user, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "")
    Given.http.force_authenticate(user)
    body = preview()
    assert body["changes"] == [] and body["unreadable_changes"] == 1
    grant(user, {"http.GET": [UI_COMPONENT_TYPE_URI]}, "alice")
    body = preview()
    [shown] = body["changes"]
    assert shown["after"]["template"] == "<i>wip</i>"
    assert body["unreadable_changes"] == 0


@pytest.mark.django_db
@test_context
def test_push_preview_needs_get_on_both(admin_login):
    Given.http.force_authenticate(admin_login)
    make_wip(admin_login)
    make_wip(admin_login, "other", context="bob")
    make_wip(admin_login, "sub", parent="wip")
    make_wip(admin_login, "osub", parent="other", context="bob")
    user = assign_ui_role((), "")
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI]}, "alice")
    Given.http.force_authenticate(user)
    # main, which everyone may select, needs no grant.
    preview()
    preview("wip", target="main")
    # A branch the user may not GET is answered as an unknown one, the
    # same 404, before a push it would refuse (other into its child osub)
    # could be described by a 400.
    unknown = preview("nope", status=404)
    for source, params in (
        ("wip", {"target": "other"}),
        ("other", {}),
        ("other", {"target": "wip"}),
        ("other", {"target": "osub"}),
        ("osub", {}),
        ("wip", {"target": "nope"}),
        ("wip@pinned", {}),
    ):
        assert preview(source, status=404, **params) == unknown, (source, params)
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI]}, "bob")
    preview("wip", target="other")
    preview("other")
    preview("other", status=400, target="osub")
    # A push the server would refuse is refused alike (400).
    for source, params in (
        ("wip", {"target": "sub"}),
        ("wip", {"target": "wip"}),
        ("main", {}),
        ("main", {"target": "wip"}),
    ):
        body = preview(source, status=400, **params)
        assert body["type"].endswith("/ui-branch-invalid")
    Given.http.force_authenticate(None)
    preview(status=401)


@pytest.mark.django_db
@test_context
def test_push_preview_writes_nothing(admin_login):
    main_row, _override = wip_overrides_row(admin_login)
    create("ui-components", {"name": "gone", "deleted": True, **WIP})
    ResourceRegistration.update(
        {**main_row.content, "template": "<b>changed</b>"}, "ui-components", main_row.pk
    )
    before = stored_state()
    body = preview()
    assert body["conflicts"] and body["changes"]
    assert stored_state() == before
    for method in ("post", "put", "delete"):
        getattr(When.http, method)(preview_url("wip"), data={}, format="json")
        Then.http.status_code_is(405)
    assert stored_state() == before


@pytest.mark.django_db
@test_context
def test_push_preview_default_target_is_parent(admin_login):
    Given.http.force_authenticate(admin_login)
    make_component("row", "<b>main</b>")
    make_wip(admin_login, "team")
    make_wip(admin_login, "mine", parent="team")
    create(
        "ui-components",
        component(
            name="row", template="<i>mine</i>", branch="mine", rbac_context="alice"
        ),
    )
    body = preview("mine")
    assert body["target"] == "team"
    assert body == preview("mine", target="team")
    into_main = preview("mine", target="main")
    assert into_main["target"] == "main" and into_main != body
    # Into team, a new row; into main, an update of main's.
    assert body["changes"][0]["action"] == "create"
    assert into_main["changes"][0]["action"] == "update"


@pytest.mark.django_db
@test_context
def test_push_of_layer_override_previewed(admin_login):
    Given.http.force_authenticate(admin_login)
    make_wip(admin_login, context="project-a")
    with installed(MORE_APP):
        layer_etag, layer_component = layer_item("component", ("layermore:list",))
        css_etag, layer_css = layer_item("css", ("layermore:theme",))
        # main overrides the CSS item; wip overrides the component and
        # deletes the CSS item.
        create("ui-css", css_body())
        main_css = Resource.objects.get(
            resource_type__type_uri=UI_CSS_TYPE_URI, content__name="layermore:theme"
        )
        create("ui-components", wip_body())
        create(
            "ui-css",
            {
                "name": "layermore:theme",
                "deleted": True,
                "branch": "wip",
                "rbac_context": "project-a",
            },
        )
        body = preview()
        assert body["conflicts"] == []
        component_change, css_delete, css_marker = body["changes"]
        # The override of the layer's component: before is the packaged
        # item, after the override, based on it.
        assert component_change["action"] == "create"
        assert component_change["before"] == layer_component
        assert component_change["after"]["base"] == layer_etag
        assert component_change["after"]["branch"] == "main"
        # main's override is deleted, and a marker hides the layer's item.
        assert css_delete == change(
            "css", ["layermore:theme"], "delete", main_css.content, None
        )
        assert css_marker["action"] == "create"
        assert css_marker["before"] == layer_css
        assert css_marker["after"]["deleted"] is True
        assert css_marker["after"]["base"] == css_etag
        push(source="wip", target="main", context="")
        Then.http.status_code_is(201)
        assert own_rows(UI_COMPONENT_TYPE_URI, "main") == [component_change["after"]]
        assert own_rows(UI_CSS_TYPE_URI, "main") == [css_marker["after"]]


@pytest.mark.django_db
@test_context
def test_push_preview_renders_through_the_cascade(admin_login):
    # The studio's review panel embeds it: a stylesheet rule on its type
    # renders it like any resource.
    wip_overrides_row(admin_login)
    make_component(
        "push-preview",
        "<section>{{ value.source }} into {{ value.target }}: "
        "{{ value.changes|length }}</section>",
    )
    reverse("ui-stylesheets-list")
    ResourceRegistration.register(
        {
            "name": "studio-data",
            "prefixes": {"ot": OT},
            "rules": [{"select": "ot:UIPushPreview", "component": "push-preview"}],
            "rbac_context": "",
        },
        "ui-stylesheets",
    )
    When.http.get(preview_url("wip"), HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    assert_html()
    assert "<section>wip into main: 1</section>" in html()
    assert 'rel="describedby"' in Http.response["Link"]


@pytest.mark.django_db
@test_context
def test_push_preview_shows_a_packaged_item_as_before_to_any_reader(admin_login):
    # A packaged layer item is public: a user who may not read context ""
    # (the layer item's) sees it as the before of an override.
    Given.http.force_authenticate(admin_login)
    make_wip(admin_login, "team", context="project-a")
    make_wip(admin_login, "wip", parent="team", context="project-a")
    user = assign_ui_role((), "project-a")
    grant(user, {"http.GET": [UI_BRANCH_TYPE_URI, UI_COMPONENT_TYPE_URI]}, "project-a")
    with installed(MORE_APP):
        layer_etag, layer_component = layer_item("component", ("layermore:list",))
        assert layer_component["rbac_context"] == ""
        create("ui-components", wip_body())
        Then.http.status_code_is(201)
        Given.http.force_authenticate(user)
        body = preview()
        assert body["target"] == "team" and body["unreadable_changes"] == 0
        [change_] = body["changes"]
        assert change_["action"] == "create"
        assert change_["before"] == layer_component
        assert change_["after"]["base"] == layer_etag
        assert change_["after"]["rbac_context"] == "project-a"


@pytest.mark.django_db
@test_context
def test_push_preview_into_a_branch_pinned_to_a_missing_version(admin_login):
    Given.http.force_authenticate(admin_login)
    make_branch("gap", "alice", branching_point={"main": 999}, base="pinned")
    make_wip(admin_login, "child", parent="gap")
    body = preview("child", status=400)
    assert body["type"].endswith("/ui-branch-invalid")
    assert "missing" in body["detail"]
