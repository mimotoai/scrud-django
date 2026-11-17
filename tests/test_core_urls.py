"""`absolute_reverse` reverses within the namespace the request was resolved
under (R5), falling back to the global route, then to the namespaces the
APIs noted; `api_route_name` and `api_reverse` find a type's API routes by
their bare names, then in those namespaces (follow-up ``ui-ninja-routes``)."""

import pytest
from django.test import RequestFactory
from django.urls import NoReverseMatch, resolve, reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.core.urls import absolute_reverse

pytestmark = [pytest.mark.django_db, pytest.mark.urls("tests.namespaced_urls")]


def resolved_request(path):
    request = RequestFactory().get(path)
    request.resolver_match = resolve(path)
    return request


def test_namespaced_request_reverses_in_its_namespace():
    request = resolved_request("/ns/partner-profiles/a/")
    assert request.resolver_match.namespace == "ns"
    url = absolute_reverse(
        "partner-profiles-detail", kwargs={"slug": "b"}, request=request
    )
    assert url == "http://testserver/ns/partner-profiles/b/"


def test_plain_request_reverses_globally():
    request = resolved_request("/services/")
    assert request.resolver_match.namespace == ""
    url = absolute_reverse(
        "partner-profiles-detail", kwargs={"slug": "b"}, request=request
    )
    assert url == "http://testserver/partner-profiles/b/"


def test_request_without_resolver_match_reverses_globally():
    request = RequestFactory().get("/ns/partner-profiles/a/")
    assert absolute_reverse("services", request=request) == (
        "http://testserver/services/"
    )


def test_namespace_without_the_name_falls_back():
    request = resolved_request("/ns/partner-profiles/a/")
    assert absolute_reverse("services", request=request) == (
        "http://testserver/services/"
    )


def test_namespace_reverses_a_link_without_a_request_in_it():
    # A link described without a request (scrudful's HTTP Schema Resource)
    # points at the routes of the API describing it.
    url = absolute_reverse(
        "partner-profiles-detail", kwargs={"slug": "b"}, namespace="ns"
    )
    assert url == "/ns/partner-profiles/b/"


def test_namespace_without_the_name_falls_back_without_a_request():
    assert absolute_reverse("services", namespace="ns") == "/services/"


def test_the_requests_namespace_wins_over_the_given_one():
    request = resolved_request("/services/")
    # Resolved under no namespace: the given one is asked first.
    assert absolute_reverse(
        "partner-profiles-detail", kwargs={"slug": "b"}, request=request, namespace="ns"
    ) == ("http://testserver/ns/partner-profiles/b/")
    request = resolved_request("/ns/partner-profiles/a/")
    assert absolute_reverse(
        "partner-profiles-detail",
        kwargs={"slug": "b"},
        request=request,
        namespace="other",
    ) == ("http://testserver/ns/partner-profiles/b/")


def test_first_absolute_reverse_refuses_a_bare_name():
    """A bare string is one name, not a sequence of its characters
    (`types.md` S8)."""
    from scrud_django.core.urls import first_absolute_reverse

    with pytest.raises(TypeError):
        first_absolute_reverse("services")


@pytest.fixture
def api_routes(monkeypatch):
    """A registry of the API routes of its own (`note_api_routes`), so that
    what a test notes is forgotten after it."""
    from scrud_django.core import urls

    monkeypatch.setattr(urls, "_api_routes", {})
    return urls


def test_the_bare_name_wins_then_each_noted_namespace(api_routes):
    """`api_route_name` tries a type's bare route name first, then the name in
    each namespace an API noted (in the order noted), and takes the first the
    URLconf reverses (a detail with one key)."""
    api_routes.note_api_routes(["partner-profiles"], namespace=None)
    assert api_routes.api_namespaces() == (None,)
    assert api_routes.api_route_name("partner-profiles", "detail") == (
        "partner-profiles-detail"
    )
    # ``ns`` routes a detail of ``partner-profiles`` named so; the bare name
    # does not reverse for ``ns-only``.
    assert api_routes.api_route_name("ns-only", "detail") is None
    api_routes.note_api_routes(["partner-profiles"], namespace="absent")
    api_routes.note_api_routes(["partner-profiles", "other"], namespace="ns")
    assert api_routes.api_namespaces() == (None, "absent", "ns")
    assert api_routes.api_slugs() == ("partner-profiles", "other")
    assert api_routes.api_route_name("partner-profiles", "detail") == (
        "partner-profiles-detail"
    )
    assert api_routes.api_route_name("partner-profiles", "list") == (
        "partner-profiles-list"
    )
    assert api_routes.api_reverse("partner-profiles", "detail", args=["b"]) == (
        "/partner-profiles/b/"
    )
    with pytest.raises(NoReverseMatch):
        api_routes.api_reverse("never-routed", "list")
    with pytest.raises(ValueError):
        api_routes.api_route_name("partner-profiles", "items")


@pytest.mark.urls("tests.ninja_ui_urls")
def test_a_type_routed_in_a_noted_namespace_only(api_routes):
    """On ``tests.ninja_ui_urls`` (the ninja API in ``scrud-ninja``, no DRF
    route) a type's routes are found in the namespace its API noted, and a
    link a request outside it (a UI page's, or none) asks for reverses
    there."""
    from .ninja_api import API

    # The URLconf imported (its first import notes its API's routes), then
    # the registry emptied.
    assert reverse("scrud-ninja:todo-lists-list") == "/api/todo-lists/"
    api_routes._api_routes.clear()
    api_routes.note_api_routes(["todo-lists"], namespace=None)
    assert api_routes.api_route_name("todo-lists", "list") is None
    api_routes.note_api_routes(["todo-lists"], namespace=API.urls_namespace)
    assert api_routes.api_route_name("todo-lists", "list") == (
        "scrud-ninja:todo-lists-list"
    )
    assert api_routes.api_reverse("todo-lists", "detail", args=["7"]) == (
        "/api/todo-lists/7/"
    )
    request = RequestFactory().get("/todo-lists/")
    assert absolute_reverse("todo-lists-detail", args=["7"], request=request) == (
        "http://testserver/api/todo-lists/7/"
    )
    with pytest.raises(NoReverseMatch):
        absolute_reverse("ui-components-list", request=request)
