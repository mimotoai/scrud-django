"""Parity: the DRF and the ninja front end, both mounted by
`tests.parity_urls` (``/drf/``, ``/ninja/``) over one database, answer each
case alike, normalized only as `tests.front_ends` says."""

import pytest
from django.http import HttpResponse

from scrud_django import registration
from scrud_django.models import Resource

from .front_ends import SETUP_TIME, Case, Exchange, normalized, parity
from .resource_types import PARTNER_PROFILES_TYPE

pytestmark = [pytest.mark.urls("tests.parity_urls"), pytest.mark.django_db]

HSR_REL = "https://w3id.org/semantic-http/ns#http-schema-resource-document"


def test_the_harness_normalizes_only_the_listed_differences():
    body = (
        '{"href":"http://testserver/ninja/todos/12/","id":12,'
        '"next":"http://testserver/ninja/todos/123/?page=12","other":"/x12/"}'
    )
    response = HttpResponse(body, content_type="application/json")
    response["Content-Length"] = str(len(body.encode()))
    response["Date"] = "Thu, 01 Oct 2026 12:00:00 GMT"
    response["Vary"] = "Accept"
    response["WWW-Authenticate"] = 'Basic realm="api"'
    response.set_cookie("sessionid", "x")
    response["Location"] = "http://testserver/ninja/todos/12/"
    response["Link"] = (
        '<http://testserver/ninja/todos/_hsr/slug>; rel="' + HSR_REL + '"; '
        'type="application/json", <http://testserver/ninja/todos/12/s>; '
        'rel="describedby"'
    )
    response["ETag"] = '"12"'

    exchange = normalized(response, "/ninja/", {"todo": 12, "next": 123})

    expected_body = (
        '{"href":"http://testserver{mount}todos/{todo}/","id":12,'
        '"next":"http://testserver{mount}todos/{next}/?page=12","other":"/x12/"}'
    )
    assert exchange == Exchange(
        status=200,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(expected_body.encode())),
            "Location": "http://testserver{mount}todos/{todo}/",
            "Link": (
                '<{hsr}>; rel="' + HSR_REL + '"; type="application/json", '
                '<http://testserver{mount}todos/{todo}/s>; rel="describedby"'
            ),
            "ETag": '"12"',
        },
        body=expected_body,
    )


def test_the_harness_refuses_keys_it_cannot_tell_apart():
    with pytest.raises(ValueError):
        normalized(HttpResponse(), "/drf/", {"a": 1, "b": 1})


def test_each_front_end_runs_on_the_same_fresh_data():
    registration.json_resource_type_spec(**PARTNER_PROFILES_TYPE).register()
    before = Resource.objects.count()
    seen = []

    def setup(client):
        count = Resource.objects.count()
        resource = registration.ResourceRegistration.register(
            {"name": "x", "rbac_context": "a"}, "partner-profiles"
        )
        seen.append((count, resource.etag, resource.modified_at))
        return {"profile": resource.pk}

    parity(Case("fresh", "GET", "{mount}partner-profiles/{profile}/", setup))

    assert seen[0] == seen[1] == (before, seen[0][1], SETUP_TIME)
    assert Resource.objects.count() == before


@pytest.mark.xfail(strict=True, reason="Task 8")
def test_services_match():
    drf, ninja = parity(Case("services", "GET", "{mount}services/"))

    assert drf == ninja


def test_unknown_route_is_404_on_both():
    drf, ninja = parity(Case("no-such-type", "GET", "{mount}no-such-type/"))

    assert drf.status == ninja.status == 404
