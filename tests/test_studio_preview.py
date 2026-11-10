"""The studio's previews: a type's sample, or one of its instances, rendered
on a branch, in a mode, scope chain and data context, with no write
attributes and each node marked with its JSON Pointer; and the sample as
JSON. These tests serve the API at ``/api/`` and the studio at ``/studio/``
(``tests.mounted_urls``)."""

import re

import pytest
from django.test import RequestFactory
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.ui.branches.effective import Selection
from scrud_django.ui.branches.selection import choose, parse_value, selectable

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_branch_css import css_hrefs, file_url, make_css
from .test_ui_branch_render import (
    BRANCH_HEADER,
    may_select_branches,
    setup_main_and_wip,
)
from .test_ui_branch_types import make_branch, problem
from .test_ui_http import html, make_component, make_stylesheet

SLUG = "todo-lists"
UNAVAILABLE = "https://api.openteams.com/http-problem/ui-branch-unavailable"


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def preview(status=200, slug=SLUG, **params):
    reverse("todo-lists-list")  # the URLconf registers the types
    When.http.get(reverse("scrud-studio:preview", args=[slug]), params)
    Then.http.status_code_is(status)
    return html()


def branch_css(page):
    """The page's CSS links but the studio's own (the last)."""
    hrefs = css_hrefs(page)
    assert hrefs[-1] == "/static/scrud_studio/preview.css"
    return hrefs[:-1]


def frame_tag(page):
    """The opening tag of the preview's resource frame."""
    return re.search(r'<[a-z]+ class="scrud-resource"[^>]*>', page).group(0)


@pytest.mark.django_db
@test_context
def test_preview_flag_drops_write_attributes_and_marks_nodes(login_no_roles):
    Given.http.force_authenticate(login_no_roles)
    page = preview(mode="edit")
    assert '<scrud-node data-pointer="/items/0/text">' in page
    assert '<scrud-node data-pointer="">' in page
    for attribute in ("hx-put", "hx-ext", "hx-trigger", "hx-delete"):
        assert attribute not in page
    assert "<form" not in page and ">Delete</button>" not in page
    assert frame_tag(page).startswith("<div ")
    assert "data-scrud-branch" not in frame_tag(page)
    # Still the edit components: inputs, with the sample's values.
    assert '<textarea id="' in page and ">\nLorem ipsum</textarea>" in page
    assert '<input type="checkbox"' in page
    # A full page for the iframe: the static CSS that hides the markers'
    # boxes, no script, and framable by the studio only.
    assert '<style' not in page
    assert "scrud_studio/preview.css" in page
    assert not re.search(r"<script(?![^>]*\stype=\"application/json\")", page)
    assert Http.response["X-Frame-Options"] == "SAMEORIGIN"
    assert Http.response["Content-Type"] == "text/html; charset=utf-8"


@pytest.mark.django_db
@test_context
def test_preview_defaults_to_view_mode(login_no_roles):
    Given.http.force_authenticate(login_no_roles)
    page = preview()
    assert "<input" not in page and "<textarea" not in page
    assert '<scrud-node data-pointer="/items/0/text">' in page
    preview(400, mode="other")


@pytest.mark.django_db
@test_context
def test_preview_and_sample_need_login():
    reverse("todo-lists-list")
    When.http.get(reverse("scrud-studio:preview", args=[SLUG]))
    Then.http.status_code_is(401)
    When.http.get(reverse("scrud-studio:sample", args=[SLUG]))
    Then.http.status_code_is(401)


@pytest.mark.django_db
@test_context
def test_sample_endpoint_returns_the_sample(login_no_roles):
    reverse("todo-lists-list")
    Given.http.force_authenticate(login_no_roles)
    url = reverse("scrud-studio:sample", args=[SLUG])
    When.http.get(url)
    Then.http.status_code_is(200)
    assert Http.response.json() == {
        "title": "Lorem ipsum",
        "rbac_context": "",
        "items": [{"text": "Lorem ipsum", "done": False}],
    }
    When.http.get(url, {"context": "acme"})
    assert Http.response.json()["rbac_context"] == "acme"
    When.http.get(reverse("scrud-studio:sample", args=["no-such-type"]))
    Then.http.status_code_is(404)
    preview(404, slug="no-such-type")


@pytest.mark.django_db
@test_context
def test_sample_preview_is_never_stored(login_no_roles, todo_list, todo_reader_login):
    reverse("todo-lists-list")
    before = Resource.objects.count()
    Given.http.force_authenticate(login_no_roles)
    for mode in ("view", "edit"):
        preview(mode=mode)
        preview(mode=mode, context="acme")
    When.http.get(reverse("scrud-studio:sample", args=[SLUG]))
    Then.http.status_code_is(200)
    Given.http.force_authenticate(todo_reader_login)
    preview(mode="edit", instance=todo_list.pk)
    assert Resource.objects.count() == before
    todo_list.refresh_from_db()
    assert todo_list.content["title"] == "Groceries"


@pytest.mark.django_db
@test_context
def test_preview_uses_the_branch_and_its_css(todo_reader_login):
    setup_main_and_wip()
    main_css = make_css("site", "p { color: red }")
    wip_css = make_css("site", "p { color: green }", branch="wip", context="project-a")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    page = preview(branch="wip")
    assert "<i>Lorem ipsum</i>" in page and "<b>Lorem ipsum</b>" not in page
    assert branch_css(page) == [file_url(wip_css) + "?ui-branch=wip"]
    # The link serves the branch's CSS, whatever branch a cookie browses.
    Http.api_client.cookies["scrud_ui_branch"] = "main"
    When.http.get(branch_css(page)[0])
    Then.http.status_code_is(200)
    assert Http.response.content == b"p { color: green }"
    # main, by name or by default.
    for params in ({"branch": "main"}, {}):
        page = preview(**params)
        assert "<b>Lorem ipsum</b>" in page and "<i>Lorem ipsum</i>" not in page
        assert branch_css(page) == [file_url(main_css) + "?ui-branch=main"]


@pytest.mark.django_db
@test_context
def test_preview_of_a_published_branch_links_its_css_under_its_prefix(
    todo_reader_login,
):
    make_branch("shop", "project-a", prefix="/shop/")
    shop_css = make_css("site", "p { color: blue }", branch="shop", context="project-a")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    page = preview(branch="shop")
    assert branch_css(page) == [file_url(shop_css, "/shop/") + "?ui-branch=shop"]
    When.http.get(branch_css(page)[0])
    Then.http.status_code_is(200)
    assert Http.response.content == b"p { color: blue }"


@pytest.mark.django_db
@test_context
def test_preview_refuses_an_unselectable_branch(todo_reader_login):
    setup_main_and_wip()
    make_branch("orphan", "project-a", parent="gone")
    # The reader may not GET wip: refused as an unknown branch is.
    Given.http.force_authenticate(todo_reader_login)
    preview(400, branch="wip")
    unreadable = problem()
    preview(400, branch="nope")
    unknown = problem()
    assert unreadable["type"] == unknown["type"] == UNAVAILABLE
    assert unreadable["title"] == unknown["title"]
    assert unreadable["detail"].replace("'wip'", "'nope'") == unknown["detail"]
    for value in ("orphan", "Not A Branch!", "wip@pinned"):
        preview(400, branch=value)
        assert problem()["type"] == UNAVAILABLE
    # Readable, it previews.
    may_select_branches(todo_reader_login)
    assert "<i>Lorem ipsum</i>" in preview(branch="wip")


@pytest.mark.django_db
@test_context
def test_preview_of_a_real_instance_needs_read(
    todo_list, todo_reader_login, login_no_roles
):
    detail = reverse("todo-lists-detail", args=[todo_list.pk])
    Given.http.force_authenticate(login_no_roles)
    When.http.get(detail, HTTP_ACCEPT="application/json")
    refused = Http.response.status_code
    assert refused in (403, 404)
    preview(refused, instance=todo_list.pk)
    When.http.get(reverse("todo-lists-detail", args=[999999]))
    missing = Http.response.status_code
    preview(missing, instance=999999)
    Given.http.force_authenticate(todo_reader_login)
    preview(404, instance=999999)
    preview(404, instance="not-a-pk")
    page = preview(instance=todo_list.pk)
    assert "Groceries" in page and "Buy eggs" in page
    assert '<scrud-node data-pointer="/items/1/text">' in page
    assert "hx-put" not in page and "<form" not in page
    page = preview(instance=todo_list.pk, mode="edit")
    assert ">\nBuy eggs</textarea>" in page and "hx-put" not in page


@pytest.mark.django_db
@test_context
def test_preview_data_context_selects_context_stylesheets(login_no_roles):
    make_component("acme-row", '<i class="acme">{{ value.text }}</i>')
    make_stylesheet("acme", [("ex:TodoItem", "acme-row")], context="acme")
    Given.http.force_authenticate(login_no_roles)
    assert '<i class="acme">Lorem ipsum</i>' in preview(context="acme")
    assert 'class="acme"' not in preview()
    assert 'class="acme"' not in preview(context="other")


@pytest.mark.django_db
@test_context
def test_preview_scope_chain(login_no_roles):
    make_component("scoped-row", '<u>{{ value.text }}</u>')
    make_stylesheet("s", [("compact ex:TodoItem", "scoped-row")])
    Given.http.force_authenticate(login_no_roles)
    assert "<u>Lorem ipsum</u>" in preview(scope="compact")
    assert "<u>Lorem ipsum</u>" not in preview()


@pytest.mark.django_db
def test_selectable_matches_choose_for_api_urls(todo_reader_login, login_no_roles):
    make_branch("wip", "project-a")
    make_branch("orphan", "project-a", parent="gone")
    may_select_branches(todo_reader_login)
    expected = {
        todo_reader_login: {"main", "wip"},
        login_no_roles: {"main"},
    }
    for user, allowed in expected.items():
        for value in ("main", "wip", "wip@pinned", "nope", "orphan"):
            request = RequestFactory().get(
                reverse("todo-lists-list"), **{BRANCH_HEADER: value}
            )
            request.user = user
            chosen = choose(request)
            by_choose = chosen.refused is None and chosen.selection == parse_value(
                value
            )
            assert selectable(request, parse_value(value)) == by_choose
            assert by_choose == (value in allowed), (user, value)
    request = RequestFactory().get("/")
    request.user = login_no_roles
    assert selectable(request, Selection())
