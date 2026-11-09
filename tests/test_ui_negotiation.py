"""Content negotiation with ``scrud_django.ui`` installed, and the unchanged
behaviour of the API without it."""

import importlib
import json

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import clear_url_caches, reverse
from rest_framework.renderers import BrowsableAPIRenderer, JSONRenderer
from rest_framework.settings import api_settings
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.ui.renderer import ScrudHTMLRenderer, ui_renderers
from scrud_django.views import ResourceViewSet

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401

BROWSER = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8"
)
JSON = "application/json"
HTML = "text/html; charset=utf-8"


def url_of(todo_list):
    return reverse("todo-lists-detail", args=[todo_list.pk])


def without_ui_app():
    installed = [app for app in settings.INSTALLED_APPS if app != "scrud_django.ui"]
    return override_settings(INSTALLED_APPS=installed)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "accept,query,expected",
    [
        (None, "", JSON),
        ("*/*", "", JSON),
        (BROWSER, "", HTML),
        ("text/html", "", HTML),
        ("application/json;q=0.9,text/html;q=0.8", "", JSON),
        # DRF ignores q-values: the first renderer (JSON) that matches any
        # listed type wins, so a higher q for HTML does not select HTML.
        ("text/html;q=0.9,application/json;q=0.8", "", JSON),
        (None, "?format=json", JSON),
        ("*/*", "?format=json", JSON),
        ("*/*", "?format=api", HTML),
        ("text/html", "?format=api", HTML),
        ("application/xml", "", 406),
        ("application/xml", "?format=api", 406),
    ],
)
@test_context
def test_negotiation_with_the_ui_app(
    todo_list, todo_owner_login, accept, query, expected
):
    Given.http.force_authenticate(todo_owner_login)
    headers = {"HTTP_ACCEPT": accept} if accept else {}
    When.http.get(url_of(todo_list) + query, **headers)
    body = Http.response.content.decode()
    if expected == 406:
        Then.http.status_code_is(406)
        assert Http.response["Content-Type"] == "application/problem+json"
        assert "scrud-notice" not in body and "Groceries" not in body
        return
    Then.http.status_code_is(200)
    assert Http.response["Content-Type"].startswith(expected.split(";")[0])
    if expected == JSON:
        assert json.loads(body) == todo_list.content
    elif query == "?format=api":
        # DRF's browsable API, not the scrud UI
        assert "Django REST framework" in body
        assert 'class="scrud-resource"' not in body
    else:
        assert 'class="scrud-resource"' in body
        assert "Django REST framework" not in body


@pytest.mark.django_db
@test_context
def test_html_format_query_selects_the_ui(todo_list, todo_owner_login):
    Given.http.force_authenticate(todo_owner_login)
    When.http.get(url_of(todo_list) + "?format=html", HTTP_ACCEPT="*/*")
    Then.http.status_code_is(200)
    assert 'class="scrud-resource"' in Http.response.content.decode()


def test_renderers_without_the_ui_app_are_drf_s():
    drf = [cls for cls in api_settings.DEFAULT_RENDERER_CLASSES]
    assert [type(r) for r in ResourceViewSet().get_renderers()] == [
        JSONRenderer,
        ScrudHTMLRenderer,
        BrowsableAPIRenderer,
    ]
    with without_ui_app():
        assert [type(r) for r in ResourceViewSet().get_renderers()] == drf


@pytest.mark.django_db
@test_context
def test_errors_with_a_html_accept_are_unchanged_without_the_ui_app(
    todo_list, todo_reader_login
):
    import scrud_django.urls

    url = url_of(todo_list)
    Given.http.force_authenticate(todo_reader_login)
    body = json.dumps(todo_list.content)
    responses = {}
    try:
        with without_ui_app():
            clear_url_caches()
            importlib.reload(scrud_django.urls)
            for accept in ("text/html", JSON):
                # 403 (a reader PUTs), 412 and 400 are the JSON problems, as
                # before the app existed, whatever the client accepts
                When.http.put(
                    url,
                    data=body,
                    content_type="application/json",
                    HTTP_IF_MATCH=f'"{todo_list.etag}"',
                    HTTP_ACCEPT=accept,
                )
                responses[accept] = Http.response
                Then.http.status_code_is(403)
                When.http.put(
                    url,
                    data="{",
                    content_type="application/json",
                    HTTP_IF_MATCH='"stale"',
                    HTTP_ACCEPT=accept,
                )
                assert Http.response.status_code in (400, 403)
                assert "scrud-notice" not in Http.response.content.decode()
    finally:
        clear_url_caches()
        importlib.reload(scrud_django.urls)
    html_accept, json_accept = responses["text/html"], responses[JSON]
    assert html_accept["Content-Type"] == json_accept["Content-Type"]
    assert html_accept["Content-Type"] == "application/problem+json"
    assert html_accept.content == json_accept.content
    assert "scrud-notice" not in html_accept.content.decode()
    vary = html_accept.get("Vary", "").lower()
    assert "scrud-ui" not in vary and "hx-request" not in vary
    cache = html_accept.get("Cache-Control", "")
    assert "no-store" not in cache


@pytest.mark.django_db
@test_context
def test_json_round_trip_without_the_ui_app(todo_list, todo_owner_login):
    Given.http.force_authenticate(todo_owner_login)
    with without_ui_app():
        When.http.get(url_of(todo_list), HTTP_ACCEPT=JSON)
        Then.http.status_code_is(200)
        etag = Http.response["ETag"]
        When.http.put(
            url_of(todo_list),
            data=json.dumps({**todo_list.content, "title": "Renamed"}),
            content_type="application/json",
            HTTP_IF_MATCH=etag,
        )
        Then.http.status_code_is(200)
        assert Http.response.json()["title"] == "Renamed"


def test_ui_renderers_places_html_before_the_browsable_api():
    ordered = ui_renderers([JSONRenderer(), BrowsableAPIRenderer()])
    assert [type(r) for r in ordered] == [
        JSONRenderer,
        ScrudHTMLRenderer,
        BrowsableAPIRenderer,
    ]


def test_ui_renderers_puts_html_ahead_of_a_browsable_api_listed_first():
    """A project listing the browsable API first would otherwise answer a
    ``*/*`` with it; the HTML renderer still sits just before it (a ``*/*``
    then gets HTML, as the project's own order says)."""
    ordered = ui_renderers([BrowsableAPIRenderer(), JSONRenderer()])
    assert [type(r) for r in ordered] == [
        ScrudHTMLRenderer,
        BrowsableAPIRenderer,
        JSONRenderer,
    ]


def test_ui_renderers_appends_without_a_browsable_api():
    ordered = ui_renderers([JSONRenderer()])
    assert [type(r) for r in ordered] == [JSONRenderer, ScrudHTMLRenderer]
    assert [type(r) for r in ui_renderers([])] == [ScrudHTMLRenderer]


def test_ui_renderers_is_idempotent_and_leaves_its_input_alone():
    original = [JSONRenderer(), BrowsableAPIRenderer()]
    once = ui_renderers(original)
    assert [type(r) for r in original] == [JSONRenderer, BrowsableAPIRenderer]
    twice = ui_renderers(once)
    assert [type(r) for r in twice] == [type(r) for r in once]
    assert twice == once
    assert sum(isinstance(r, ScrudHTMLRenderer) for r in twice) == 1
    mine = ScrudHTMLRenderer()
    kept = ui_renderers([mine, JSONRenderer()])
    assert kept[0] is mine and len(kept) == 2
