"""Published branches at URL prefixes: under a branch's prefix, a type's URLs
reach the same views as under the API's, with the same checks; only the
presentation differs. These tests serve the API at ``/api/``
(``tests.mounted_urls``)."""

import json
import re
from html import unescape
from urllib.parse import urlsplit

import pytest
from django.core import checks
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import TYPE_URI_TODO_LIST
from .steps import *  # noqa: F403, F401
from .test_ui_branch_render import BRANCH_HEADER, may_select_branches, put_item
from .test_ui_branch_types import make_branch
from .test_ui_http import (
    HTML,
    assert_html,
    frame_hrefs,
    html,
    make_component,
    make_list,
    make_stylesheet,
    put_json,
)

ADMIN = "/admin/resources/"
CUSTOMER = "/"
CHECK_ID = "scrud_django_ui.E001"
# Shows the decisions the component was given.
TITLE = (
    "<{0}>{{{{ value.title }}}}|{{{{ resource.decisions.GET }}}}"
    "|{{{{ resource.decisions.PUT }}}}|{{{{ resource.decisions.DELETE }}}}</{0}>"
)
COOKIE = "scrud_ui_branch"
MIDDLEWARE = "scrud_django.ui.branches.middleware.PublishedBranchMiddleware"


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    """The API at ``/api/``: ``tests.urls`` serves it at ``/``, where no
    prefix applies."""
    settings.ROOT_URLCONF = "tests.mounted_urls"


def publish(name, prefix, parent="main", context="project-a", **fields):
    return make_branch(name, context, parent=parent, prefix=prefix, **fields)


def setup_admin_and_customer():
    """main's ``title`` (an ``<h1>``) for ``ex:TodoList``; ``admin`` at
    `ADMIN` overrides it with an ``<h2>``, ``customer`` at `CUSTOMER` with an
    ``<h3>``."""
    make_component("title", TITLE.format("h1"))
    make_stylesheet("s", [("ex:TodoList", "title")])
    publish("admin", ADMIN)
    publish("customer", CUSTOMER)
    put_item("ui-components", "title", TITLE.format("h2"), "admin")
    put_item("ui-components", "title", TITLE.format("h3"), "customer")


def api_url(resource):
    return reverse(f"{resource.resource_type.slug}-detail", args=[resource.pk])


def mounted(prefix, resource=None, slug="todo-lists"):
    """The URL of `resource` (else of the `slug` list) under `prefix`."""
    return prefix + slug + "/" + (f"{resource.pk}/" if resource is not None else "")


def get_html(url, status=200, **headers):
    When.http.get(url, HTTP_ACCEPT=HTML, **headers)
    Then.http.status_code_is(status)
    return html()


def get_json(url, status=200, **headers):
    When.http.get(url, HTTP_ACCEPT="application/json", **headers)
    Then.http.status_code_is(status)
    return Http.response.json()


def heading(page, tag):
    found = re.search(rf"<{tag}>(.*?)</{tag}>", page)
    assert found, (tag, page)
    return found.group(1)


def hx_puts(page):
    return re.findall(r'hx-put="([^"]*)"', page)


def notices(page):
    found = re.search(r'<div class="scrud-notices">.*?</div>\s*</div>', page, re.S)
    assert found, page
    return found.group(0)


def path_of(href):
    return urlsplit(unescape(href)).path


def prefix_errors(*, overlap=None):
    """The E001 errors the checks report."""
    found = [m for m in checks.run_checks(databases=["default"]) if m.id == CHECK_ID]
    if overlap is not None:
        found = [m for m in found if ("overlap" in m.msg) == overlap]
    return found


@pytest.mark.django_db
@test_context
def test_two_prefixes_render_same_resource_differently(todo_list, todo_reader_login):
    setup_admin_and_customer()
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    admin = get_html(mounted(ADMIN, todo_list))
    customer = get_html(mounted(CUSTOMER, todo_list))
    api = get_html(api_url(todo_list))
    assert "<h2>" in admin and "<h3>" not in admin and "<h1>" not in admin
    assert "<h3>" in customer and "<h2>" not in customer and "<h1>" not in customer
    assert "<h1>" in api and "<h2>" not in api and "<h3>" not in api
    # The same list, with the same decisions.
    assert heading(admin, "h2") == heading(customer, "h3") == heading(api, "h1")
    assert heading(admin, "h2") == "Groceries|allowed|denied|denied"
    # No banner: a published branch at its own prefix is the normal site.
    assert "scrud-branch-banner" not in admin + customer
    # JSON is the same everywhere.
    assert get_json(mounted(ADMIN, todo_list)) == get_json(api_url(todo_list))


@pytest.mark.django_db
@test_context
def test_published_branch_live_over_another(todo_list, todo_reader_login):
    setup_admin_and_customer()
    publish("msp", "/msp/", parent="admin")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    assert "<h2>Groceries" in get_html(mounted("/msp/", todo_list))
    title = Resource.objects.get(content__name="title", content__branch="admin")
    title.content = {**title.content, "template": TITLE.format("h4")}
    title.save()
    page = get_html(mounted("/msp/", todo_list))
    assert "<h4>Groceries" in page and "<h2>" not in page
    # Its own override wins over admin's.
    put_item("ui-components", "title", TITLE.format("h5"), "msp")
    assert "<h5>Groceries" in get_html(mounted("/msp/", todo_list))
    assert "<h4>Groceries" in get_html(mounted(ADMIN, todo_list))


@pytest.mark.django_db
@test_context
def test_prefix_refuses_without_get_on_branch(todo_list, todo_reader_login):
    setup_admin_and_customer()
    Given.http.force_authenticate(todo_reader_login)
    # The reader may GET the list at the API's URL...
    assert "<h1>Groceries" in get_html(api_url(todo_list))
    get_json(api_url(todo_list))
    # ...not under a prefix whose branch they may not GET: refused, as HTML
    # and as JSON, as a GET of a type they may not GET is, with no fallback
    # to another presentation.
    refused_html = notices(get_html(reverse("ui-pushes-list"), status=403))
    get_json(reverse("ui-pushes-list"), status=403)
    refused = (Http.response["Content-Type"], Http.response.json())
    for url in (mounted(ADMIN, todo_list), mounted(ADMIN)):
        page = get_html(url, status=403)
        assert_html()
        assert "Groceries" not in page and notices(page) == refused_html
        get_json(url, status=403)
        assert (Http.response["Content-Type"], Http.response.json()) == refused
    # Anonymous: the 401 the views answer with.
    Given.http.force_authenticate(None)
    When.http.get(api_url(todo_list), HTTP_ACCEPT="application/json")
    anonymous_status = Http.response.status_code
    assert anonymous_status in (401, 403)
    get_json(mounted(ADMIN, todo_list), status=anonymous_status)
    # Allowed to GET the branch, the reader gets the branch's presentation.
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    assert "<h2>Groceries" in get_html(mounted(ADMIN, todo_list))


@pytest.mark.django_db
@test_context
def test_prefix_refuses_anonymous_with_401(todo_list, settings):
    """An anonymous viewer allowed to GET the list but not the branch gets
    the views' answer to the anonymous: a 401."""
    setup_admin_and_customer()
    publish("public", "/public/", context="")
    anonymous = {
        "": {"http.GET": [UI_BRANCH_TYPE_URI]},
        "project-a": {"http.GET": [TYPE_URI_TODO_LIST]},
    }
    with override_settings(
        SCOPED_RBAC={**settings.SCOPED_RBAC, "POLICY_FOR_UNAUTHENTICATED": anonymous}
    ):
        Given.http.force_authenticate(None)
        get_json(api_url(todo_list))
        get_json(mounted("/public/", todo_list))
        # The answer to the anonymous at the API's URL of a type they may
        # not GET.
        get_json(reverse("ui-pushes-list"), status=401)
        refused = (Http.response["Content-Type"], Http.response.json())
        get_json(mounted(ADMIN, todo_list), status=401)
        assert (Http.response["Content-Type"], Http.response.json()) == refused
        page = get_html(mounted(ADMIN, todo_list), status=401)
        assert 'class="scrud-notices"' in page and "Groceries" not in page


@pytest.mark.django_db
@test_context
def test_root_prefix_claims_only_type_slugs(todo_list, todo_reader_login):
    setup_admin_and_customer()
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    assert "<h3>Groceries" in get_html(mounted(CUSTOMER, todo_list))
    # Anything but a type's URLs is the project's (a 404 here).
    for path in (
        "/nope/",
        "/nope/1/",
        "/todo-lists",
        "/admin/resources/nope/",
        "/admin/",
        # Out of the type's routes once rewritten: not claimed.
        f"/todo-lists/{todo_list.pk}/../../services/",
        "/ui-components/../services/",
        f"/todo-lists%2F{todo_list.pk}%2F..%2F..%2Fservices%2F",
        "/todo-lists/x/y/",
    ):
        When.http.get(path, HTTP_ACCEPT="application/json")
        Then.http.status_code_is(404)
        assert "Groceries" not in Http.response.content.decode(), path
    # The project's own URL under a prefix is not shadowed when it is no type.
    When.http.get("/shop/todo-lists/")
    Then.http.status_code_is(200)
    assert Http.response.content == b"The shop's to-do lists"
    # The API's URLs are not mounted: main.
    assert "<h1>Groceries" in get_html(api_url(todo_list))


@pytest.mark.django_db
@test_context
def test_api_urls_read_no_prefix(todo_list, todo_reader_login, settings):
    """The middleware reads no branch for a URL under the API's."""
    setup_admin_and_customer()
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    get_html(api_url(todo_list))  # warm up

    def queries():
        with CaptureQueriesContext(connection) as captured:
            get_html(api_url(todo_list))
        return len(captured)

    with_middleware = queries()
    assert MIDDLEWARE in settings.MIDDLEWARE
    with override_settings(
        MIDDLEWARE=[m for m in settings.MIDDLEWARE if m != MIDDLEWARE]
    ):
        assert queries() == with_middleware


@pytest.mark.django_db
@test_context
def test_writes_under_prefix_use_same_checks(
    todo_list, todo_owner_login, todo_reader_login
):
    setup_admin_and_customer()
    body = {**todo_list.content, "title": "Shopping"}
    answers = {}
    for url in (api_url(todo_list), mounted(ADMIN, todo_list)):
        Given.http.force_authenticate(may_select_branches(todo_owner_login))
        put_json(url, body, HTTP_ACCEPT="application/json")
        no_if_match = (Http.response.status_code, Http.response.json())
        Given.http.force_authenticate(may_select_branches(todo_reader_login))
        put_json(
            url,
            body,
            HTTP_IF_MATCH=f'"{todo_list.etag}"',
            HTTP_ACCEPT="application/json",
        )
        refused = (Http.response.status_code, Http.response.json())
        answers[url] = (no_if_match, refused)
    api, admin = answers.values()
    assert api == admin
    assert api[0][0] == 428 and api[1][0] == 403
    # And an allowed write under the prefix is the same write.
    Given.http.force_authenticate(may_select_branches(todo_owner_login))
    put_json(
        mounted(ADMIN, todo_list),
        body,
        HTTP_IF_MATCH=f'"{todo_list.etag}"',
        HTTP_ACCEPT="application/json",
    )
    Then.http.status_code_is(200)
    todo_list.refresh_from_db()
    assert todo_list.content["title"] == "Shopping"


@pytest.mark.django_db
@test_context
def test_links_stay_in_mount(todo_list, todo_owner_login):
    setup_admin_and_customer()
    make_list("Chores")
    Given.http.force_authenticate(may_select_branches(todo_owner_login))
    # A list: its items' frames, their writes and its pager stay in the mount.
    page = get_html(mounted(ADMIN) + "?page_size=1")
    hrefs = frame_hrefs(page) + hx_puts(page)
    assert len(hrefs) == 2, page
    assert all(path_of(h).startswith(ADMIN + "todo-lists/") for h in hrefs), hrefs
    pager = re.search(r'<a rel="next" href="([^"]*)"', page)
    assert pager and path_of(pager.group(1)) == ADMIN + "todo-lists/", page
    # A resource: its frame writes to its mounted URL.
    page = get_html(mounted(ADMIN, todo_list))
    assert hx_puts(page) == [mounted(ADMIN, todo_list)]
    # JSON keeps the API's hrefs.
    listed = get_json(mounted(ADMIN) + "?page_size=1")
    assert path_of(listed["next"]) == reverse("todo-lists-list")
    assert [path_of(item["href"]) for item in listed["content"]] == [
        path_of(item["href"])
        for item in get_json(reverse("todo-lists-list") + "?page_size=1")["content"]
    ]
    assert all(
        path_of(item["href"]).startswith("/api/todo-lists/")
        for item in listed["content"]
    )
    # A create under the prefix answers with its frame in the mount, and the
    # API's Location.
    When.http.post(
        mounted(ADMIN),
        data=json.dumps({"title": "New", "rbac_context": "project-a", "items": []}),
        content_type="application/json",
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(201)
    assert path_of(Http.response["Location"]).startswith("/api/todo-lists/")
    created = path_of(Http.response["Location"])[len("/api/") :]
    assert hx_puts(html()) == [ADMIN + created]


@pytest.mark.django_db
@test_context
def test_working_branch_browsed_under_its_root_prefix(todo_list, todo_reader_login):
    setup_admin_and_customer()
    make_branch("testing-admin", "project-a", parent="admin")
    put_item("ui-components", "title", TITLE.format("h6"), "testing-admin")
    make_branch("orphan", "project-a", parent=None)
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    fragment = {"HTTP_HX_REQUEST": "true", BRANCH_HEADER: "testing-admin"}
    # In admin's tree: selectable under admin's prefix...
    page = get_html(mounted(ADMIN, todo_list), **fragment)
    assert "<h6>Groceries" in page
    page = get_html(mounted(ADMIN, todo_list))
    assert "<h2>Groceries" in page
    # ...where a shared link asks first, and the cookie it sets is scoped to
    # the prefix.
    continue_page = get_html(mounted(ADMIN, todo_list) + "?ui-branch=testing-admin")
    assert "You're about to browse branch" in continue_page
    continue_href = re.search(
        r'class="scrud-branch-continue" href="([^"]*)"', continue_page
    ).group(1)
    assert path_of(continue_href) == mounted(ADMIN, todo_list)
    page = get_html(unescape(continue_href), HTTP_SEC_FETCH_SITE="same-origin")
    assert "<h6>Groceries" in page and "testing-admin" in page
    cookie = Http.response.cookies.get(COOKIE)
    assert cookie is not None and cookie.value == "testing-admin"
    assert cookie["path"] == ADMIN
    # The banner's links (switcher, toggle, copy, back) stay in the mount.
    banner = re.search(r'<aside class="scrud-branch-banner".*?</aside>', page, re.S)
    links = re.findall(r'href="([^"]*)"', banner.group(0))
    assert links and all(path_of(h) == mounted(ADMIN, todo_list) for h in links)
    # ...not under customer's, or the API's: the published branch, with the
    # banner's note.
    for url in (mounted(CUSTOMER, todo_list), api_url(todo_list)):
        page = get_html(url, **fragment)
        assert "<h6>" not in page
        page = get_html(url, **{BRANCH_HEADER: "testing-admin"})
        assert "The branch <code>testing-admin</code> isn't available" in page
    assert "<h3>Groceries" in get_html(mounted(CUSTOMER, todo_list), **fragment)
    # A working branch with no published ancestor is selectable nowhere.
    for url in (mounted(CUSTOMER, todo_list), mounted(ADMIN, todo_list)):
        page = get_html(url, **{BRANCH_HEADER: "orphan"})
        assert "The branch <code>orphan</code> isn't available" in page


@pytest.mark.django_db
@test_context
def test_overlapping_prefixes_reported_by_check(todo_list):
    # Nested prefixes whose types' URLs don't meet: no error.
    setup_admin_and_customer()
    publish("msp", "/msp/resources/", parent="admin")
    assert prefix_errors() == []
    # A prefix inside another's types' URLs.
    publish("lists", "/msp/resources/todo-lists/", parent="admin")
    overlaps = prefix_errors(overlap=True)
    assert len(overlaps) == 1, overlaps
    assert "/msp/resources/todo-lists/" in overlaps[0].msg
    assert "/msp/resources/" in overlaps[0].msg
    assert isinstance(overlaps[0], checks.Error)
    assert prefix_errors(overlap=False) == []
    # A prefix and a type's slug that the project's URLconf already serves.
    publish("shop", "/shop/", parent="admin")
    shadowed = prefix_errors(overlap=False)
    assert len(shadowed) == 1, shadowed
    assert "/shop/todo-lists/" in shadowed[0].msg
    # A prefix under the API's URLs, where no prefix applies.
    publish("v2", "/api/v2/", parent="admin")
    assert any("/api/v2/" in m.msg for m in prefix_errors(overlap=False))
