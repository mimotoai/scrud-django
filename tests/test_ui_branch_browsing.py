"""Browsing a working branch: how a request chooses its branch (header,
parameter, cookie), the shared-link confirmation, the banner and the frames'
branch attribute."""

import json
import re
from html import unescape

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI
from scrud_django.ui.branches.selection import api_base

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_render import (
    BRANCH_HEADER,
    MAIN_RENDER_QUERIES,
    may_select_branches,
    put_item,
    setup_main_and_wip,
)
from .test_ui_branch_types import make_branch
from .test_ui_branch_versions import versions_of
from .test_ui_http import HTML, detail_url, html, make_stylesheet

COOKIE = "scrud_ui_branch"
BANNER = 'class="scrud-branch-banner"'
PAGE_BRANCH = '<div class="scrud-branch-page" data-scrud-branch="{}" hidden></div>'
CONFIRM = "You're about to browse branch"
MAIN_ROW = "<b>Buy milk</b>"
WIP_ROW = "<i>Buy milk</i>"
OTHER_ROW = "<u>Buy milk</u>"


def setup_branches(user):
    """main, `wip` and `other` (both in "project-a", over main), each with
    its own `row`; `user` may GET both branches."""
    setup_main_and_wip()
    make_branch("other", "project-a")
    put_item("ui-components", "row", "<u>{{ value.text }}</u>", "other")
    Given.http.force_authenticate(may_select_branches(user))


def get(todo_list, query="", **headers):
    url = detail_url(todo_list) + (f"?{query}" if query else "")
    When.http.get(url, HTTP_ACCEPT=HTML, **headers)
    Then.http.status_code_is(200)
    return html()


def with_branching_point(name):
    """Give branch `name` main's latest version as its branching point."""
    stored = Resource.objects.get(
        resource_type__type_uri=UI_BRANCH_TYPE_URI, content__name=name
    )
    point = {"main": versions_of("main")[-1]}
    stored.content = {**stored.content, "branching_point": point}
    stored.save()


def set_cookie(value):
    Http.api_client.cookies[COOKIE] = value


def response_cookie():
    return Http.response.cookies.get(COOKIE)


def get_href(href, **headers):
    """GET `href` as found (HTML-escaped) in a page, as a same-origin link
    unless `headers` say otherwise."""
    headers = {"HTTP_SEC_FETCH_SITE": "same-origin", **headers}
    When.http.get(unescape(href), HTTP_ACCEPT=HTML, **headers)
    Then.http.status_code_is(200)
    return html()


def href_of(page, css_class):
    found = re.search(rf'class="{css_class}" href="([^"]*)"', page)
    assert found, (css_class, page)
    return found.group(1)


def switch_hrefs(banner):
    """``{branch: href}`` of the switcher's links (the current branch has
    none: ``None``)."""
    links = re.findall(
        r'<li>(?:<a href="([^"]*)">([^<]*)</a>|<strong aria-current="page">([^<]*)<)',
        banner,
    )
    return {name or current: href or None for href, name, current in links}


def banner_of(page):
    found = re.search(r'<aside class="scrud-branch-banner".*?</aside>', page, re.S)
    return found.group(0) if found else ""


@pytest.mark.django_db
@test_context
def test_precedence_header_param_cookie(todo_list, todo_reader_login):
    setup_branches(todo_reader_login)
    fragment = {"HTTP_HX_REQUEST": "true"}
    # none: main
    assert MAIN_ROW in get(todo_list, **fragment)
    # the cookie
    set_cookie("wip")
    assert WIP_ROW in get(todo_list, **fragment)
    # the parameter over the cookie
    assert OTHER_ROW in get(todo_list, "ui-branch=other", **fragment)
    # the header over the parameter and the cookie
    page = get(todo_list, "ui-branch=other", **fragment, **{BRANCH_HEADER: "main"})
    assert MAIN_ROW in page
    page = get(todo_list, "ui-branch=main", **fragment, **{BRANCH_HEADER: "other"})
    assert OTHER_ROW in page
    # an empty parameter or header names the published branch
    assert MAIN_ROW in get(todo_list, "ui-branch=", **fragment)
    assert MAIN_ROW in get(todo_list, **fragment, **{BRANCH_HEADER: ""})
    # the cookie again, with neither
    assert WIP_ROW in get(todo_list, **fragment)


@pytest.mark.django_db
@test_context
def test_json_lists_ignore_the_cookie(admin_login):
    """JSON is not affected by browsing: the cookie selects nothing there, an
    explicit header or parameter still does."""
    setup_main_and_wip()
    Given.http.force_authenticate(admin_login)
    set_cookie("wip")

    def branches_listed(**extra):
        When.http.get(reverse("ui-components-list"), **extra)
        Then.http.status_code_is(200)
        return {
            (item["content"]["name"], item["content"].get("branch", "main"))
            for item in Http.response.json()["content"]
        }

    assert ("row", "wip") not in branches_listed()
    assert ("row", "wip") in branches_listed(**{BRANCH_HEADER: "wip"})
    assert ("row", "wip") in branches_listed(QUERY_STRING="ui-branch=wip")
    # Asked for as HTML, the list is the browsed branch's.
    When.http.get(reverse("ui-components-list"), HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    assert "&lt;i&gt;" in html() and BANNER in html()


@pytest.mark.django_db
@test_context
def test_param_from_shared_link_asks_first_then_sets_cookie(
    todo_list, todo_reader_login
):
    setup_branches(todo_reader_login)
    url = detail_url(todo_list)
    # A shared link: the confirmation page, with nothing of the branch.
    page = get(todo_list, "ui-branch=wip")
    assert CONFIRM in page and "wip" in page
    assert WIP_ROW not in page and MAIN_ROW not in page
    assert response_cookie() is None
    continue_href = href_of(page, "scrud-branch-continue")
    assert re.fullmatch(
        rf"{url}\?ui-branch=wip&amp;ui-branch-confirm=[0-9a-f]+", continue_href
    )
    assert href_of(page, "scrud-branch-cancel") == url
    # A fragment request is never asked: it renders the branch, no cookie.
    assert WIP_ROW in get(todo_list, "ui-branch=wip", HTTP_HX_REQUEST="true")
    assert response_cookie() is None
    # A link planted in content (same-origin) with a made-up confirmation is
    # still asked, and sets no cookie.
    for planted in ("1", "", "0" * 40):
        page = get(
            todo_list,
            f"ui-branch=wip&ui-branch-confirm={planted}",
            HTTP_SEC_FETCH_SITE="same-origin",
        )
        assert CONFIRM in page and response_cookie() is None, planted
    # A token minted for another value is refused.
    other_href = href_of(get(todo_list, "ui-branch=other"), "scrud-branch-continue")
    other_token = re.search(r"ui-branch-confirm=([0-9a-f]+)", other_href).group(1)
    page = get_href(f"{url}?ui-branch=wip&amp;ui-branch-confirm={other_token}")
    assert CONFIRM in page and response_cookie() is None
    # The real token from a cross-site link is still asked.
    page = get_href(continue_href, HTTP_SEC_FETCH_SITE="cross-site")
    assert CONFIRM in page and response_cookie() is None
    # A token minted for another viewer (another CSRF secret) is refused.
    secret = Http.api_client.cookies["csrftoken"].value
    Http.api_client.cookies["csrftoken"] = "x" * len(secret)
    page = get_href(continue_href)
    assert CONFIRM in page and response_cookie() is None
    Http.api_client.cookies["csrftoken"] = secret
    # Continue: the branch, and the cookie is set.
    page = get_href(continue_href)
    assert WIP_ROW in page and CONFIRM not in page
    cookie = response_cookie()
    assert cookie is not None and cookie.value == "wip"
    assert cookie["path"] == api_base() and cookie["httponly"]
    assert cookie["samesite"] == "Lax"
    # A later plain GET stays on the branch (the cookie).
    page = get(todo_list)
    assert WIP_ROW in page and BANNER in page and response_cookie() is None
    back_href = href_of(page, "scrud-branch-back")
    # A link to the branch you are on asks nothing.
    page = get(todo_list, "ui-branch=wip")
    assert WIP_ROW in page and CONFIRM not in page and response_cookie() is None
    # A link to another working branch asks again.
    assert CONFIRM in get(todo_list, "ui-branch=other")
    # A link to the published branch asks nothing, and keeps the cookie; its
    # frames and the page name the published branch, so its fragments stay
    # there.
    page = get(todo_list, "ui-branch=")
    assert MAIN_ROW in page and CONFIRM not in page and BANNER not in page
    assert response_cookie() is None
    assert 'class="scrud-resource"' in page and PAGE_BRANCH.format("") in page
    assert re.search(r'class="scrud-resource"[^>]*data-scrud-branch=""', page)
    # Back to main (the banner's link): the cookie is deleted.
    page = get_href(back_href)
    assert MAIN_ROW in page and BANNER not in page
    cookie = response_cookie()
    assert cookie is not None and cookie.value == "" and cookie["max-age"] == 0
    assert cookie["path"] == api_base()


@pytest.mark.django_db
@test_context
def test_banner_only_on_working_branch(todo_list, todo_reader_login):
    setup_branches(todo_reader_login)
    make_branch("secret", "project-b")  # not GET-able
    make_branch("orphan", "project-a", parent="gone")  # broken chain
    with_branching_point("wip")
    page = get(todo_list)
    assert BANNER not in page and "data-scrud-branch" not in page
    page = get(todo_list, "page=2", **{BRANCH_HEADER: "wip"})
    banner = banner_of(page)
    assert banner and page.index(BANNER) < page.index('class="scrud-resource"')
    assert "wip" in banner and "live" in banner
    url = detail_url(todo_list)
    token = "&amp;ui-branch-confirm=[0-9a-f]+"
    # the switcher: a link to each branch the viewer may GET in this tree
    switch = switch_hrefs(banner)
    assert set(switch) == {"wip", "other"} and switch["wip"] is None
    assert re.fullmatch(rf"{url}\?page=2&amp;ui-branch=other{token}", switch["other"])
    # the live/pinned toggle, the copy link and the way back
    toggle = href_of(banner, "scrud-branch-toggle")
    assert re.fullmatch(rf"{url}\?page=2&amp;ui-branch=wip%40pinned{token}", toggle)
    assert href_of(banner, "scrud-branch-copy") == f"{url}?page=2&amp;ui-branch=wip"
    back = href_of(banner, "scrud-branch-back")
    assert re.fullmatch(rf"{url}\?page=2&amp;ui-branch={token}", back)
    assert "Back to main" in banner
    # the page and its frames carry the selection; fragments get no banner
    assert PAGE_BRANCH.format("wip") in page
    assert page.index(PAGE_BRANCH.format("wip")) < page.index(BANNER)
    assert 'data-scrud-branch="wip"' in page
    page = get(todo_list, HTTP_HX_REQUEST="true", **{BRANCH_HEADER: "wip"})
    assert BANNER not in page and 'data-scrud-branch="wip"' in page
    # pinned: its mode names the version
    page = get(todo_list, **{BRANCH_HEADER: "wip@pinned"})
    assert f"pinned at main v{versions_of('main')[-1]}" in banner_of(page)
    assert 'data-scrud-branch="wip@pinned"' in page
    toggle = href_of(banner_of(page), "scrud-branch-toggle")
    assert re.fullmatch(rf"{url}\?ui-branch=wip{token}", toggle)
    # the switcher's and the toggle's links switch, with no confirmation
    page = get_href(switch["other"])
    assert OTHER_ROW in page and response_cookie().value == "other"
    page = get_href(toggle)
    assert WIP_ROW in page and response_cookie().value == "wip"


@pytest.mark.django_db
@test_context
def test_banner_not_hideable_by_rules(todo_list, todo_reader_login):
    setup_branches(todo_reader_login)
    make_stylesheet("pages", [("scrud:page", "my-page")])
    # A page component of its own on the branch, which renders the body.
    put_item(
        "ui-components",
        "my-page",
        '<html><body id="mine">{{ body }}</body></html>',
        "wip",
        context="project-a",
    )
    page = get(todo_list, **{BRANCH_HEADER: "wip"})
    assert page.startswith('<html><body id="mine">')
    assert page.index('id="mine"') < page.index(BANNER) < page.index(WIP_ROW)
    # One that omits the body (and so the banner): the built-in page is used.
    stored = Resource.objects.get(content__name="my-page", content__branch="wip")
    stored.content = {**stored.content, "template": "<html><body>gone</body></html>"}
    stored.save()
    page = get(todo_list, **{BRANCH_HEADER: "wip"})
    assert "gone" not in page and BANNER in page and WIP_ROW in page
    # Said as what it is, not as a failure to render.
    assert page.startswith(
        '<!-- scrud-ui: page component "my-page" leaves out the branch banner'
        " or the safe mode notice -->"
    )
    assert "failed" not in page


@pytest.mark.django_db
@test_context
def test_refused_branch_falls_back_with_note(todo_list, todo_reader_login):
    setup_branches(todo_reader_login)
    make_branch("secret", "project-b")
    for refused in ("secret", "nope"):
        set_cookie(refused)
        page = get(todo_list)
        assert MAIN_ROW in page
        banner = banner_of(page)
        assert f"The branch <code>{refused}</code> isn't available" in banner
        # the switcher is still there, and the way back clears the cookie
        assert set(switch_hrefs(banner)) == {"wip", "other"}
        assert "ui-branch=&amp;ui-branch-confirm=" in href_of(
            banner, "scrud-branch-back"
        )
        assert PAGE_BRANCH.format("") in page
        # the cookie is a preference: kept
        assert response_cookie() is None
    # A broken chain is refused with the note too.
    make_branch("child", "project-a", parent="wip")
    Resource.objects.filter(
        resource_type__type_uri=UI_BRANCH_TYPE_URI, content__name="wip"
    ).delete()
    set_cookie("child")
    assert "isn't available" in banner_of(get(todo_list))


@pytest.mark.django_db
@pytest.mark.parametrize(
    "value",
    ["../x", "x@pinned@", "w" * 200, "wip\x00", "Wip", "wip@pinned@pinned"],
)
@test_context
def test_odd_selection_values_are_ignored(todo_list, todo_reader_login, value):
    setup_branches(todo_reader_login)
    # As a header or a cookie: ignored, so the cookie or main is used.
    set_cookie("wip")
    page = get(todo_list, HTTP_HX_REQUEST="true", **{BRANCH_HEADER: value})
    assert WIP_ROW in page
    del Http.api_client.cookies[COOKIE]
    page = get(todo_list, HTTP_HX_REQUEST="true", **{BRANCH_HEADER: value})
    assert MAIN_ROW in page
    set_cookie(value)
    page = get(todo_list)
    assert MAIN_ROW in page and BANNER not in page
    # As a parameter: ignored, no confirmation, no note.
    page = get(todo_list, "ui-branch=" + value.replace("\x00", "%00"))
    assert MAIN_ROW in page and CONFIRM not in page and BANNER not in page


@pytest.mark.django_db
@test_context
def test_vary_names_branch_header_and_cookie(todo_list, todo_reader_login):
    Given.http.force_authenticate(todo_reader_login)
    get(todo_list)
    vary = {v.strip().lower() for v in Http.response["Vary"].split(",")}
    assert {
        "accept",
        "hx-request",
        "scrud-ui-scope",
        "scrud-ui-pointer",
        "scrud-ui-branch",
        "cookie",
    } <= vary


@pytest.mark.django_db
@test_context
def test_branch_names_and_links_are_escaped(todo_list, todo_reader_login):
    setup_branches(todo_reader_login)
    page = get(todo_list, 'a="><script>x</script>', **{BRANCH_HEADER: "wip"})
    assert "<script>x</script>" not in page
    assert "%3Cscript%3E" in banner_of(page)


@pytest.mark.django_db
@test_context
def test_selecting_main_reads_no_branch(todo_list, todo_reader_login):
    """Ruling R1: a cookie, parameter or header naming main costs nothing."""
    setup_branches(todo_reader_login)
    get(todo_list)
    for query, headers, cookie in (
        ("", {}, "main"),
        ("ui-branch=main&ui-branch-confirm=1", {}, None),
        ("ui-branch=", {BRANCH_HEADER: "main"}, None),
    ):
        if cookie:
            set_cookie(cookie)
        else:
            Http.api_client.cookies.pop(COOKIE, None)
        with CaptureQueriesContext(connection) as queries:
            assert MAIN_ROW in get(todo_list, query, **headers)
        assert len(queries) == MAIN_RENDER_QUERIES, [q["sql"] for q in queries]


@pytest.mark.django_db
@test_context
def test_reload_after_a_stale_write_stays_on_the_branch(todo_list, todo_owner_login):
    setup_main_and_wip()
    Given.http.force_authenticate(may_select_branches(todo_owner_login))
    When.http.put(
        detail_url(todo_list),
        data=json.dumps({**todo_list.content, "title": "Too late"}),
        content_type="application/json",
        HTTP_IF_MATCH='"stale"',
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
        **{BRANCH_HEADER: "wip"},
    )
    Then.http.status_code_is(412)
    headers = re.search(r'hx-headers="([^"]*)"', html()).group(1)
    assert json.loads(headers.replace("&quot;", '"')) == {
        "Scrud-UI-Scope": "",
        "Scrud-UI-Pointer": "",
        "Scrud-UI-Branch": "wip",
    }
