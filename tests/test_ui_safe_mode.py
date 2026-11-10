"""Safe mode (``?ui-safe=1``): a page rendered from the packaged layers,
built-ins and the default stylesheet alone, whatever is stored, so that a
branch whose stored UI breaks rendering can still be reached. Every gate stays:
`check_mount` and the refusal of a branch the viewer may not GET. These tests
serve the API at ``/api/`` (``tests.mounted_urls``)."""

import re
from html import unescape
from types import SimpleNamespace

import pytest
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.ui import renderer
from scrud_django.ui.branches.chrome import SAFE_NOTICE

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_css import css_hrefs, get_css, make_css
from .test_ui_branch_js import get_js, make_js
from .test_ui_branch_prefixes import (
    ADMIN,
    api_url,
    get_html,
    get_json,
    mounted,
    setup_admin_and_customer,
)
from .test_ui_branch_render import BRANCH_HEADER, may_select_branches, put_item
from .test_ui_branch_types import make_branch, problem
from .test_ui_http import make_component, make_stylesheet
from .test_ui_layers import MORE_APP, MORE_LIST, OVERRIDE, installed

SAFE = "?ui-safe=1"
NOTICE = (
    '<div class="scrud-safe-mode" role="status">'
    "Safe mode: this page ignores stored UI items.</div>"
)


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def setup_stored(todo_reader_login):
    """A stored ``<h1>`` title on main and an ``<h2>`` one on ``wip``."""
    make_component("title", "<h1>{{ value.title }}</h1>")
    make_stylesheet("s", [("ex:TodoList", "title")])
    make_branch("wip", "project-a")
    put_item("ui-components", "title", "<h2>{{ value.title }}</h2>", "wip")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))


def test_notice_is_the_documented_markup():
    assert str(SAFE_NOTICE) == NOTICE


@pytest.mark.django_db
@test_context
def test_safe_mode_ignores_stored_items_on_every_branch(todo_list, todo_reader_login):
    setup_stored(todo_reader_login)
    url = api_url(todo_list)
    assert "<h1>Groceries" in get_html(url)
    assert "<h2>Groceries" in get_html(url, **{BRANCH_HEADER: "wip"})
    for headers in ({}, {BRANCH_HEADER: "wip"}):
        page = get_html(url + SAFE, **headers)
        assert NOTICE in page
        assert "<h2>Groceries" not in page and "<h1>Groceries" not in page
        assert "Groceries" in page  # the built-in rendering of the resource
    # Any other value is ignored.
    assert "<h2>Groceries" in get_html(url + "?ui-safe=0", **{BRANCH_HEADER: "wip"})
    assert NOTICE not in get_html(url + "?ui-safe=yes")
    assert NOTICE not in get_html(url)
    # Only a page has the notice: a fragment is the built-in's alone.
    When.http.get(
        url + SAFE,
        HTTP_ACCEPT="text/html",
        HTTP_HX_REQUEST="true",
        **{BRANCH_HEADER: "wip"},
    )
    Then.http.status_code_is(200)
    assert NOTICE not in html_of_response()
    assert "<h2>Groceries" not in html_of_response()


def html_of_response():
    return Http.response.content.decode()


@pytest.mark.django_db
@test_context
def test_safe_mode_uses_layer_items(todo_list, todo_reader_login):
    with installed(MORE_APP):
        Given.http.force_authenticate(todo_reader_login)
        make_component("layermore:list", '<p class="override">{{ value.title }}</p>')
        url = api_url(todo_list) + "?ui-scope=layertest"
        assert OVERRIDE in get_html(url)
        page = get_html(url + "&ui-safe=1")
        assert MORE_LIST in page and OVERRIDE not in page and NOTICE in page


@pytest.mark.django_db
@test_context
def test_safe_mode_keeps_mount_gate(todo_list, todo_reader_login):
    setup_admin_and_customer()
    Given.http.force_authenticate(todo_reader_login)
    refused = get_json(mounted(ADMIN, todo_list), status=403)
    for url in (mounted(ADMIN, todo_list), mounted(ADMIN)):
        page = get_html(url + SAFE, status=403)
        assert "Groceries" not in page and "permission" in page
        assert get_json(url + SAFE, status=403) == refused
    # The files of a refused prefix too.
    css = make_css("site")
    get_css(f"{ADMIN}ui-css-files/site/{css.etag}.css{SAFE}", status=403)
    # Anonymous: refused, not rendered.
    Given.http.force_authenticate(None)
    When.http.get(mounted(ADMIN, todo_list) + SAFE, HTTP_ACCEPT="text/html")
    assert Http.response.status_code in (401, 403)


@pytest.mark.django_db
@test_context
def test_safe_mode_keeps_branch_refusal_for_json(ui_author_login):
    make_branch("hidden", "bob")
    from .test_ui_branch_authz import MAIN_GET
    from .test_ui_http import grant

    grant(ui_author_login, MAIN_GET, "project-a")
    Given.http.force_authenticate(ui_author_login)
    for how in (
        {BRANCH_HEADER: "hidden"},
        {"QUERY_STRING": "ui-branch=hidden&ui-safe=1"},
    ):
        When.http.get("/api/ui-components/", HTTP_ACCEPT="application/json", **how)
        Then.http.status_code_is(400)
        assert problem()["type"].endswith("/ui-branch-unavailable")
    # An HTML page does not render the branch either: the published one is
    # used, and the banner says so, beside the notice.
    page = get_html("/api/ui-components/" + SAFE + "&ui-branch=hidden")
    assert "isn't available" in page and NOTICE in page
    assert "bob" not in page.split("isn't available")[0][-200:]


@pytest.mark.django_db
@test_context
def test_safe_mode_notice_present_and_required(
    todo_list, todo_reader_login, monkeypatch
):
    Given.http.force_authenticate(todo_reader_login)
    url = api_url(todo_list) + SAFE
    page = get_html(url)
    assert page.count(NOTICE) == 1
    assert page.index(NOTICE) < page.index("Groceries")
    # A page component that leaves the notice out is replaced by the built-in.
    leaves_out = SimpleNamespace(
        name="layertest:page",
        compiled=lambda: SimpleNamespace(render=lambda ctx: "<main>bare</main>"),
    )
    monkeypatch.setattr(renderer, "resolve_component", lambda *a, **k: leaves_out)
    page = get_html(url)
    assert 'scrud-ui: component "layertest:page" failed' in page
    assert "<main>bare</main>" not in page and NOTICE in page
    # One that keeps the body (which holds the chrome) is used.
    keeps = SimpleNamespace(
        name="layertest:page",
        compiled=lambda: SimpleNamespace(
            render=lambda ctx: "<main>kept</main>" + ctx["body"]
        ),
    )
    monkeypatch.setattr(renderer, "resolve_component", lambda *a, **k: keeps)
    page = get_html(url)
    assert "<main>kept</main>" in page and NOTICE in page


@pytest.mark.django_db
@test_context
def test_safe_mode_css_and_js_are_layer_only(todo_list, todo_reader_login):
    with installed(MORE_APP):
        Given.http.force_authenticate(todo_reader_login)
        site = make_css("site", "h1 { color: red }")
        script = make_js("site", "window.stored = 1;")
        url = api_url(todo_list)
        normal = css_hrefs(get_html(url))
        assert f"/api/ui-css-files/site/{site.etag}.css" in normal
        page = get_html(url + SAFE)
        [css] = css_hrefs(page)
        assert css.startswith("/api/ui-css-files/layermore:theme/")
        assert css.endswith(".css" + SAFE)
        scripts = [
            unescape(s) for s in re.findall(r'<script src="([^"]*ui-js[^"]*)"', page)
        ]
        [js] = scripts
        assert js.startswith("/api/ui-js-files/layermore:app/")
        assert js.endswith(".js" + SAFE)
        assert get_css(css).content == b".layermore { color: teal; }"
        assert get_js(js).content == b"window.layermore = true;"
        # The stored item's URL is not served in safe mode, only without it.
        stored_css = f"/api/ui-css-files/site/{site.etag}.css"
        stored_js = f"/api/ui-js-files/site/{script.etag}.js"
        get_css(stored_css + SAFE, status=404)
        get_js(stored_js + SAFE, status=404)
        assert get_css(stored_css).content == b"h1 { color: red }"
        assert get_js(stored_js).content == b"window.stored = 1;"
