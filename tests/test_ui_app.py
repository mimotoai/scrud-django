"""The UI app (`scrud_django.ui.urls`) at ``/``: plain Django pages that read
and write resources through the core and answer HTML as the API's HTML
answers today, refusing what the API refuses, with Django's session user
and CSRF check. These tests serve the API at ``/api/`` and the UI at ``/``
(``tests.ui.UI_URLCONF``)."""

import hashlib
import json
import re
from html import unescape
from urllib.parse import urlsplit

import pytest
from django.test import Client, RequestFactory
from django.urls import reverse
from django.utils.http import quote_etag
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django import registration
from scrud_django.models import Resource
from scrud_django.registration import get_resource_type_for
from scrud_django.ui.branches.selection import (
    MOUNT_ATTR,
    UI_BASE_ATTR,
    Mount,
    cookie_path,
    in_mount,
)

from .factories import UserFactory
from .fixtures import *  # noqa
from .fixtures import (
    RESOURCE_ENDPOINT_DETAIL_NAME,
    RESOURCE_ENDPOINT_LIST_NAME,
    TYPE_URI_TODO_LIST,
    assign_role,
)
from .resource_types import SUMMARIES_TYPE
from .steps import *  # noqa: F403, F401
from .test_ui_branch_css import css_hrefs, make_css
from .test_ui_branch_render import may_select_branches, setup_main_and_wip
from .test_ui_http import (
    CONDITIONAL_PUT,
    FRAME,
    HTML_CONTENT_TYPE,
    STALE,
    assert_not_cached,
    assert_notice_and_reload,
    etag_attribute,
    frame_hrefs,
    html,
    make_list,
    put_json,
    three_items,
    without_content,
)
from .ui import HTML, UI_URLCONF, ui_url

JSON = "application/json"
NEW_LIST = {"title": "Created", "rbac_context": "project-a", "items": []}
CSRF_META = re.compile(r'(<meta name="csrf-token" content=")[^"]*(")')


@pytest.fixture(autouse=True)
def ui_urlconf(settings):
    settings.ROOT_URLCONF = UI_URLCONF


def api_detail(resource):
    return reverse(f"{resource.resource_type.slug}-detail", args=[resource.pk])


def ui_detail(resource):
    return ui_url(api_detail(resource))


def ui_list(slug="todo-lists"):
    return ui_url(reverse(f"{slug}-list"))


def comparable(page):
    """`page` with its per-request values normalised (ruling R3): the CSRF
    token the page carries is masked anew for every response."""
    return CSRF_META.sub(r"\1\2", page)


def id_prefix(href):
    """The prefix of the ids of the form elements of the frame at `href`
    (`scrud_django.ui.render`), which its href determines."""
    return "scrud-" + hashlib.sha256(href.encode()).hexdigest()[:8]


def api_hrefs_as_ui(page, api_path=None):
    """`page`, an API HTML answer, with its ``/api/`` hrefs (paths and
    absolute URLs) as the UI's (ruling R3), and so the ids of the form
    elements of its frame at `api_path` as those of the frame at the UI
    URL."""
    if api_path is not None:
        page = page.replace(id_prefix(api_path), id_prefix(ui_url(api_path)))
    return page.replace('"/api/', '"/').replace(
        "http://testserver/api/", "http://testserver/"
    )


def assert_html():
    assert Http.response["Content-Type"] == HTML_CONTENT_TYPE, Http.response[
        "Content-Type"
    ]


def pager_href(rel, page):
    found = re.search(rf'<a rel="{rel}" href="([^"]*)"', page)
    return unescape(found.group(1)) if found else None


def stored(resource):
    return Resource.objects.get(pk=resource.pk)


@pytest.mark.django_db
@test_context
def test_a_page_renders_at_the_ui_url(todo_list, todo_owner_login):
    Given.http.force_login(todo_owner_login)
    When.http.get(ui_detail(todo_list))
    Then.http.status_code_is(200)
    assert_html()
    page = html()
    assert FRAME in page and "Buy milk" in page
    assert page.lower().startswith("<!doctype html>")
    # Its frame (navigation and writes alike) is at the UI URL.
    assert f'data-scrud-href="{ui_detail(todo_list)}"' in page
    assert f'hx-put="{ui_detail(todo_list)}"' in page
    ui_etag = Http.response["ETag"]

    When.http.get(api_detail(todo_list), HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    assert Http.response["ETag"] == ui_etag
    # Normalised: the CSRF token, and the API's hrefs as the UI's.
    assert comparable(page) == comparable(
        api_hrefs_as_ui(html(), api_detail(todo_list))
    )


@pytest.mark.django_db
@test_context
def test_a_fragment_renders_at_the_ui_url(todo_list, todo_owner_login):
    Given.http.force_login(todo_owner_login)
    When.http.get(ui_detail(todo_list), HTTP_HX_REQUEST="true")
    Then.http.status_code_is(200)
    fragment = html()
    assert fragment.startswith('<form class="scrud-resource"')
    assert etag_attribute(Http.response["ETag"]) in fragment
    When.http.get(api_detail(todo_list), HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true")
    assert fragment == api_hrefs_as_ui(html(), api_detail(todo_list))


@pytest.mark.django_db
@test_context
def test_links_in_a_ui_list_are_ui_urls(todo_list, todo_reader_login):
    make_list("Second", context="project-a")
    Given.http.force_login(todo_reader_login)
    When.http.get(ui_list() + "?page_size=1")
    Then.http.status_code_is(200)
    assert_html()
    first = html()
    next_href = pager_href("next", first)
    assert next_href is not None
    When.http.get(next_href)
    Then.http.status_code_is(200)
    second = html()
    previous_href = pager_href("prev", second)
    hrefs = [
        *frame_hrefs(first),
        *frame_hrefs(second),
        next_href,
        previous_href,
    ]
    assert len(frame_hrefs(first)) == len(frame_hrefs(second)) == 1
    for href in hrefs:
        assert urlsplit(href).path.startswith("/todo-lists/"), href
    assert "/api/" not in first and "/api/" not in second
    # The frames are those of the API's list.
    When.http.get(reverse("todo-lists-list") + "?page_size=1", HTTP_ACCEPT=JSON)
    assert frame_hrefs(first) == [
        api_hrefs_as_ui(f'"{item["href"]}"').strip('"')
        for item in Http.response.json()["content"]
    ]


# Who asks: each of these users, and the anonymous user.
WHO = ["login_no_roles", "login_other_context", "todo_reader_login", None]
# What they ask: (method, the item (else the list)).
REQUESTS = [
    ("GET", True),
    ("PUT", True),
    ("DELETE", True),
    ("GET", False),
    ("POST", False),
]


def send(method, url, resource, body, **headers):
    """The status of `method` on `url` (`resource`'s item or list), a write
    sending `body` (a PUT and a DELETE at `resource`'s ETag)."""
    if_match = {"HTTP_IF_MATCH": quote_etag(resource.etag)}
    if method == "GET":
        When.http.get(url, **headers)
    elif method == "PUT":
        put_json(url, body, **if_match, **headers)
    elif method == "DELETE":
        When.http.delete(url, **if_match, **headers)
    else:
        When.http.post(url, data=json.dumps(body), content_type=JSON, **headers)
    return Http.response.status_code


@pytest.mark.django_db
@pytest.mark.parametrize("who", WHO)
@pytest.mark.parametrize("method,item", REQUESTS)
@pytest.mark.parametrize("slug", ["todo-lists", "partner-profiles"])
@test_context
def test_page_and_api_refuse_alike(
    request, todo_list, partner_profiles, who, method, item, slug
):
    """Review Focus 2: the page answers the status `/api/` answers the same
    request (401, 403, or 404 for a resource hidden as a missing one), and
    a refused one writes nothing. (The anonymous user may create partner
    profiles: `tests.rbac_defaults`.)"""
    if who is not None:
        Given.http.force_login(request.getfixturevalue(who))
    resource = todo_list if slug == "todo-lists" else partner_profiles
    body = dict(resource.content)
    if slug == "todo-lists":
        body["title"] = "Written"
    api = api_detail(resource) if item else reverse(f"{slug}-list")
    count = Resource.objects.count()

    page_status = send(method, ui_url(api), resource, body)
    page = html()
    if page_status >= 400:
        assert_html()
        assert_not_cached()
        assert FRAME not in page and "Buy milk" not in page
        assert stored(resource).etag == resource.etag
        assert Resource.objects.count() == count
    api_status = send(method, api, resource, body, HTTP_ACCEPT=JSON)
    assert page_status == api_status, (page_status, api_status)


@pytest.mark.django_db
@test_context
def test_a_ui_put_writes_and_rerenders_the_frame(todo_list, todo_owner_login):
    url = ui_detail(todo_list)
    body = {**todo_list.content, "title": "Weekend shopping"}
    Given.http.force_login(todo_owner_login)
    put_json(
        url, body, HTTP_IF_MATCH=quote_etag(todo_list.etag), HTTP_HX_REQUEST="true"
    )
    Then.http.status_code_is(200)
    assert_html()
    assert_not_cached()
    output = html()
    assert "Weekend shopping" in without_content(output)
    etag = Http.response["ETag"]
    assert etag == quote_etag(stored(todo_list).etag) != quote_etag(todo_list.etag)
    assert etag_attribute(etag) in output
    assert f'hx-put="{url}"' in output
    assert stored(todo_list).content["title"] == "Weekend shopping"


@pytest.mark.django_db
@test_context
def test_a_refused_ui_put_rerenders_the_form_with_errors(todo_list, todo_owner_login):
    url = ui_detail(todo_list)
    if_match = quote_etag(todo_list.etag)
    # Refused by a grant's condition (403): the submitted body as the form.
    user = assign_role(
        {"http.GET": [TYPE_URI_TODO_LIST], **CONDITIONAL_PUT}, "project-a"
    )
    Given.http.force_login(user)
    put_json(
        url, three_items(todo_list), HTTP_IF_MATCH=if_match, HTTP_HX_REQUEST="true"
    )
    Then.http.status_code_is(403)
    assert_html()
    output = html()
    assert "At most 3 items" in output
    assert output.startswith('<form class="scrud-resource"')
    assert "Item 2" in without_content(output)
    assert etag_attribute(if_match) in output
    assert f'hx-put="{url}"' in output
    # The form writes at the stored resource's ETag, never at the If-Match.
    put_json(url, three_items(todo_list), HTTP_IF_MATCH="*", HTTP_HX_REQUEST="true")
    Then.http.status_code_is(403)
    assert etag_attribute(if_match) in html()
    assert 'data-scrud-etag="*"' not in html()

    # Refused by the schema (400): the form, with the field's error.
    Given.http.force_login(todo_owner_login)
    body = {**todo_list.content, "items": [{"text": 5, "done": False}]}
    put_json(url, body, HTTP_IF_MATCH=if_match, HTTP_HX_REQUEST="true")
    Then.http.status_code_is(400)
    assert_html()
    output = html()
    assert output.startswith('<form class="scrud-resource"')
    assert etag_attribute(if_match) in output
    assert "must be string" in output
    assert stored(todo_list).etag == todo_list.etag


@pytest.mark.django_db
@test_context
def test_a_refused_ui_put_by_a_reader_gets_no_form(todo_list, todo_reader_login):
    Given.http.force_login(todo_reader_login)
    body = {**todo_list.content, "title": "Crafted title"}
    for headers in ({"HTTP_HX_REQUEST": "true"}, {}):
        put_json(
            ui_detail(todo_list),
            body,
            HTTP_IF_MATCH=quote_etag(todo_list.etag),
            **headers,
        )
        Then.http.status_code_is(403)
        assert_html()
        output = html()
        assert "Crafted title" not in output and "<form" not in output
        assert FRAME not in output and 'class="scrud-notice' in output


@pytest.mark.django_db
@test_context
def test_a_stale_ui_put_answers_412_with_a_reloading_frame(todo_list, todo_owner_login):
    url = ui_detail(todo_list)
    Given.http.force_login(todo_owner_login)
    put_json(
        url,
        {**todo_list.content, "title": "Too late"},
        HTTP_IF_MATCH='"stale"',
        HTTP_HX_REQUEST="true",
        HTTP_SCRUD_UI_SCOPE="sidebar",
        HTTP_SCRUD_UI_POINTER="/items/0",
    )
    Then.http.status_code_is(412)
    output = assert_notice_and_reload(url)
    assert STALE in output and "Too late" not in output
    assert stored(todo_list).etag == todo_list.etag


@pytest.mark.django_db
@test_context
def test_a_stale_ui_delete_answers_412_with_a_reloading_frame(
    todo_list, todo_owner_login
):
    url = ui_detail(todo_list)
    Given.http.force_login(todo_owner_login)
    When.http.delete(
        url,
        HTTP_IF_MATCH='"stale"',
        HTTP_HX_REQUEST="true",
        HTTP_SCRUD_UI_SCOPE="sidebar",
        HTTP_SCRUD_UI_POINTER="/items/0",
    )
    Then.http.status_code_is(412)
    assert STALE in assert_notice_and_reload(url)
    assert Resource.objects.filter(pk=todo_list.pk).exists()


@pytest.mark.django_db
@test_context
def test_a_ui_put_without_if_match_answers_as_the_api(todo_list, todo_owner_login):
    body = {**todo_list.content, "title": "Unconditional"}
    Given.http.force_login(todo_owner_login)
    put_json(ui_detail(todo_list), body, HTTP_HX_REQUEST="true")
    page_status = Http.response.status_code
    assert_html()
    assert 'class="scrud-notice' in html() and FRAME not in html()
    assert stored(todo_list).etag == todo_list.etag
    put_json(api_detail(todo_list), body, HTTP_ACCEPT=JSON)
    assert page_status == Http.response.status_code == 428
    assert stored(todo_list).etag == todo_list.etag


@pytest.mark.django_db
@test_context
def test_a_ui_delete_deletes(todo_list, todo_owner_login):
    Given.http.force_login(todo_owner_login)
    When.http.delete(
        ui_detail(todo_list),
        HTTP_IF_MATCH=quote_etag(todo_list.etag),
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(204)
    assert Http.response.content == b""
    assert not Resource.objects.filter(pk=todo_list.pk).exists()


@pytest.mark.django_db
@test_context
def test_a_ui_delete_of_a_resource_in_use_answers_409():
    """A schema a type uses is protected: the 409 keeps the item on the page,
    with a notice and a frame that reloads its current state."""
    reverse("todo-lists-list")
    schema = get_resource_type_for("todo-lists").schema
    url = ui_url(reverse("json-schema-detail", args=[schema.pk]))
    Given.http.force_login(UserFactory(is_staff=True, is_superuser=True))
    When.http.delete(
        url,
        HTTP_IF_MATCH=quote_etag(schema.etag),
        HTTP_HX_REQUEST="true",
        HTTP_SCRUD_UI_SCOPE="sidebar",
        HTTP_SCRUD_UI_POINTER="/items/0",
    )
    Then.http.status_code_is(409)
    assert "in use" in assert_notice_and_reload(url).lower()
    assert Resource.objects.filter(pk=schema.pk).exists()


@pytest.mark.django_db
@test_context
def test_a_ui_post_creates_and_renders_the_new_resource(todo_owner_login):
    Given.http.force_login(todo_owner_login)
    When.http.post(
        ui_list(),
        data=json.dumps(NEW_LIST),
        content_type=JSON,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(201)
    assert_html()
    created = Resource.objects.get(content__title="Created")
    url = ui_detail(created)
    assert Http.response["Location"] == f"http://testserver{url}"
    assert f'data-scrud-href="{url}"' in html()
    assert etag_attribute(Http.response["ETag"]) in html()
    assert Http.response["ETag"] == quote_etag(created.etag)
    assert "Created" in without_content(html())


@pytest.mark.django_db
@test_context
def test_a_refused_ui_post_answers_a_notice(todo_owner_login):
    Given.http.force_login(todo_owner_login)
    count = Resource.objects.count()
    for body, status in (
        ({**NEW_LIST, "items": [{"text": 5}]}, 400),
        ({**NEW_LIST, "rbac_context": "project-b"}, 403),
    ):
        When.http.post(
            ui_list(), data=json.dumps(body), content_type=JSON, HTTP_HX_REQUEST="true"
        )
        Then.http.status_code_is(status)
        assert_html()
        assert 'class="scrud-notice' in html() and FRAME not in html()
    assert Resource.objects.count() == count


@pytest.mark.django_db
@pytest.mark.parametrize(
    "data,content_type,status",
    [
        ('{"title": ', JSON, 400),
        ("title=x", "application/x-www-form-urlencoded", 415),
        ("", JSON, 400),
    ],
)
@test_context
def test_a_ui_put_of_a_body_the_api_refuses_answers_as_the_api(
    todo_list, todo_owner_login, data, content_type, status
):
    Given.http.force_login(todo_owner_login)
    for url, headers in (
        (ui_detail(todo_list), {}),
        (api_detail(todo_list), {"HTTP_ACCEPT": JSON}),
    ):
        When.http.put(
            url,
            data=data,
            content_type=content_type,
            HTTP_IF_MATCH=quote_etag(todo_list.etag),
            **headers,
        )
        Then.http.status_code_is(status)
    assert stored(todo_list).etag == todo_list.etag


@pytest.mark.django_db
@test_context
def test_a_ui_write_without_the_csrf_token_is_refused(todo_list, todo_owner_login):
    """Review Focus 1: a write by the session user without the CSRF token is
    refused (403) and writes nothing; with it, it is made."""
    client = Client(enforce_csrf_checks=True)
    client.force_login(todo_owner_login)
    url = ui_detail(todo_list)
    if_match = quote_etag(todo_list.etag)
    body = json.dumps({**todo_list.content, "title": "Forged"})
    count = Resource.objects.count()
    for response in (
        client.put(url, body, content_type=JSON, HTTP_IF_MATCH=if_match),
        client.delete(url, HTTP_IF_MATCH=if_match),
        client.post(ui_list(), json.dumps(NEW_LIST), content_type=JSON),
    ):
        assert response.status_code == 403
        assert b"CSRF" in response.content
    assert stored(todo_list).etag == todo_list.etag
    assert Resource.objects.count() == count

    page = client.get(url).content.decode()
    token = re.search(r'<meta name="csrf-token" content="([^"]*)"', page).group(1)
    response = client.put(
        url, body, content_type=JSON, HTTP_IF_MATCH=if_match, HTTP_X_CSRFTOKEN=token
    )
    assert response.status_code == 200
    assert stored(todo_list).content["title"] == "Forged"


@pytest.mark.django_db
@test_context
def test_a_page_answers_304_to_its_etag(todo_list, todo_reader_login):
    Given.http.force_login(todo_reader_login)
    for url in (ui_detail(todo_list), ui_list()):
        When.http.get(url)
        Then.http.status_code_is(200)
        etag = Http.response["ETag"]
        assert Http.response["Last-Modified"]
        When.http.get(url, HTTP_IF_NONE_MATCH=etag)
        Then.http.status_code_is(304)
        assert Http.response.content == b""
        assert Http.response["ETag"] == etag
        assert_not_cached()


@pytest.mark.django_db
@test_context
def test_pages_are_not_stored(todo_list, todo_reader_login):
    Given.http.force_login(todo_reader_login)
    for url in (ui_detail(todo_list), ui_list()):
        When.http.get(url)
        Then.http.status_code_is(200)
        assert "no-store" in Http.response["Cache-Control"]
        vary = {v.strip().lower() for v in Http.response["Vary"].split(",")}
        assert {"hx-request", "scrud-ui-scope", "cookie"} <= vary


@pytest.mark.django_db
@test_context
def test_head_of_a_page_has_no_body(todo_list, todo_reader_login):
    Given.http.force_login(todo_reader_login)
    for url in (ui_detail(todo_list), ui_list()):
        When.http.get(url)
        etag = Http.response["ETag"]
        When.http.head(url)
        Then.http.status_code_is(200)
        assert Http.response.content == b""
        assert Http.response["ETag"] == etag
        assert Http.response["Content-Type"] == HTML_CONTENT_TYPE


@pytest.mark.django_db
@test_context
def test_options_on_a_page_is_405_with_allow(todo_list, todo_owner_login):
    Given.http.force_login(todo_owner_login)
    for url, allow in (
        (ui_detail(todo_list), "GET, HEAD, PUT, DELETE"),
        (ui_list(), "GET, HEAD, POST"),
    ):
        for method in ("options", "patch"):
            Http.response = getattr(Http.api_client, method)(url)
            Then.http.status_code_is(405)
            assert Http.response["Allow"] == allow
            assert_html()
            assert 'class="scrud-notice' in html()
    assert stored(todo_list).etag == todo_list.etag


@pytest.mark.django_db
@test_context
def test_the_ui_does_not_claim_app_urls(todo_list, todo_owner_login):
    """Review Focus 3: a URL that looks like a UI route but names no type the
    UI serves is the project's (its page, else Django's 404)."""
    Given.http.force_login(todo_owner_login)
    When.http.get("/shop/todo-lists/")
    Then.http.status_code_is(200)
    assert Http.response.content == b"The shop's to-do lists"
    for url in ("/not-a-type/", "/not-a-type/1/", "/todo-summaries/project-a/"):
        When.http.get(url)
        Then.http.status_code_is(404)
        assert b"scrud-" not in Http.response.content, url
    # A page's URL without its slash is no page, and not redirected to it.
    When.http.get("/todo-lists")
    Then.http.status_code_is(404)
    # A computed type has no list.
    When.http.get("/summaries/")
    Then.http.status_code_is(404)
    assert b"scrud-" not in Http.response.content


@pytest.mark.django_db
@test_context
def test_a_computed_page_renders_at_the_ui_url(todo_owner_login):
    reverse("summaries-detail", args=["k"])  # the URLconf registers the types
    # Its row, which an earlier test's import may have rolled back.
    registration.computed_resource_type(**SUMMARIES_TYPE)
    Given.http.force_login(todo_owner_login)
    url = ui_url(reverse("summaries-detail", args=["k"]))
    When.http.get(url)
    Then.http.status_code_is(200)
    assert_html()
    page = html()
    assert FRAME in page and todo_owner_login.get_username() in page
    etag = Http.response["ETag"]
    When.http.get(url, HTTP_IF_NONE_MATCH=etag)
    Then.http.status_code_is(304)
    for method in ("put", "delete"):
        getattr(When.http, method)(url)
        Then.http.status_code_is(405)
        assert Http.response["Allow"] == "GET, HEAD"
    # Its API page is the same, the hrefs aside.
    When.http.get(url)
    page = html()
    api = reverse("summaries-detail", args=["k"])
    When.http.get(api, HTTP_ACCEPT=HTML)
    assert comparable(page) == comparable(api_hrefs_as_ui(html(), api))
    # The anonymous user is refused, as the API refuses them.
    Http.api_client.logout()
    When.http.get(url)
    page_status = Http.response.status_code
    When.http.get(reverse("summaries-detail", args=["k"]), HTTP_ACCEPT=JSON)
    assert page_status == Http.response.status_code >= 400


@pytest.mark.django_db
@test_context
def test_a_ui_page_links_and_serves_its_css(todo_list, todo_reader_login):
    site = make_css("site", "h2 { color: red }")
    Given.http.force_login(todo_reader_login)
    When.http.get(ui_detail(todo_list))
    hrefs = css_hrefs(html())
    assert hrefs == [f"/ui-css-files/site/{site.etag}.css"]
    When.http.get(hrefs[0])
    Then.http.status_code_is(200)
    assert Http.response["Content-Type"] == "text/css; charset=utf-8"
    assert Http.response["Cache-Control"] == "private, max-age=31536000, immutable"
    assert Http.response["X-Content-Type-Options"] == "nosniff"
    assert Http.response.content == b"h2 { color: red }"
    # A stale URL is a 404; a write is a 405.
    When.http.get(f"/ui-css-files/site/{'0' * 32}.css")
    Then.http.status_code_is(404)
    When.http.post(hrefs[0])
    Then.http.status_code_is(405)


@pytest.mark.django_db
@test_context
def test_a_confirmed_branch_link_keeps_the_branch_for_the_ui(
    todo_list, todo_reader_login
):
    setup_main_and_wip()
    Given.http.force_login(may_select_branches(todo_reader_login))
    url = ui_detail(todo_list)
    When.http.get(f"{url}?ui-branch=wip")
    Then.http.status_code_is(200)
    continue_href = re.search(
        r'class="scrud-branch-continue" href="([^"]*)"', html()
    ).group(1)
    assert unescape(continue_href).startswith(f"{url}?")
    When.http.get(unescape(continue_href), HTTP_SEC_FETCH_SITE="same-origin")
    Then.http.status_code_is(200)
    assert "<i>Buy milk</i>" in html()
    cookie = Http.response.cookies.get("scrud_ui_branch")
    assert cookie is not None and cookie.value == "wip"
    # Scoped to the UI's URLs, which the pages are at.
    assert cookie["path"] == "/"


def test_in_mount_maps_api_hrefs_to_the_ui_base_of_a_page(settings):
    settings.ROOT_URLCONF = UI_URLCONF
    request = RequestFactory().get("/todo-lists/")
    href = "http://testserver/api/todo-lists/1/?page=2"
    # The API's HTML (no UI base): its own hrefs.
    assert in_mount(request, href) == href
    assert cookie_path(request) == "/api/"
    setattr(request, UI_BASE_ATTR, "/")
    assert in_mount(request, href) == "http://testserver/todo-lists/1/?page=2"
    assert in_mount(request, "/api/todo-lists/") == "/todo-lists/"
    assert in_mount(request, "/elsewhere/") == "/elsewhere/"
    assert cookie_path(request) == "/"
    # Under a prefix, the prefix's.
    setattr(request, MOUNT_ATTR, Mount("/shop/", "shop", "/api/"))
    assert in_mount(request, "/api/todo-lists/") == "/shop/todo-lists/"
    assert cookie_path(request) == "/shop/"


@pytest.mark.django_db
@test_context
def test_partner_profile_pages_render(partner_profiles, admin_login):
    Given.http.force_login(admin_login)
    url = ui_url(
        reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.pk})
    )
    When.http.get(url)
    Then.http.status_code_is(200)
    assert FRAME in html()
    When.http.get(ui_url(reverse(RESOURCE_ENDPOINT_LIST_NAME)))
    Then.http.status_code_is(200)
    assert frame_hrefs(html()) == [f"http://testserver{url}"]
