"""Published branches at URL prefixes: under a branch's prefix, a type's URLs
reach the UI app's pages of the same URLs, with the same checks (and GET on
the branch); only the presentation differs. Under a prefix only the UI
answers (HTML, whatever the ``Accept``); JSON is at the API's URLs. The UI's
own base is main's: a branch is never served there. These tests serve the
API at ``/api/`` and the UI at ``/`` (``tests.mounted_urls``)."""

import hashlib
import json
import logging
import re
from html import unescape
from urllib.parse import urlsplit

import pytest
from django.core import checks
from django.db import connection
from django.db.utils import OperationalError
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django import ui
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI
from scrud_django.ui.branches.selection import Mount, mount_at

from .fixtures import *  # noqa
from .fixtures import TYPE_URI_TODO_LIST
from .steps import *  # noqa: F403, F401
from .test_ui_branch_render import BRANCH_HEADER, may_select_branches, put_item
from .test_ui_branch_types import make_branch
from .test_ui_http import (
    assert_html,
    frame_hrefs,
    html,
    make_component,
    make_list,
    make_stylesheet,
    put_json,
)
from .ui import UI_URLCONF, comparable, ui_url

ADMIN = "/admin/resources/"
CUSTOMER = "/customer/"
# The UI's own base (``tests.mounted_urls``): main's pages.
UI_BASE = "/"
JSON = "application/json"
CHECK_ID = "scrud_django_ui.W001"
# Shows the decisions the component was given.
TITLE = (
    "<{0}>{{{{ value.title }}}}|{{{{ resource.decisions.GET }}}}"
    "|{{{{ resource.decisions.PUT }}}}|{{{{ resource.decisions.DELETE }}}}</{0}>"
)
COOKIE = "scrud_ui_branch"
MIDDLEWARE = "scrud_django.ui.branches.middleware.PublishedBranchMiddleware"


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    """The API at ``/api/`` and the UI at ``/`` (``tests.urls`` serves the
    API at ``/``, where no prefix applies)."""
    settings.ROOT_URLCONF = UI_URLCONF


def mount_cookie(prefix):
    """The name of the branch cookie under `prefix` (ruling R28)."""
    return f"{COOKIE}_{hashlib.sha256(prefix.encode()).hexdigest()[:12]}"


def choose_by_link(url, branch):
    """Follow a shared link to `branch` at `url`, then its confirmation's
    Continue: the page, with the cookie set."""
    page = get_html(f"{url}?ui-branch={branch}")
    continue_href = re.search(
        r'class="scrud-branch-continue" href="([^"]*)"', page
    ).group(1)
    return get_html(unescape(continue_href), HTTP_SEC_FETCH_SITE="same-origin")


def publish(name, prefix, parent="main", context="project-a", **fields):
    return make_branch(name, context, parent=parent, prefix=prefix, **fields)


def setup_admin_and_customer():
    """main's ``title`` (an ``<h1>``, at the UI's own URLs) for
    ``ex:TodoList``; ``admin`` at `ADMIN` overrides it with an ``<h2>``,
    ``customer`` at `CUSTOMER` with an ``<h3>``."""
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
    """GET the page at `url`, a UI URL or one under a prefix (the UI does
    not negotiate: no ``Accept``)."""
    When.http.get(url, **headers)
    Then.http.status_code_is(status)
    assert_html()
    return html()


def get_json(url, status=200, **headers):
    """GET the JSON at `url`, an API URL."""
    When.http.get(url, HTTP_ACCEPT=JSON, **headers)
    Then.http.status_code_is(status)
    return Http.response.json()


def ui_page(resource=None, slug="todo-lists"):
    """The UI's own URL of `resource` (else of the `slug` list): main's."""
    return mounted(UI_BASE, resource, slug)


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
    """The W001 warnings the checks report."""
    found = [m for m in checks.run_checks(databases=["default"]) if m.id == CHECK_ID]
    if overlap is not None:
        found = [m for m in found if ("overlap" in m.msg) == overlap]
    return found


def refusal(url, status, **headers):
    """The notices of the page at `url`, refused with `status`, asked for as
    HTML (no ``Accept``) and as JSON: the UI's HTML both times, the same."""
    page = get_html(url, status=status, **headers)
    assert "Groceries" not in page
    When.http.get(url, HTTP_ACCEPT=JSON, **headers)
    Then.http.status_code_is(status)
    assert_html()
    assert notices(html()) == notices(page)
    return notices(page)


@pytest.mark.django_db
@test_context
def test_two_prefixes_render_same_resource_differently(todo_list, todo_reader_login):
    setup_admin_and_customer()
    Given.http.force_login(may_select_branches(todo_reader_login))
    admin = get_html(mounted(ADMIN, todo_list))
    customer = get_html(mounted(CUSTOMER, todo_list))
    main = get_html(ui_page(todo_list))
    assert "<h2>" in admin and "<h3>" not in admin and "<h1>" not in admin
    assert "<h3>" in customer and "<h2>" not in customer and "<h1>" not in customer
    assert "<h1>" in main and "<h2>" not in main and "<h3>" not in main
    # The same list, with the same decisions.
    assert heading(admin, "h2") == heading(customer, "h3") == heading(main, "h1")
    assert heading(admin, "h2") == "Groceries|allowed|denied|denied"
    # No banner: a published branch at its own prefix is the normal site.
    assert "scrud-branch-banner" not in admin + customer
    # JSON is the API's, the same with the prefix's branch selected.
    api = get_json(api_url(todo_list))
    assert get_json(api_url(todo_list), **{BRANCH_HEADER: "admin"}) == api


@pytest.mark.django_db
@test_context
def test_a_prefix_serves_the_ui_only(todo_list, todo_reader_login):
    """Under a prefix only the UI answers: a request asking for JSON gets
    the page's HTML (the UI does not negotiate)."""
    setup_admin_and_customer()
    Given.http.force_login(may_select_branches(todo_reader_login))
    for url in (mounted(ADMIN, todo_list), mounted(ADMIN)):
        page = get_html(url)
        for accept in (JSON, "application/json, text/html;q=0.1", "*/*"):
            When.http.get(url, HTTP_ACCEPT=accept)
            Then.http.status_code_is(200)
            assert_html()
            assert 'class="scrud-resource"' in html() and "<h2>Groceries" in html()
            assert comparable(html()) == comparable(page), accept


@pytest.mark.django_db
@test_context
def test_a_prefix_maps_to_the_ui_views(todo_list, todo_reader_login):
    """A page under a prefix is the UI's page of the same URL under the UI's
    base, in the prefix's branch: with no item of its own, ``front`` shows
    main's, so its page is main's page, the prefix aside."""
    setup_admin_and_customer()
    make_list("Chores")
    publish("front", "/front/")
    Given.http.force_login(may_select_branches(todo_reader_login))
    for resource in (todo_list, None):
        page = get_html(mounted("/front/", resource) + "?page_size=1")
        main = get_html(ui_page(resource) + "?page_size=1")
        assert "front/" in page and "<h1>Groceries" in page
        assert comparable(page.replace("/front/", "/")) == comparable(main)


@pytest.mark.django_db
@test_context
def test_published_branch_live_over_another(todo_list, todo_reader_login):
    setup_admin_and_customer()
    publish("msp", "/msp/", parent="admin")
    Given.http.force_login(may_select_branches(todo_reader_login))
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
    Given.http.force_login(todo_reader_login)
    # The reader may GET the list at the UI's own URL (and the API's)...
    assert "<h1>Groceries" in get_html(ui_page(todo_list))
    get_json(api_url(todo_list))
    # ...not under a prefix whose branch they may not GET: refused, as a GET
    # of a type they may not GET is, with no fallback to another
    # presentation; asked for as JSON too, with the UI's HTML.
    refused = notices(get_html(ui_url(reverse("ui-pushes-list")), status=403))
    get_json(reverse("ui-pushes-list"), status=403)
    for url in (mounted(ADMIN, todo_list), mounted(ADMIN)):
        assert refusal(url, 403) == refused
    # JSON is the API's, whatever branch is named: the reader's to-do list.
    api = get_json(api_url(todo_list))
    assert get_json(api_url(todo_list), **{BRANCH_HEADER: "admin"}) == api
    # Anonymous: the status the API answers the anonymous with.
    Http.api_client.logout()
    When.http.get(api_url(todo_list), HTTP_ACCEPT=JSON)
    anonymous_status = Http.response.status_code
    assert anonymous_status in (401, 403)
    refusal(mounted(ADMIN, todo_list), anonymous_status)
    # Allowed to GET the branch, the reader gets the branch's presentation.
    Given.http.force_login(may_select_branches(todo_reader_login))
    assert "<h2>Groceries" in get_html(mounted(ADMIN, todo_list))


@pytest.mark.django_db
@test_context
def test_prefix_refuses_anonymous_with_401(todo_list, settings):
    """An anonymous viewer allowed to GET the list but not the branch gets
    the pages' answer to the anonymous: a 401."""
    setup_admin_and_customer()
    publish("public", "/public/", context="")
    anonymous = {
        "": {"http.GET": [UI_BRANCH_TYPE_URI]},
        "project-a": {"http.GET": [TYPE_URI_TODO_LIST]},
    }
    with override_settings(
        SCOPED_RBAC={**settings.SCOPED_RBAC, "POLICY_FOR_UNAUTHENTICATED": anonymous}
    ):
        api = get_json(api_url(todo_list))
        assert get_json(api_url(todo_list), **{BRANCH_HEADER: "public"}) == api
        assert "<h1>Groceries" in get_html(mounted("/public/", todo_list))
        # The answer to the anonymous at the URL of a type they may not GET.
        get_json(reverse("ui-pushes-list"), status=401)
        refused = notices(get_html(ui_url(reverse("ui-pushes-list")), status=401))
        assert refusal(mounted(ADMIN, todo_list), 401) == refused


@pytest.mark.django_db
@test_context
def test_a_prefix_claims_only_type_slugs(todo_list, todo_reader_login):
    setup_admin_and_customer()
    Given.http.force_login(may_select_branches(todo_reader_login))
    assert "<h3>Groceries" in get_html(mounted(CUSTOMER, todo_list))
    # Anything but a type's URLs is the project's (a 404 here).
    for path in (
        "/customer/nope/",
        "/customer/nope/1/",
        "/admin/resources/nope/",
        "/admin/",
        "/customer/studio/",
        # Out of the type's pages once rewritten: not claimed.
        f"/customer/todo-lists/{todo_list.pk}/../../services/",
        "/customer/ui-components/../services/",
        f"/customer/todo-lists%2F{todo_list.pk}%2F..%2F..%2Fservices%2F",
        "/customer/todo-lists/x/y/",
        # Rewritten into the API's base: never mounted, never the API's.
        "/customer/api/todo-lists/",
        f"/customer/api/todo-lists/{todo_list.pk}/",
        "/customer/api/",
    ):
        When.http.get(path, HTTP_ACCEPT=JSON)
        Then.http.status_code_is(404)
        assert "Groceries" not in Http.response.content.decode(), path
        assert "json" not in Http.response["Content-Type"], path
    # The project's own URL is not shadowed by a prefix whose types it is not.
    When.http.get("/shop/todo-lists/")
    Then.http.status_code_is(200)
    assert Http.response.content == b"The shop's to-do lists"
    # The UI's own URLs are main's.
    assert "<h1>Groceries" in get_html(ui_page(todo_list))


@pytest.mark.django_db
@test_context
def test_a_prefix_without_its_slash_is_not_redirected(todo_list, todo_reader_login):
    setup_admin_and_customer()
    Given.http.force_login(may_select_branches(todo_reader_login))
    for path in ("/customer/todo-lists", "/admin/resources/todo-lists", "/todo-lists"):
        When.http.get(path)
        Then.http.status_code_is(404)
        assert "Location" not in Http.response, path
        assert "Groceries" not in Http.response.content.decode(), path


@pytest.mark.django_db
@test_context
def test_a_branch_at_the_ui_base_is_not_served_there(
    todo_list, todo_reader_login, admin_login
):
    """The UI's own base (``/``) is main's, the site's normal presentation:
    a branch stored with that prefix is not served there (nor does anything
    loop), the system check warns of it, and setting it is refused."""
    setup_admin_and_customer()
    # Setting it is refused.
    Given.http.force_login(admin_login)
    front = publish("front", "/front/")
    When.http.put(
        reverse("ui-branches-detail", args=[front.pk]),
        data=json.dumps({**front.content, "prefix": UI_BASE}),
        content_type="application/json",
        HTTP_ACCEPT=JSON,
        HTTP_IF_MATCH=f'"{front.etag}"',
    )
    Then.http.status_code_is(400)
    assert "UI's own base" in Http.response.json()["detail"]
    front.refresh_from_db()
    assert front.content["prefix"] == "/front/"
    # Stored otherwise, it is not served there.
    publish("root", UI_BASE)
    put_item("ui-components", "title", TITLE.format("h5"), "root")
    Given.http.force_login(todo_reader_login)  # no GET on any branch
    for url in (ui_page(todo_list), ui_page()):
        page = get_html(url)
        assert "<h1>Groceries" in page and "<h5>" not in page
        assert "scrud-branch-banner" not in page
    # The other prefixes are served as ever.
    Given.http.force_login(may_select_branches(todo_reader_login))
    assert "<h2>Groceries" in get_html(mounted(ADMIN, todo_list))
    [warning] = prefix_errors()
    assert "'/'" in warning.msg and "'root'" in warning.msg
    assert "UI's own base" in warning.msg
    # It claims nothing, so no project URL is hidden...
    When.http.get("/shop/todo-lists/")
    Then.http.status_code_is(200)
    assert Http.response.content == b"The shop's to-do lists"
    # ...and no prefix overlaps it: a type's first segment is reported for
    # what it shadows only.
    publish("lists", "/todo-lists/")
    assert prefix_errors(overlap=True) == []
    [shadowing] = [m for m in prefix_errors() if "'lists'" in m.msg]
    assert "/todo-lists/todo-lists/" in shadowing.msg


@pytest.mark.django_db
@test_context
def test_api_urls_read_no_prefix(todo_list, todo_reader_login, settings):
    """The middleware reads no branch for a URL under the API's."""
    setup_admin_and_customer()
    Given.http.force_login(may_select_branches(todo_reader_login))
    get_json(api_url(todo_list))  # warm up

    def queries():
        with CaptureQueriesContext(connection) as captured:
            get_json(api_url(todo_list))
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
    """A write under a prefix is the UI page's: the API's checks, at the
    API's statuses, answered with HTML (asked for as JSON too)."""
    setup_admin_and_customer()
    body = {**todo_list.content, "title": "Shopping"}

    def answers(url, **headers):
        Given.http.force_login(may_select_branches(todo_owner_login))
        put_json(url, body, **headers)
        no_if_match = Http.response
        Given.http.force_login(may_select_branches(todo_reader_login))
        put_json(url, body, HTTP_IF_MATCH=f'"{todo_list.etag}"', **headers)
        return no_if_match, Http.response

    api = [
        (r.status_code, r.json()) for r in answers(api_url(todo_list), HTTP_ACCEPT=JSON)
    ]
    assert api[0][0] == 428 and api[1][0] == 403
    # JSON is the API's: the same with the prefix's branch selected.
    assert [
        (r.status_code, r.json())
        for r in answers(
            api_url(todo_list), HTTP_ACCEPT=JSON, **{BRANCH_HEADER: "admin"}
        )
    ] == api
    for url in (ui_page(todo_list), mounted(ADMIN, todo_list)):
        for headers in ({}, {"HTTP_ACCEPT": JSON}):
            found = answers(url, **headers)
            assert [r.status_code for r in found] == [428, 403], (url, headers)
            assert all(r["Content-Type"].startswith("text/html") for r in found)
    todo_list.refresh_from_db()
    assert todo_list.content["title"] == "Groceries"
    # And an allowed write under the prefix is the same write.
    Given.http.force_login(may_select_branches(todo_owner_login))
    put_json(
        mounted(ADMIN, todo_list),
        body,
        HTTP_IF_MATCH=f'"{todo_list.etag}"',
        HTTP_ACCEPT=JSON,
    )
    Then.http.status_code_is(200)
    assert_html()
    todo_list.refresh_from_db()
    assert todo_list.content["title"] == "Shopping"
    # So is a DELETE.
    When.http.delete(mounted(ADMIN, todo_list), HTTP_IF_MATCH=f'"{todo_list.etag}"')
    Then.http.status_code_is(204)
    assert not Resource.objects.filter(pk=todo_list.pk).exists()


@pytest.mark.django_db
@test_context
def test_links_stay_in_mount(todo_list, todo_owner_login):
    setup_admin_and_customer()
    make_list("Chores")
    Given.http.force_login(may_select_branches(todo_owner_login))
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
    # JSON is the API's, with the API's hrefs, the prefix's branch selected
    # or not.
    listed = get_json(
        reverse("todo-lists-list") + "?page_size=1", **{BRANCH_HEADER: "admin"}
    )
    assert path_of(listed["next"]) == reverse("todo-lists-list")
    assert [path_of(item["href"]) for item in listed["content"]] == [
        path_of(item["href"])
        for item in get_json(reverse("todo-lists-list") + "?page_size=1")["content"]
    ]
    assert all(
        path_of(item["href"]).startswith("/api/todo-lists/")
        for item in listed["content"]
    )
    # A create under the prefix answers with its frame in the mount, and its
    # Location there too (the UI's page of it under the prefix).
    When.http.post(
        mounted(ADMIN),
        data=json.dumps({"title": "New", "rbac_context": "project-a", "items": []}),
        content_type="application/json",
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(201)
    created = path_of(Http.response["Location"])
    assert created.startswith(ADMIN + "todo-lists/")
    assert hx_puts(html()) == [created]


@pytest.mark.django_db
@test_context
def test_working_branch_browsed_under_its_root_prefix(todo_list, todo_reader_login):
    setup_admin_and_customer()
    make_branch("testing-admin", "project-a", parent="admin")
    put_item("ui-components", "title", TITLE.format("h6"), "testing-admin")
    make_branch("orphan", "project-a", parent=None)
    Given.http.force_login(may_select_branches(todo_reader_login))
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
    assert COOKIE not in Http.response.cookies
    cookie = Http.response.cookies.get(mount_cookie(ADMIN))
    assert cookie is not None and cookie.value == "testing-admin"
    assert cookie["path"] == ADMIN
    # The banner's links (switcher, toggle, copy, back) stay in the mount.
    banner = re.search(r'<aside class="scrud-branch-banner".*?</aside>', page, re.S)
    links = re.findall(r'href="([^"]*)"', banner.group(0))
    assert links and all(path_of(h) == mounted(ADMIN, todo_list) for h in links)
    # ...not under customer's, or at the UI's own URLs (main's): the
    # published branch, with the banner's note.
    for url in (mounted(CUSTOMER, todo_list), ui_page(todo_list)):
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
    # A Warning, not an Error: it must not stop migrate (ruling R26).
    assert type(overlaps[0]) is checks.Warning
    assert overlaps[0].level == checks.WARNING
    assert prefix_errors(overlap=False) == []
    # A prefix and a type's slug that the project's URLconf already serves.
    publish("shop", "/shop/", parent="admin")
    shadowed = prefix_errors(overlap=False)
    assert len(shadowed) == 1, shadowed
    assert "/shop/todo-lists/" in shadowed[0].msg
    # A prefix under the API's URLs, where no prefix applies.
    publish("v2", "/api/v2/", parent="admin")
    assert any("/api/v2/" in m.msg for m in prefix_errors(overlap=False))


@pytest.mark.django_db
@test_context
def test_each_prefix_keeps_its_own_cookie(todo_list, todo_reader_login, admin_login):
    """A cookie set at the UI's own URLs (main's, scoped to ``/``) is sent to
    every prefix too: each prefix's own name keeps it from shadowing the
    prefix's (ruling R28)."""
    setup_admin_and_customer()
    make_branch("testing-admin", "project-a", parent="admin")
    put_item("ui-components", "title", TITLE.format("h6"), "testing-admin")
    make_branch("testing-customer", "project-a", parent="customer")
    put_item("ui-components", "title", TITLE.format("h5"), "testing-customer")
    make_branch("testing-main", "project-a", parent="main")
    put_item("ui-components", "title", TITLE.format("h4"), "testing-main")
    Given.http.force_login(may_select_branches(todo_reader_login))
    assert "<h6>Groceries" in choose_by_link(mounted(ADMIN, todo_list), "testing-admin")
    page = choose_by_link(mounted(CUSTOMER, todo_list), "testing-customer")
    assert "<h5>Groceries" in page
    cookie = Http.response.cookies[mount_cookie(CUSTOMER)]
    assert cookie.value == "testing-customer" and cookie["path"] == CUSTOMER
    page = choose_by_link(ui_page(todo_list), "testing-main")
    assert "<h4>Groceries" in page
    cookie = Http.response.cookies[COOKIE]
    assert cookie.value == "testing-main" and cookie["path"] == UI_BASE
    # The client sends every cookie everywhere, as a browser does for `/`.
    sent = Http.api_client.cookies
    assert sent[mount_cookie(ADMIN)].value == "testing-admin"
    assert sent[mount_cookie(CUSTOMER)].value == "testing-customer"
    assert sent[COOKIE].value == "testing-main"
    # Each reads its own, and none shows a refused branch.
    page = get_html(mounted(ADMIN, todo_list))
    assert "<h6>Groceries" in page and "isn't available" not in page
    assert "You are browsing branch <strong>testing-admin</strong>" in page
    page = get_html(mounted(CUSTOMER, todo_list))
    assert "<h5>Groceries" in page and "isn't available" not in page
    page = get_html(ui_page(todo_list))
    assert "<h4>Groceries" in page and "isn't available" not in page
    # The API's JSON reads none (the cookies still sent): JSON is not
    # affected by browsing.
    Given.http.force_login(admin_login)
    listed = json.dumps(get_json(reverse("ui-components-list")))
    assert "<h1>" in listed
    assert not any(f"<{tag}>" in listed for tag in ("h4", "h5", "h6"))


@pytest.mark.django_db
@test_context
def test_mounted_request_reads_branches_once(todo_list, todo_reader_login):
    """A page under a prefix costs one query over the UI's own page: the
    middleware's read of the branches, which the mount check reuses."""
    setup_admin_and_customer()
    publish("front", "/front/")  # no item of its own: main's presentation
    Given.http.force_login(may_select_branches(todo_reader_login))

    def queries(url):
        get_html(url)  # warm up
        with CaptureQueriesContext(connection) as captured:
            get_html(url)
        return len(captured)

    assert queries(mounted("/front/", todo_list)) == queries(ui_page(todo_list)) + 1


@pytest.mark.django_db
@test_context
def test_database_error_mounts_nothing(todo_list, monkeypatch, caplog):
    """A database error reading the branches leaves the request to the
    project's URLconf, with a warning: its other pages still answer."""
    from scrud_django.ui.branches import middleware

    setup_admin_and_customer()

    def unreachable(*args, **kwargs):
        raise OperationalError("the database is unreachable")

    monkeypatch.setattr(middleware, "stored_branches", unreachable)
    for path, status in (("/shop/todo-lists/", 200), (mounted(ADMIN, todo_list), 404)):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=middleware.__name__):
            When.http.get(path)
        Then.http.status_code_is(status)
        warnings = [r for r in caplog.records if r.name == middleware.__name__]
        assert len(warnings) == 1, warnings
        assert warnings[0].levelno == logging.WARNING
        assert "unreachable" in warnings[0].getMessage()


@pytest.mark.django_db
@test_context
def test_middleware_inert_without_ui(todo_list, todo_reader_login, monkeypatch):
    """Without scrud_django.ui installed, the pages would not check the
    mount: so nothing is mounted."""
    setup_admin_and_customer()
    Given.http.force_login(todo_reader_login)  # no GET on admin
    get_html(mounted(ADMIN, todo_list), status=403)
    monkeypatch.setattr(ui, "is_enabled", lambda: False)
    get_json(api_url(todo_list))
    When.http.get(mounted(ADMIN, todo_list))
    Then.http.status_code_is(404)


@pytest.mark.django_db
def test_a_mount_maps_api_and_ui_hrefs_into_its_prefix(todo_list):
    """`Mount.href` maps an API URL, or a UI page's that a prefix claims,
    into the prefix (any other as it is); `Mount.canonical` maps a URL under
    the prefix to the UI page it serves."""
    mount = Mount(ADMIN, "admin", UI_BASE, "/")
    item = mounted(ADMIN, todo_list)
    for href in (api_url(todo_list), ui_page(todo_list), item):
        assert mount.href(href) == item, href
        assert mount.href("http://testserver" + href + "?a=1") == (
            "http://testserver" + item + "?a=1"
        )
    assert (
        mount.href("/ui-css-files/site/abc.css") == ADMIN + "ui-css-files/site/abc.css"
    )
    for other in ("/studio/", "/shop/todo-lists/", "/nope/", "/todo-lists/x/y/"):
        assert mount.href(other) == other
    assert mount.canonical(item) == ui_page(todo_list)
    assert mount.canonical("/studio/") == "/studio/"


@pytest.mark.django_db
@test_context
def test_links_never_lead_into_a_branch_at_the_ui_base(todo_list, settings):
    """A branch stored at the UI's base is never mounted, so the studio's
    links into its tree stay unmapped (no `Mount`); a prefix elsewhere is
    mapped."""
    from scrud_django.ui.branches.effective import Selection
    from scrud_django.ui.branches.store import branches
    from scrud_django.ui.studio.links import studio_href
    from scrud_django.ui.studio.page import browse_url
    from scrud_django.ui.studio.preview import preview_css_urls

    setup_admin_and_customer()
    root = publish("root", UI_BASE)
    css = ResourceRegistration.register(
        {
            "name": "site",
            "css": "a { b: c }",
            "rbac_context": "project-a",
            "branch": "root",
        },
        "ui-css",
    )
    found = branches()
    assert mount_at(found["root"]) is None
    assert mount_at(found["admin"]) == Mount(ADMIN, "admin", UI_BASE, "/")
    lists = reverse("todo-lists-list")
    assert browse_url("todo-lists", found["root"], Selection("root")) == (
        f"{lists}?ui-branch=root"
    )
    assert browse_url("todo-lists", found["admin"], Selection("admin")) == (
        f"{ADMIN}todo-lists/?ui-branch=admin"
    )
    assert preview_css_urls(Selection("root")) == [
        f"/api/ui-css-files/site/{css.etag}.css?ui-branch=root"
    ]
    settings.SCRUD_UI_STUDIO_BRANCH = "root"
    assert studio_href() is None
    settings.SCRUD_UI_STUDIO_BRANCH = "admin"
    admin = Resource.objects.get(
        resource_type__slug="ui-branches", content__name="admin"
    )
    assert studio_href() == (
        f"{ADMIN}ui-branches/{admin.pk}/?ui-scope=studio&ui-mode=view"
    )
    assert root.content["prefix"] == UI_BASE


@pytest.mark.django_db
@test_context
def test_api_json_selects_only_in_mains_tree(admin_login):
    """JSON is only at the API's URLs, where a selection is in main's tree
    (as before prefixes stopped serving JSON): a published branch outside
    it is refused for a UI item list (ruling R40); a detail by key is the
    item whatever the selection."""
    setup_admin_and_customer()
    title = Resource.objects.get(content__name="title", content__branch="admin")
    Given.http.force_login(admin_login)
    get_json(reverse("ui-components-list"), status=400, **{BRANCH_HEADER: "admin"})
    assert Http.response.json()["type"].endswith("/ui-branch-unavailable")
    detail = reverse("ui-components-detail", args=[title.pk])
    found = get_json(detail, **{BRANCH_HEADER: "admin"})
    assert found["template"] == TITLE.format("h2")
    assert found == get_json(detail)
