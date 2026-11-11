"""JavaScript items (``ui-js``): scripts' text, per branch, served at
versioned URLs (``ui-js-files/<name>/<etag>.js``) under the API's URLs and
under a published branch's prefix, and linked by the built-in page after
``scrud-ui.js``. These tests serve the API at ``/api/``
(``tests.mounted_urls``)."""

import re
from html import unescape

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django import collection_type_uri_for
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.ui.branches import ITEM_KINDS, UI_BRANCH_TYPE_URI, UI_JS_TYPE_URI
from scrud_django.ui.branches.versions import snapshot
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import assign_ui_role
from .steps import *  # noqa: F403, F401
from .test_ui_branch_prefixes import (
    ADMIN,
    api_url,
    get_html,
    mounted,
    setup_admin_and_customer,
)
from .test_ui_branch_push import (
    WIP,
    conflicts_of,
    create,
    make_wip,
    own_rows,
    push,
    pusher,
    stored_state,
)
from .test_ui_branch_render import BRANCH_HEADER, may_select_branches
from .test_ui_branch_types import make_branch, problem
from .test_ui_branch_versions import versions_of
from .test_ui_http import grant

PRIVATE = "private, max-age=31536000, immutable"


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def make_js(name, js="console.log(1)", branch=None, context="", **fields):
    """A JS item stored as is (not through the API)."""
    reverse("ui-js-list")  # the URLconf registers the types
    body = {"name": name, "js": js, "rbac_context": context, **fields}
    if branch is not None:
        body["branch"] = branch
    return ResourceRegistration.register(body, "ui-js")


def script_srcs(page):
    return [
        unescape(h) for h in re.findall(r'<script src="([^"]*)" defer></script>', page)
    ]


def file_url(item, base="/api/"):
    return f"{base}ui-js-files/{item.content['name']}/{item.etag}.js"


def get_js(url, status=200, **headers):
    When.http.get(url, **headers)
    Then.http.status_code_is(status)
    return Http.response


def js_body(**overrides):
    return {"name": "app", "js": "let a = 1", "rbac_context": "", **overrides}


@pytest.mark.django_db
@test_context
def test_js_item_crud_with_branch_fields(admin_login):
    Given.http.force_authenticate(admin_login)
    created = create("ui-js", js_body())
    assert created.content["branch"] == "main" and created.content["base"] is None
    url = reverse("ui-js-detail", args=[created.pk])
    When.http.put(
        url,
        data={**created.content, "js": "let a = 2", "order": 3},
        format="json",
        HTTP_IF_MATCH=f'"{created.etag}"',
    )
    Then.http.status_code_is(200)
    When.http.get(url, HTTP_ACCEPT="application/json")
    assert Http.response.json()["js"] == "let a = 2"
    assert Http.response.json()["order"] == 3
    # Names are unique on a branch, not across branches.
    When.http.post(reverse("ui-js-list"), data=js_body(), format="json")
    Then.http.status_code_is(400)
    assert problem()["type"].endswith("/ui-js-name-taken")
    make_branch("wip", "alice")
    create("ui-js", js_body(branch="wip", rbac_context="alice"))
    # A deletion marker needs only a name; the text is required otherwise.
    create(
        "ui-js",
        {"name": "gone", "deleted": True, "branch": "wip", "rbac_context": "alice"},
    )
    When.http.post(
        reverse("ui-js-list"), data={"name": "none", "rbac_context": ""}, format="json"
    )
    Then.http.status_code_is(400)
    When.http.get(url, HTTP_ACCEPT="application/json")
    When.http.delete(url, HTTP_IF_MATCH=Http.response["ETag"])
    Then.http.status_code_is(204)


@pytest.mark.django_db
@test_context
def test_js_files_served_versioned_private_immutable(
    todo_list, todo_reader_login, admin_login
):
    setup_admin_and_customer()
    app = make_js("app", "window.x = 1")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    response = get_js(file_url(app, ADMIN))
    assert response["Content-Type"] == "text/javascript; charset=utf-8"
    assert response["Cache-Control"] == PRIVATE
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response.content == b"window.x = 1"
    old_url = file_url(app)
    assert get_js(old_url).content == b"window.x = 1"
    # Only GET.
    When.http.post(old_url, data="x", content_type="text/javascript")
    Then.http.status_code_is(405)
    Given.http.force_authenticate(admin_login)
    When.http.put(
        reverse("ui-js-detail", args=[app.pk]),
        data={**app.content, "js": "window.x = 2"},
        format="json",
        HTTP_IF_MATCH=f'"{app.etag}"',
    )
    Then.http.status_code_is(200)
    app.refresh_from_db()
    Given.http.force_authenticate(todo_reader_login)
    # A stale ETag is a 404; so is an unknown name.
    get_js(old_url, status=404)
    get_js("/api/ui-js-files/nope/x.js", status=404)
    assert get_js(file_url(app)).content == b"window.x = 2"


@pytest.mark.django_db
@test_context
def test_js_gated_by_branch_get_under_prefix(todo_list, todo_reader_login):
    setup_admin_and_customer()
    app = make_js("app")
    url = file_url(app, ADMIN)
    # The reader sees the page at the API, and so its script...
    Given.http.force_authenticate(todo_reader_login)
    get_js(file_url(app))
    # ...but not under a prefix whose branch they may not GET.
    get_js(url, status=403)
    Given.http.force_authenticate(None)
    When.http.get(mounted(ADMIN), HTTP_ACCEPT="application/json")
    refused = Http.response.status_code
    assert refused in (401, 403)
    get_js(url, status=refused)
    # A working branch's item is not shown to a viewer who may not GET it.
    make_branch("wip", "project-a")
    wip = make_js("app", "secret()", branch="wip", context="project-a")
    get_js(file_url(wip), status=404, **{BRANCH_HEADER: "wip"})
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    assert get_js(url).content == app.content["js"].encode()
    assert get_js(file_url(wip), **{BRANCH_HEADER: "wip"}).content == b"secret()"
    get_js(file_url(wip), status=404)


@pytest.mark.django_db
@test_context
def test_page_links_scripts_in_order(todo_list, todo_reader_login):
    third = make_js("c", order=2)
    second = make_js("b", order=1)
    first = make_js("a", order=1)
    hidden = make_js("hidden")
    make_branch("wip", "project-a", prefix="/wip/")
    make_js("hidden", "", branch="wip", deleted=True, context="project-a")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    page = get_html(api_url(todo_list))
    srcs = script_srcs(page)
    assert srcs[-4:] == [file_url(i) for i in (hidden, first, second, third)]
    # They follow scrud-ui.js.
    assert "scrud-ui" in srcs[-5]
    assert len(srcs) == 6
    page = get_html(mounted("/wip/", todo_list))
    assert script_srcs(page)[-3:] == [
        file_url(i, "/wip/") for i in (first, second, third)
    ]
    # A fragment has no <head>.
    When.http.get(api_url(todo_list), HTTP_ACCEPT="text/html", HTTP_HX_REQUEST="true")
    assert "ui-js-files" not in Http.response.content.decode()


@pytest.mark.django_db
@test_context
def test_page_without_js_items_has_no_extra_scripts(todo_list, todo_reader_login):
    Given.http.force_authenticate(todo_reader_login)
    page = get_html(api_url(todo_list))
    assert len(script_srcs(page)) == 2
    assert "scrud-ui" in script_srcs(page)[-1]
    assert page.count("<script src=") == 2


@pytest.mark.django_db
@test_context
def test_js_refuses_closing_script_tag(admin_login):
    Given.http.force_authenticate(admin_login)
    for text in ("a() </script><b>", "</SCRIPT >", "</Script"):
        When.http.post(
            reverse("ui-js-list"), data=js_body(name="x", js=text), format="json"
        )
        Then.http.status_code_is(400)
        assert problem()["type"].endswith("/ui-js-invalid")
        assert "</script" in problem()["detail"].lower()
    When.http.post(
        reverse("ui-js-list"), data=js_body(name="big", js="a" * 262145), format="json"
    )
    Then.http.status_code_is(400)
    When.http.post(
        reverse("ui-js-list"), data=js_body(name="ok", js="a" * 262144), format="json"
    )
    Then.http.status_code_is(201)


@pytest.mark.django_db
@test_context
def test_js_write_needs_ui_js_grant_not_ui_components():
    make_branch("wip", "alice")
    user = assign_ui_role((UI_BRANCH_TYPE_URI, UI_COMPONENT_TYPE_URI), "alice")
    Given.http.force_authenticate(user)
    When.http.post(
        reverse("ui-js-list"),
        data=js_body(branch="wip", rbac_context="alice"),
        format="json",
    )
    Then.http.status_code_is(403)
    grant(
        user,
        {"http.POST": [UI_JS_TYPE_URI, collection_type_uri_for(UI_JS_TYPE_URI)]},
        "alice",
    )
    When.http.post(
        reverse("ui-js-list"),
        data=js_body(branch="wip", rbac_context="alice"),
        format="json",
    )
    Then.http.status_code_is(201)


@pytest.mark.django_db
@test_context
def test_push_pull_restore_and_conflicts_cover_js(admin_login):
    assert ITEM_KINDS["js"] == UI_JS_TYPE_URI
    Given.http.force_authenticate(admin_login)
    main_js = make_js("app", "a()")
    make_wip(admin_login)
    override = create("ui-js", js_body(js="b()", **WIP))
    assert override.content["base"] == main_js.etag
    # Versions snapshot js.
    number = versions_of("wip")[-1]
    items = snapshot("wip", number)["items"]
    assert [i["name"] for i in items["js"]] == ["app"]
    # A change on main since the override was made conflicts.
    ResourceRegistration.update(
        {**main_js.content, "js": "changed()"}, "ui-js", main_js.pk
    )
    main_js.refresh_from_db()
    before = stored_state()
    push()
    Then.http.status_code_is(409)
    assert problem()["conflicts"] == [
        {
            "kind": "js",
            "key": ["app"],
            "base": override.content["base"],
            "current": main_js.etag,
        }
    ]
    assert stored_state() == before
    # Without the grant on ui-js, a push is refused; with it, applied.
    Resource.objects.filter(pk=override.pk).update(
        content={**override.content, "base": main_js.etag}
    )
    Given.http.force_authenticate(pusher(**{"": {"http.PUT": [UI_COMPONENT_TYPE_URI]}}))
    push()
    Then.http.status_code_is(403)
    Given.http.force_authenticate(pusher(**{"": {"http.PUT": [UI_JS_TYPE_URI]}}))
    push()
    Then.http.status_code_is(201)
    main_js.refresh_from_db()
    assert main_js.content["js"] == "b()"
    assert own_rows(UI_JS_TYPE_URI, "wip") == []
    # Restore brings the override back.
    Given.http.force_authenticate(admin_login)
    push(source=None, restore=number, target="wip", context="alice")
    Then.http.status_code_is(201)
    assert [r["js"] for r in own_rows(UI_JS_TYPE_URI, "wip")] == ["b()"]
    assert conflicts_of("wip") in (None, [])
