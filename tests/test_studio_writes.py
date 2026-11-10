"""The studio's writes: saving and reverting an item on a working branch,
pulling it and pushing it, each made through the resource views with the
studio user's own rights (`studio.dispatch.call_view`), so that the studio
adds no rights. These tests serve the API at ``/api/`` and the studio at
``/studio/`` (``tests.mounted_urls``)."""

from unittest import mock

import pytest
from django.middleware.csrf import _get_new_csrf_string
from django.urls import reverse
from rest_framework.test import APIClient
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django import collection_type_uri_for
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI, UI_PUSH_TYPE_URI
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI, UI_STYLESHEET_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import assign_ui_role
from .steps import *  # noqa: F403, F401
from .test_ui_branch_push import make_wip, own_rows, quoted, stored_state
from .test_ui_branch_push import push as api_push
from .test_ui_branch_types import component, make_branch, problem
from .test_ui_http import grant, make_component

READ_ONLY = "https://api.openteams.com/http-problem/ui-studio-branch-read-only"
UNAVAILABLE = "https://api.openteams.com/http-problem/ui-branch-unavailable"
SAVED = "scrud-studio-saved"
WIP = {"branch": "wip", "rbac_context": "alice"}


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def studio(verb, body, **headers):
    """POST `body` to the studio's write endpoint `verb`."""
    When.http.post(reverse(f"scrud-studio:{verb}"), data=body, format="json", **headers)
    return Http.response


def save(name="row", template="<i>mine</i>", **headers):
    return studio(
        "save-item",
        {
            "kind": "component",
            "branch": "wip",
            "name": name,
            "content": {"engine": "django", "mode": "view", "template": template},
        },
        **headers,
    )


def stored(pk):
    return Resource.objects.get(pk=pk)


def main_and_wip(admin_login):
    """main has the component ``row``; ``wip`` (in "alice") is over main.
    Returns main's row."""
    main_row = make_component("row", "<b>main</b>")
    make_wip(admin_login)
    return main_row


@pytest.mark.django_db
@test_context
def test_save_inherited_item_creates_override_with_server_base(
    admin_login, ui_alice_login
):
    main_row = main_and_wip(admin_login)
    Given.http.force_authenticate(ui_alice_login)
    response = studio(
        "save-item",
        {
            "kind": "component",
            "branch": "wip",
            "name": "row",
            # A client's base is not the server's: it is server-managed.
            "content": {
                "engine": "django",
                "mode": "view",
                "template": "<i>mine</i>",
                "base": "forged",
            },
        },
    )
    Then.http.status_code_is(201)
    assert response["HX-Trigger"] == SAVED
    body = response.json()
    assert body["base"] == main_row.etag
    assert body["branch"] == "wip"
    assert body["rbac_context"] == "alice"
    assert body["template"] == "<i>mine</i>"
    (override,) = own_rows(UI_COMPONENT_TYPE_URI, "wip")
    assert override == body
    # The resource view's ETag and Location come through.
    row = Resource.objects.get(
        resource_type__slug="ui-components", content__branch="wip"
    )
    assert response["ETag"] == quoted(row.etag)
    assert response["Location"].endswith(reverse("ui-components-detail", args=[row.pk]))
    # main's row is untouched.
    assert stored(main_row.pk).etag == main_row.etag
    # An item main does not have is an addition: no base.
    response = save(name="fresh")
    Then.http.status_code_is(201)
    assert response.json()["base"] is None


@pytest.mark.django_db
@test_context
def test_save_own_item_needs_if_match_and_412s_on_stale(admin_login, ui_alice_login):
    main_row = main_and_wip(admin_login)
    Given.http.force_authenticate(ui_alice_login)
    first = save()
    Then.http.status_code_is(201)
    etag = first["ETag"]
    pk = Resource.objects.get(
        resource_type__slug="ui-components", content__branch="wip"
    ).pk
    # Own now: a save is a PUT, conditional as the API's.
    save(template="<i>no if-match</i>")
    Then.http.status_code_is(428)
    assert stored(pk).content["template"] == "<i>mine</i>"
    response = save(template="<i>second</i>", HTTP_IF_MATCH=etag)
    Then.http.status_code_is(200)
    assert response["HX-Trigger"] == SAVED
    assert response.json()["template"] == "<i>second</i>"
    assert response.json()["base"] == main_row.etag
    assert response["ETag"] == quoted(stored(pk).etag) != etag
    # The same ETag again is stale: the resource view's 412, unchanged.
    after = stored(pk).etag
    stale = save(template="<i>third</i>", HTTP_IF_MATCH=etag)
    Then.http.status_code_is(412)
    assert "HX-Trigger" not in stale
    assert stored(pk).etag == after
    studio_body = (stale["Content-Type"], stale.json())
    When.http.put(
        reverse("ui-components-detail", args=[pk]),
        data=component(name="row", template="<i>third</i>", **WIP),
        format="json",
        HTTP_IF_MATCH=etag,
    )
    Then.http.status_code_is(412)
    assert studio_body == (Http.response["Content-Type"], Http.response.json())
    assert own_rows(UI_COMPONENT_TYPE_URI, "wip")[0]["template"] == "<i>second</i>"


@pytest.mark.django_db
@test_context
def test_published_branch_is_read_only(admin_login):
    main_and_wip(admin_login)
    make_branch("shop", "", parent="main", prefix="/shop/")
    before = stored_state()
    bodies = {
        "save-item": {
            "kind": "component",
            "name": "row",
            "content": {"engine": "django", "mode": "view", "template": "<u/>"},
        },
        "revert-item": {"kind": "component", "name": "row"},
        "pull": {},
        "push": {"target": "main"},
    }
    # Even for a superuser, and before anything is dispatched.
    Given.http.force_authenticate(admin_login)
    with mock.patch(
        "scrud_django.ui.studio.writes.call_view",
        side_effect=AssertionError("dispatched"),
    ):
        for verb, body in bodies.items():
            for branch in ("main", "shop"):
                response = studio(verb, {**body, "branch": branch})
                Then.http.status_code_is(400)
                assert problem()["type"] == READ_ONLY
                assert branch in problem()["detail"]
                assert "HX-Trigger" not in response
    assert stored_state() == before


@pytest.mark.django_db
@test_context
def test_studio_adds_no_rights(admin_login):
    main_row = main_and_wip(admin_login)
    # May GET (and PUT) the branch and GET components in "alice", but may
    # not POST nor PUT components there.
    user = assign_ui_role((UI_BRANCH_TYPE_URI,), "alice")
    grant(
        user,
        {
            "http.GET": [
                UI_COMPONENT_TYPE_URI,
                collection_type_uri_for(UI_COMPONENT_TYPE_URI),
            ]
        },
        "alice",
    )
    before = stored_state()
    Given.http.force_authenticate(user)
    When.http.post(
        reverse("ui-components-list"),
        data=component(name="row", template="<i>mine</i>", **WIP),
        format="json",
    )
    Then.http.status_code_is(403)
    api = (Http.response["Content-Type"], Http.response.json())
    response = save()
    Then.http.status_code_is(403)
    assert (response["Content-Type"], response.json()) == api
    assert "HX-Trigger" not in response
    assert stored_state() == before
    # An own row: the PUT is refused as the API refuses it.
    Given.http.force_authenticate(admin_login)
    When.http.post(
        reverse("ui-components-list"),
        data=component(name="row", template="<i>admin's</i>", **WIP),
        format="json",
    )
    Then.http.status_code_is(201)
    etag = Http.response["ETag"]
    pk = int(Http.response["Location"].rstrip("/").rsplit("/", 1)[1])
    before = stored_state()
    Given.http.force_authenticate(user)
    When.http.put(
        reverse("ui-components-detail", args=[pk]),
        data=component(name="row", template="<i>mine</i>", **WIP),
        format="json",
        HTTP_IF_MATCH=etag,
    )
    Then.http.status_code_is(403)
    api = (Http.response["Content-Type"], Http.response.json())
    response = save(HTTP_IF_MATCH=etag)
    Then.http.status_code_is(403)
    assert (response["Content-Type"], response.json()) == api
    assert stored_state() == before
    # A push needs the API's grants too: none on pushes in "" here.
    api_push(target="main", source="wip")
    Then.http.status_code_is(403)
    api = (Http.response["Content-Type"], Http.response.json())
    response = studio("push", {"branch": "wip"})
    Then.http.status_code_is(403)
    assert (response["Content-Type"], response.json()) == api
    assert stored_state() == before
    assert stored(main_row.pk).etag == main_row.etag
    # A branch the user may not GET is refused alike for every write, as
    # one that does not exist, and nothing is dispatched.
    Given.http.force_authenticate(assign_ui_role((UI_COMPONENT_TYPE_URI,), "alice"))
    with mock.patch(
        "scrud_django.ui.studio.writes.call_view",
        side_effect=AssertionError("dispatched"),
    ):
        for verb in ("save-item", "revert-item", "pull", "push"):
            for branch in ("wip", "no-such-branch"):
                studio(
                    verb,
                    {
                        "kind": "component",
                        "branch": branch,
                        "name": "row",
                        "content": {"template": "<u/>"},
                    },
                )
                Then.http.status_code_is(400)
                assert problem()["type"] == UNAVAILABLE
    assert stored_state() == before


@pytest.mark.django_db
@test_context
def test_revert_deletes_own_row_and_resolves_conflict(admin_login, ui_alice_login):
    main_row = main_and_wip(admin_login)
    Given.http.force_authenticate(ui_alice_login)
    first = save()["ETag"]
    Then.http.status_code_is(201)
    # A newer edit (another author's, on the shared branch).
    current = save(template="<i>newer</i>", HTTP_IF_MATCH=first)["ETag"]
    Then.http.status_code_is(200)
    ResourceRegistration.update(
        {**main_row.content, "template": "<b>changed</b>"}, "ui-components", main_row.pk
    )
    studio("pull", {"branch": "wip"})
    Then.http.status_code_is(200)
    assert [c["key"] for c in Http.response.json()["conflicts"]] == [["row"]]
    revert = {"kind": "component", "branch": "wip", "name": "row"}
    before = stored_state()
    # A revert is conditional, as the API's DELETE (ruling S10): without
    # If-Match the view's 428, and with an ETag older than the newer edit
    # its 412, each passed through; nothing is deleted.
    for headers, status in (({}, 428), ({"HTTP_IF_MATCH": first}, 412)):
        response = studio("revert-item", revert, **headers)
        Then.http.status_code_is(status)
        assert response["Content-Type"] == "application/problem+json"
        assert response.json()["status"] == status
        assert "HX-Trigger" not in response
        assert stored_state() == before
    assert own_rows(UI_COMPONENT_TYPE_URI, "wip")[0]["template"] == "<i>newer</i>"
    # Take theirs, having seen the newer edit: drop the override.
    response = studio("revert-item", revert, HTTP_IF_MATCH=current)
    Then.http.status_code_is(204)
    assert response["HX-Trigger"] == SAVED
    assert own_rows(UI_COMPONENT_TYPE_URI, "wip") == []
    wip = Resource.objects.get(
        resource_type__type_uri=UI_BRANCH_TYPE_URI, content__name="wip"
    )
    assert wip.content["conflicts"] == []
    assert stored(main_row.pk).content["template"] == "<b>changed</b>"
    # Nothing left to revert.
    studio("revert-item", revert, HTTP_IF_MATCH=current)
    Then.http.status_code_is(404)
    # A stylesheet is identified by its name and the context it applies to:
    # only the override that applies to "acme" is reverted.
    etags = {}
    for target in ("", "acme"):
        etags[target] = studio(
            "save-item",
            {
                "kind": "stylesheet",
                "branch": "wip",
                "name": "studio",
                "applies_to": target,
                "content": {"rules": []},
            },
        )["ETag"]
        Then.http.status_code_is(201)
        assert Http.response.json()["applies_to"] == target
        assert Http.response.json()["rbac_context"] == "alice"
    studio(
        "revert-item",
        {"kind": "stylesheet", "branch": "wip", "name": "studio", "applies_to": "acme"},
        HTTP_IF_MATCH=etags["acme"],
    )
    Then.http.status_code_is(204)
    assert [row["applies_to"] for row in own_rows(UI_STYLESHEET_TYPE_URI, "wip")] == [
        ""
    ]


@pytest.mark.django_db
@test_context
def test_pull_and_push_through_existing_resources(admin_login, ui_alice_login):
    main_row = main_and_wip(admin_login)
    # alice may also push into main: pushes in "", and GET and PUT on the
    # components there, where main's row is (as the API needs; no GET on
    # branches in "").
    grant(
        ui_alice_login,
        {
            "http.GET": [
                UI_PUSH_TYPE_URI,
                collection_type_uri_for(UI_PUSH_TYPE_URI),
                UI_COMPONENT_TYPE_URI,
            ],
            "http.POST": [UI_PUSH_TYPE_URI, collection_type_uri_for(UI_PUSH_TYPE_URI)],
            "http.PUT": [UI_COMPONENT_TYPE_URI],
        },
        "",
    )
    Given.http.force_authenticate(ui_alice_login)
    save()
    Then.http.status_code_is(201)
    ResourceRegistration.update(
        {**main_row.content, "template": "<b>changed</b>"}, "ui-components", main_row.pk
    )
    main_row.refresh_from_db()
    # The push conflicts: the API's 409, with its body, and nothing written.
    before = stored_state()
    response = studio("push", {"branch": "wip"})
    Then.http.status_code_is(409)
    assert "HX-Trigger" not in response
    studio_answer = (response["Content-Type"], response.json())
    assert studio_answer[1]["type"].endswith("/ui-push-conflict")
    assert [c["key"] for c in studio_answer[1]["conflicts"]] == [["row"]]
    assert stored_state() == before
    api_push(target="main", source="wip")
    Then.http.status_code_is(409)
    assert studio_answer == (Http.response["Content-Type"], Http.response.json())
    assert stored_state() == before
    # Pull: the branch's PUT with pull, its answer passed through.
    response = studio("pull", {"branch": "wip"})
    Then.http.status_code_is(200)
    assert response["HX-Trigger"] == SAVED
    body = response.json()
    assert "pull" not in body
    assert body["branching_point"] == {"main": 2}
    assert body["conflicts"] == [
        {
            "kind": "component",
            "key": ["row"],
            "base": own_rows(UI_COMPONENT_TYPE_URI, "wip")[0]["base"],
            "current": main_row.etag,
        }
    ]
    # Keep mine (rebase), then push to the parent, the default target.
    override = Resource.objects.get(
        resource_type__slug="ui-components", content__branch="wip"
    )
    studio(
        "save-item",
        {
            "kind": "component",
            "branch": "wip",
            "name": "row",
            "content": {
                "engine": "django",
                "mode": "view",
                "template": "<i>mine</i>",
                "rebase": True,
            },
        },
        HTTP_IF_MATCH=quoted(override.etag),
    )
    Then.http.status_code_is(200)
    # A target that is neither the parent nor readable is refused as one
    # that does not exist.
    make_branch("hidden", "bob", parent="main")
    before = stored_state()
    studio("push", {"branch": "wip", "target": "hidden"})
    Then.http.status_code_is(400)
    assert problem()["type"] == UNAVAILABLE
    assert stored_state() == before
    response = studio("push", {"branch": "wip"})
    Then.http.status_code_is(201)
    assert response["HX-Trigger"] == SAVED
    push = response.json()
    assert (push["source"], push["target"], push["state"]) == ("wip", "main", "applied")
    assert push["rbac_context"] == ""
    assert push["result"]["version"] >= 1
    assert stored(main_row.pk).content["template"] == "<i>mine</i>"
    assert own_rows(UI_COMPONENT_TYPE_URI, "wip") == []


@pytest.mark.django_db
@test_context
def test_internal_dispatch_keeps_csrf_for_the_studio_request(
    admin_login, ui_alice_login
):
    main_and_wip(admin_login)
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(ui_alice_login)
    url = reverse("scrud-studio:save-item")
    body = {
        "kind": "component",
        "branch": "wip",
        "name": "row",
        "content": {"engine": "django", "mode": "view", "template": "<i/>"},
    }
    response = client.post(url, data=body, format="json")
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]
    assert own_rows(UI_COMPONENT_TYPE_URI, "wip") == []
    # With the token, the studio request passes the check, and its in-process
    # write (which carries none) is made.
    token = _get_new_csrf_string()
    client.cookies["csrftoken"] = token
    response = client.post(url, data=body, format="json", HTTP_X_CSRFTOKEN=token)
    assert response.status_code == 201
    assert [row["template"] for row in own_rows(UI_COMPONENT_TYPE_URI, "wip")] == [
        "<i/>"
    ]


@pytest.mark.django_db
@test_context
def test_bad_bodies_are_refused(admin_login, ui_alice_login):
    main_and_wip(admin_login)
    before = stored_state()
    Given.http.force_authenticate(ui_alice_login)
    for verb, body in (
        ("save-item", {"kind": "page", "branch": "wip", "name": "x", "content": {}}),
        ("save-item", {"kind": "component", "branch": "wip", "content": {}}),
        ("save-item", {"kind": "component", "branch": "wip", "name": "x"}),
        ("revert-item", {"kind": "component", "branch": "wip", "name": 3}),
        ("pull", {}),
        ("push", {"branch": "wip", "target": 3}),
    ):
        studio(verb, body)
        Then.http.status_code_is(400)
    When.http.post(
        reverse("scrud-studio:pull"), data="[]", content_type="application/json"
    )
    Then.http.status_code_is(400)
    assert stored_state() == before
    # The write endpoints answer POST only, and need a login.
    When.http.get(reverse("scrud-studio:pull"))
    Then.http.status_code_is(405)
    Given.http.force_authenticate(None)
    studio("pull", {"branch": "wip"})
    Then.http.status_code_is(401)
