"""`absolute_reverse` reverses within the namespace the request was resolved
under (R5), falling back to the global route."""

import pytest
from django.test import RequestFactory
from django.urls import resolve
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
