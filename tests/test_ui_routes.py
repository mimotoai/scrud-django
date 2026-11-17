"""Which API routes the UI app serves pages of, and what the system checks
say of those it does not: the UI included before the API at the same URLs,
a type routed only under a URL namespace (scrud's ninja ``api()``), mixed
marked and hand-built routes, a type stored but not registered in this
process, and type slugs the UI's own routes shadow."""

import pytest
from django.urls import resolve, reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.ui import UI_NAMESPACE, pages

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401

JSON = "application/json"


@pytest.mark.django_db
@test_context
def test_the_ui_included_before_the_api_does_not_shadow_it(
    settings, todo_list, todo_owner_login
):
    """``tests.ui_first_urls``: the UI's patterns come first at the API's
    URLs. Deciding whether the UI serves a type resolves its API routes,
    which reach the UI's own pattern for it: that does not recurse, and the
    UI does not serve the type there, so the API's routes answer."""
    settings.ROOT_URLCONF = "tests.ui_first_urls"
    list_url = reverse("todo-lists-list")
    detail_url = reverse("todo-lists-detail", args=[todo_list.pk])
    assert (list_url, detail_url) == ("/todo-lists/", f"/todo-lists/{todo_list.pk}/")
    assert pages.served("todo-lists") is None
    with pages.served_memo():
        assert pages.served("todo-lists") is None
        assert pages.served("todo-lists") is None
    for url in (list_url, detail_url):
        match = resolve(url)
        assert UI_NAMESPACE not in match.namespaces, url
        assert match.func is pages.route_views("todo-lists")[url == detail_url]
    Given.http.force_login(todo_owner_login)
    When.http.get(detail_url, HTTP_ACCEPT=JSON)
    Then.http.status_code_is(200)
    assert Http.response["Content-Type"] == JSON
    assert "Groceries" in Http.response.content.decode()
    When.http.get(list_url, HTTP_ACCEPT=JSON)
    Then.http.status_code_is(200)
    assert Http.response["Content-Type"] == JSON
    # The UI's own routes still answer.
    assert resolve("/studio/").namespaces == ["scrud-studio"]


@pytest.mark.django_db
def test_deciding_a_slug_resolves_its_routes_once(settings, monkeypatch, todo_list):
    settings.ROOT_URLCONF = "tests.mounted_urls"
    calls = []
    route_views = pages.route_views

    def counted(slug):
        calls.append(slug)
        return route_views(slug)

    monkeypatch.setattr(pages, "route_views", counted)
    assert pages.served("todo-lists") is not None
    assert calls == ["todo-lists"]
