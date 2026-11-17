"""Content negotiation of the API: JSON only, as far as the scrud UI goes.

The UI's HTML is the UI app's (`scrud_django.ui.urls`); the API never answers
it. DRF's own browsable API is still among DRF's renderers, so a browser's
``Accept`` gets that on DRF (never ``scrud-resource`` markup), and ninja, which
never answers HTML, answers JSON."""

import importlib
import json

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import clear_url_caches, reverse
from rest_framework.renderers import BrowsableAPIRenderer, JSONRenderer
from rest_framework.settings import api_settings
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.drf.views import ResourceViewSet
from scrud_django.ui.branches import (
    UI_BRANCH_SLUG,
    UI_CSS_SLUG,
    UI_JS_SLUG,
    UI_PUSH_SLUG,
)

from .fixtures import *  # noqa
from .front_ends import api_url
from .steps import *  # noqa: F403, F401

BROWSER = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,*/*;q=0.8"
)
JSON = "application/json"
HTML = "text/html; charset=utf-8"
HTML_ACCEPTS = ["text/html", "text/html, application/json;q=0.9", BROWSER]
#: The UI's own resource types (served by DRF only).
UI_SLUGS = ["ui-components", "ui-stylesheets", UI_BRANCH_SLUG, UI_PUSH_SLUG]
UI_SLUGS += [UI_CSS_SLUG, UI_JS_SLUG]


def url_of(todo_list):
    return reverse("todo-lists-detail", args=[todo_list.pk])


def without_ui_app():
    installed = [app for app in settings.INSTALLED_APPS if app != "scrud_django.ui"]
    return override_settings(INSTALLED_APPS=installed)


def api_paths(front_end, todo_list):
    """The API paths the UI used to answer, on `front_end`: an item, a list
    and a computed resource."""
    return {
        "item": api_url(front_end, "todo-lists-detail", args=[todo_list.pk]),
        "list": api_url(front_end, "todo-lists-list"),
        "computed": api_url(front_end, "summaries-detail", args=["k1"]),
    }


def assert_no_scrud_ui(response):
    body = response.content.decode()
    assert "scrud-resource" not in body
    assert "scrud-notice" not in body
    assert "scrud-ui.js" not in body


@pytest.mark.django_db
@pytest.mark.parametrize("accept", HTML_ACCEPTS)
@pytest.mark.parametrize("kind", ["item", "list", "computed"])
def test_the_api_never_answers_scrud_ui_html(
    front_end, client, admin_login, todo_list, kind, accept
):
    client.force_login(admin_login)
    path = api_paths(front_end, todo_list)[kind]
    response = client.get(path, HTTP_ACCEPT=accept)
    assert response.status_code == 200
    assert_no_scrud_ui(response)
    if front_end == "ninja":
        assert response["Content-Type"].startswith(JSON)
        assert json.loads(response.content) is not None
    elif response["Content-Type"].startswith("text/html"):
        # DRF's browsable API, which its renderers still include
        assert "Django REST framework" in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("accept", HTML_ACCEPTS)
@pytest.mark.parametrize("slug", UI_SLUGS)
def test_the_api_never_answers_scrud_ui_html_for_a_ui_type(
    client, admin_login, slug, accept
):
    client.force_login(admin_login)
    response = client.get(reverse(f"{slug}-list"), HTTP_ACCEPT=accept)
    assert response.status_code == 200
    assert_no_scrud_ui(response)


def test_drf_still_offers_its_browsable_api():
    """A resource view's renderers are DRF's own: no scrud HTML renderer."""
    drf = list(api_settings.DEFAULT_RENDERER_CLASSES)
    assert [type(r) for r in ResourceViewSet().get_renderers()] == drf
    assert BrowsableAPIRenderer in drf and JSONRenderer in drf
    with without_ui_app():
        assert [type(r) for r in ResourceViewSet().get_renderers()] == drf


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["item", "list"])
def test_json_from_the_api_has_no_ui_vary(
    front_end, client, admin_login, todo_list, kind
):
    # Reading the session names Cookie in Vary (Django's session middleware),
    # whatever the front end: that is the credential's, not the UI's. DRF is
    # authenticated without a session here, so its Vary shows the API's own.
    if front_end == "drf":
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(admin_login)
    else:
        client.force_login(admin_login)
    path = api_paths(front_end, todo_list)[kind]
    response = client.get(path, HTTP_ACCEPT=JSON)
    assert response.status_code == 200
    vary = {v.strip().lower() for v in response.get("Vary", "").split(",")}
    assert "accept" in vary
    assert "hx-request" not in vary
    if front_end == "drf":
        assert "cookie" not in vary
    assert not any(v.startswith("scrud-ui") for v in vary)


@pytest.mark.django_db
@pytest.mark.parametrize("accept", [JSON, "text/html"])
@pytest.mark.parametrize("kind", ["item", "list"])
def test_the_api_sets_no_branch_cookie_and_no_store(
    front_end, client, admin_login, todo_list, kind, accept
):
    """The branch cookie is the UI pages'; the API neither sets nor reads it
    (the UI/API cookie-name collision is gone), and adds no ``no-store``."""
    client.force_login(admin_login)
    path = api_paths(front_end, todo_list)[kind]
    response = client.get(path, HTTP_ACCEPT=accept)
    assert response.status_code == 200
    assert not any(name.startswith("scrud_ui_branch") for name in response.cookies)
    assert "no-store" not in response.get("Cache-Control", "")


@pytest.mark.django_db
def test_the_api_does_not_read_the_branch_cookie(client, admin_login, todo_list):
    """A browsing branch's cookie does not change what the API lists."""
    from scrud_django.ui.branches.selection import BRANCH_COOKIE

    client.force_login(admin_login)
    path = reverse("ui-components-list")
    before = client.get(path, HTTP_ACCEPT=JSON).json()
    client.cookies[BRANCH_COOKIE] = "no-such-branch"
    response = client.get(path, HTTP_ACCEPT=JSON)
    assert response.status_code == 200
    assert response.json() == before


@pytest.mark.django_db
@pytest.mark.parametrize(
    "accept,query,expected",
    [
        (None, "", JSON),
        ("*/*", "", JSON),
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
        # The scrud UI's renderer is gone: its format is unknown.
        ("*/*", "?format=html", 404),
    ],
)
@test_context
def test_negotiation_of_the_api(todo_list, todo_owner_login, accept, query, expected):
    Given.http.force_authenticate(todo_owner_login)
    headers = {"HTTP_ACCEPT": accept} if accept else {}
    When.http.get(url_of(todo_list) + query, **headers)
    body = Http.response.content.decode()
    if expected in (404, 406):
        Then.http.status_code_is(expected)
        assert_no_scrud_ui(Http.response)
        return
    Then.http.status_code_is(200)
    assert Http.response["Content-Type"].startswith(expected.split(";")[0])
    if expected == JSON:
        assert json.loads(body) == todo_list.content
    else:
        assert "Django REST framework" in body
        assert_no_scrud_ui(Http.response)


@pytest.mark.django_db
@test_context
def test_errors_with_a_html_accept_are_unchanged_without_the_ui_app(
    todo_list, todo_reader_login
):
    import scrud_django.drf.urls
    import scrud_django.urls

    url = url_of(todo_list)
    Given.http.force_authenticate(todo_reader_login)
    body = json.dumps(todo_list.content)
    responses = {}
    try:
        with without_ui_app():
            clear_url_caches()
            # scrud_django.urls re-exports the DRF URLconf's patterns.
            importlib.reload(scrud_django.drf.urls)
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
        # scrud_django.urls re-exports the DRF URLconf's patterns.
        importlib.reload(scrud_django.drf.urls)
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
