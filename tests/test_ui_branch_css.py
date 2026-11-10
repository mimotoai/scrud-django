"""CSS items (``ui-css``): stylesheets' text, per branch, served at versioned
URLs (``ui-css-files/<name>/<etag>.css``) under the API's URLs and under a
published branch's prefix, and linked by the built-in page. These tests serve
the API at ``/api/`` (``tests.mounted_urls``)."""

import re
from html import unescape

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.ui.branches import UI_CSS_TYPE_URI
from scrud_django.ui.branches.versions import snapshot

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_prefixes import (
    ADMIN,
    api_url,
    get_html,
    mounted,
    setup_admin_and_customer,
)
from .test_ui_branch_push import create, make_wip, own_rows, push, pusher
from .test_ui_branch_render import BRANCH_HEADER, may_select_branches
from .test_ui_branch_types import make_branch, problem
from .test_ui_http import make_component

PRIVATE = "private, max-age=31536000, immutable"


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def make_css(name, css="body { margin: 0 }", branch=None, context="", **fields):
    """A CSS item stored as is (not through the API)."""
    reverse("ui-css-list")  # the URLconf registers the types
    body = {"name": name, "css": css, "rbac_context": context, **fields}
    if branch is not None:
        body["branch"] = branch
    return ResourceRegistration.register(body, "ui-css")


def css_hrefs(page):
    return [
        unescape(h) for h in re.findall(r'<link rel="stylesheet" href="([^"]*)"', page)
    ]


def file_url(item, base="/api/"):
    return f"{base}ui-css-files/{item.content['name']}/{item.etag}.css"


def get_css(url, status=200, **headers):
    When.http.get(url, **headers)
    Then.http.status_code_is(status)
    return Http.response


@pytest.mark.django_db
@test_context
def test_css_served_under_prefix_with_versioned_url(todo_list, todo_reader_login):
    setup_admin_and_customer()
    site = make_css("site", "h2 { color: red }")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    page = get_html(mounted(ADMIN, todo_list))
    assert css_hrefs(page) == [file_url(site, ADMIN)]
    response = get_css(file_url(site, ADMIN))
    assert response["Content-Type"] == "text/css; charset=utf-8"
    assert response["Cache-Control"] == PRIVATE
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response.content == b"h2 { color: red }"
    # The API's URLs link and serve it too.
    assert css_hrefs(get_html(api_url(todo_list))) == [file_url(site)]
    assert get_css(file_url(site)).content == b"h2 { color: red }"
    # Only GET.
    When.http.post(file_url(site), data="x", content_type="text/css")
    Then.http.status_code_is(405)


@pytest.mark.django_db
@test_context
def test_old_etag_url_is_404_after_edit(todo_list, todo_reader_login, admin_login):
    site = make_css("site", "a { color: red }")
    old_url = file_url(site)
    Given.http.force_authenticate(todo_reader_login)
    get_css(old_url)
    Given.http.force_authenticate(admin_login)
    When.http.put(
        reverse("ui-css-detail", args=[site.pk]),
        data={**site.content, "css": "a { color: blue }"},
        format="json",
        HTTP_IF_MATCH=f'"{site.etag}"',
    )
    Then.http.status_code_is(200)
    site.refresh_from_db()
    Given.http.force_authenticate(todo_reader_login)
    assert file_url(site) != old_url
    get_css(old_url, status=404)
    assert get_css(file_url(site)).content == b"a { color: blue }"
    assert css_hrefs(get_html(api_url(todo_list))) == [file_url(site)]
    # Neither an unknown name nor a deleted item is served.
    get_css("/api/ui-css-files/nope/x.css", status=404)
    Resource.objects.filter(pk=site.pk).update(
        content={**site.content, "deleted": True}
    )
    get_css(file_url(site), status=404)


@pytest.mark.django_db
@test_context
def test_page_links_effective_css_in_order(todo_list, todo_reader_login):
    third = make_css("c", order=2)
    second = make_css("b", order=1)
    first = make_css("a", order=1)
    hidden = make_css("hidden")
    make_branch("wip", "project-a", prefix="/wip/")
    make_css("hidden", "", branch="wip", deleted=True, context="project-a")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    page = get_html(api_url(todo_list))
    assert css_hrefs(page) == [file_url(i) for i in (hidden, first, second, third)]
    # The published branch hides main's item with its deletion marker.
    page = get_html(mounted("/wip/", todo_list))
    assert css_hrefs(page) == [file_url(i, "/wip/") for i in (first, second, third)]
    # A fragment has no <head>.
    When.http.get(api_url(todo_list), HTTP_ACCEPT="text/html", HTTP_HX_REQUEST="true")
    assert "stylesheet" not in Http.response.content.decode()


@pytest.mark.django_db
@test_context
def test_css_on_working_branch_overrides_published(todo_list, todo_reader_login):
    main = make_css("site", "p { color: red }")
    make_branch("wip", "project-a")
    wip = make_css("site", "p { color: green }", branch="wip", context="project-a")
    # A viewer who may not GET the branch is not shown it: its URL is 404.
    Given.http.force_authenticate(todo_reader_login)
    get_css(file_url(wip), status=404, **{BRANCH_HEADER: "wip"})
    assert css_hrefs(get_html(api_url(todo_list), **{BRANCH_HEADER: "wip"})) == [
        file_url(main)
    ]
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    page = get_html(api_url(todo_list), **{BRANCH_HEADER: "wip"})
    assert css_hrefs(page) == [file_url(wip)]
    response = get_css(file_url(wip), **{BRANCH_HEADER: "wip"})
    assert response.content == b"p { color: green }"
    assert response["Cache-Control"] == PRIVATE
    assert response["X-Content-Type-Options"] == "nosniff"
    # Not without the selection (main's item has another ETag)...
    get_css(file_url(wip), status=404)
    assert get_css(file_url(main)).content == b"p { color: red }"
    assert get_css(file_url(main))["Cache-Control"] == PRIVATE
    # main's item is not served as the working branch's either.
    get_css(file_url(main), status=404, **{BRANCH_HEADER: "wip"})


@pytest.mark.django_db
@test_context
def test_css_refused_without_get_on_branch(todo_list, todo_reader_login):
    setup_admin_and_customer()
    site = make_css("site")
    url = file_url(site, ADMIN)
    # The reader sees the page at the API, and so its CSS...
    Given.http.force_authenticate(todo_reader_login)
    get_css(file_url(site))
    # ...but not under a prefix whose branch they may not GET.
    get_css(url, status=403)
    Given.http.force_authenticate(None)
    # The anonymous get what any refused GET under the mount gets.
    When.http.get(mounted(ADMIN), HTTP_ACCEPT="application/json")
    refused = Http.response.status_code
    assert refused in (401, 403)
    get_css(url, status=refused)
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    assert get_css(url).content == site.content["css"].encode()


@pytest.mark.django_db
@test_context
def test_css_is_not_affected_by_stored_components(todo_reader_login):
    make_component("scrud:page", "<html>{{ body }}</html>")
    make_component("css", "<b>{{ value }}</b>")
    site = make_css("site", "x { y: z }")
    Given.http.force_authenticate(todo_reader_login)
    response = get_css(file_url(site), HTTP_ACCEPT="text/html")
    assert response.content == b"x { y: z }"
    assert response["Content-Type"] == "text/css; charset=utf-8"


def css_body(**overrides):
    return {"name": "site", "css": "a { b: c }", "rbac_context": "", **overrides}


@pytest.mark.django_db
@test_context
def test_css_validation(admin_login):
    Given.http.force_authenticate(admin_login)
    created = create("ui-css", css_body())
    assert created.content["branch"] == "main" and created.content["base"] is None
    for text in ("a{} </style><script>", "</STYLE >", "</Style"):
        When.http.post(
            reverse("ui-css-list"), data=css_body(name="x", css=text), format="json"
        )
        Then.http.status_code_is(400)
        assert "</style" in problem()["detail"].lower()
    When.http.post(
        reverse("ui-css-list"),
        data=css_body(name="big", css="a" * 262145),
        format="json",
    )
    Then.http.status_code_is(400)
    When.http.post(
        reverse("ui-css-list"),
        data=css_body(name="ok", css="a" * 262144),
        format="json",
    )
    Then.http.status_code_is(201)
    # Names are unique on a branch, not across branches.
    When.http.post(reverse("ui-css-list"), data=css_body(), format="json")
    Then.http.status_code_is(400)
    assert "already exists" in problem()["detail"]
    make_branch("wip", "alice")
    create("ui-css", css_body(branch="wip", rbac_context="alice"))
    # A deletion marker needs only a name.
    create(
        "ui-css",
        {"name": "gone", "deleted": True, "branch": "wip", "rbac_context": "alice"},
    )
    # The text is required otherwise.
    When.http.post(
        reverse("ui-css-list"), data={"name": "none", "rbac_context": ""}, format="json"
    )
    Then.http.status_code_is(400)


@pytest.mark.django_db
@test_context
def test_css_writes_are_versioned(admin_login):
    make_branch("wip", "alice")
    Given.http.force_authenticate(admin_login)
    create("ui-css", css_body(branch="wip", rbac_context="alice"))
    items = snapshot("wip", 1)["items"]
    assert [i["name"] for i in items["css"]] == ["site"]
    assert items["component"] == [] and items["stylesheet"] == []


@pytest.mark.django_db
@test_context
def test_push_applies_css(admin_login):
    Given.http.force_authenticate(admin_login)
    main_css = make_css("site", "a { color: red }")
    make_wip(admin_login)
    create(
        "ui-css", css_body(css="a { color: blue }", branch="wip", rbac_context="alice")
    )
    Given.http.force_authenticate(component_writer_with_css())
    push()
    Then.http.status_code_is(201)
    main_css.refresh_from_db()
    assert main_css.content["css"] == "a { color: blue }"
    assert own_rows(UI_CSS_TYPE_URI, "wip") == []


def component_writer_with_css():

    return pusher(**{"": {"http.PUT": [UI_CSS_TYPE_URI]}})
