"""Conditional requests (RFC 9110 §13) on scrud resources, served by scrudful."""

import json
import time

import pytest
from django.test import override_settings
from django.utils import timezone
from django.utils.http import parse_http_date, quote_etag
from safetydance_django.test import *  # noqa: F403, F401

from .fixtures import *  # noqa
from .fixtures import (
    RESOURCE_ENDPOINT_DETAIL_NAME,
    RESOURCE_ENDPOINT_LIST_NAME,
    last_modified_for,
)
from .front_ends import api_url, hsr_url
from .steps import *  # noqa: F403, F401


@pytest.mark.django_db
@test_context
def test_conditional_write_on_unreadable_resource_is_404(
    front_end, partner_profiles, login_other_context
):
    # The user passes the view-level check (grants in context "other") but can't
    # read the resource (in ""): get_object() answers 404 before any validator
    # is computed, so no 412/428/304 reveals that the resource exists.
    url = api_url(
        front_end, RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id}
    )
    Given.http.force_login(login_other_context)
    When.http.put(
        url,
        data={"rbac_context": "other"},
        format="json",
        HTTP_IF_MATCH='"anything"',
    )
    Then.http.status_code_is(404)
    When.http.delete(url, HTTP_IF_UNMODIFIED_SINCE="Thu, 01 Jan 1970 00:00:00 GMT")
    Then.http.status_code_is(404)
    When.http.get(url, HTTP_IF_NONE_MATCH="*")
    Then.http.status_code_is(404)


@pytest.mark.django_db
@test_context
def test_view_level_denial_does_not_reveal_existence(
    front_end, partner_profiles, login_no_roles
):
    existing = api_url(
        front_end, RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id}
    )
    missing = api_url(
        front_end,
        RESOURCE_ENDPOINT_DETAIL_NAME,
        kwargs={"slug": partner_profiles.id + 1000},
    )
    Given.http.force_login(login_no_roles)
    When.http.put(existing, data={}, format="json", HTTP_IF_MATCH='"anything"')
    existing_status = Http.response.status_code
    When.http.put(missing, data={}, format="json", HTTP_IF_MATCH='"anything"')
    missing_status = Http.response.status_code
    assert existing_status == missing_status
    assert existing_status == 403
    assert missing_status == 403


@pytest.mark.django_db
@test_context
def test_put_without_precondition_is_428(
    front_end, partner_profiles, admin_login, partner_profile_post_data
):
    url = api_url(
        front_end, RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id}
    )
    Given.http.force_login(admin_login)
    When.http.put(
        url,
        data=json.dumps(partner_profile_post_data),
        content_type="application/json",
    )
    Then.http.status_code_is(428)


@pytest.mark.django_db
@test_context
def test_patch_is_not_routed(
    front_end, partner_profiles, admin_login, partner_profile_post_data
):
    url = api_url(
        front_end, RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id}
    )
    Given.http.force_login(admin_login)
    # safetydance-django has no http.patch step.
    response = Http.api_client.patch(
        url,
        data=json.dumps(partner_profile_post_data),
        content_type="application/json",
    )
    assert response.status_code == 405, response.status_code


@pytest.mark.django_db
@test_context
def test_if_modified_since_current_is_304(front_end, partner_profiles, admin_login):
    url = api_url(
        front_end, RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id}
    )
    Given.http.force_login(admin_login)
    When.http.get(url)
    Then.http.status_code_is(200)
    last_modified = Http.response["Last-Modified"]
    When.http.get(url, HTTP_IF_MODIFIED_SINCE=last_modified)
    Then.http.status_code_is(304)


HSR_REL = "https://w3id.org/semantic-http/ns#http-schema-resource-document"


def hsr_link(front_end, route_name):
    """The Link scrudful (0.10) adds after a response's other Links: the
    route's HTTP Schema Resource (on `front_end`)."""
    return (
        f'<{hsr_url(front_end, route_name)}>; rel="{HSR_REL}"; type="application/json"'
    )


def _detail_url_of(front_end, resource):
    return "http://testserver" + api_url(
        front_end, resource.resource_type.route_name_detail(), args=[resource.id]
    )


@pytest.mark.django_db
@test_context
def test_detail_validators_and_links(front_end, partner_profiles, regular_login):
    url = api_url(
        front_end, RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id}
    )
    resource_type = partner_profiles.resource_type
    schema_url = _detail_url_of(front_end, resource_type.schema)
    context_url = _detail_url_of(front_end, resource_type.context)

    Given.http.force_login(regular_login)
    When.http.get(url)
    Then.http.status_code_is(200)
    # The plain quoted etag: no __version__ suffix (scrudful appends nothing).
    assert Http.response["ETag"] == quote_etag(partner_profiles.etag)
    assert Http.response["ETag"] == f'"{partner_profiles.etag}"'
    assert Http.response["Last-Modified"] == last_modified_for(partner_profiles)
    # Schema link: rel="describedby", type="application/schema+json"; context
    # link: JSON-LD's context relation and type.
    # Then the route's HTTP Schema Resource (scrudful 0.10).
    assert Http.response["Link"] == (
        f'<{schema_url}>; rel="describedby"; type="application/schema+json", '
        f'<{context_url}>; rel="http://www.w3.org/ns/json-ld#context"; '
        'type="application/ld+json", '
        + hsr_link(front_end, RESOURCE_ENDPOINT_DETAIL_NAME)
    )


# scrudful's own 400, 412 and 428 are problem details, as scoped-rbac's own
# viewsets answer them (scoped_rbac.problems.conditional_problem): type
# about:blank, the reason phrase as title, the status, scrudful's detail and a
# code, served as application/problem+json with the problem-details Links,
# then the route's HTTP Schema Resource (scrudful 0.10).
PROBLEM_LINKS = (
    '<https://w3id.org/semantic-http/0.1/problem-details.schema.json>; '
    'rel="describedby"; type="application/schema+json", '
    '<https://w3id.org/semantic-http/0.1/problem-details.context.jsonld>; '
    'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"'
)


def assert_precondition_problem(
    front_end, status, title, code, detail=None, route=RESOURCE_ENDPOINT_DETAIL_NAME
):
    assert Http.response.status_code == status, Http.response.content
    assert Http.response["Content-Type"] == "application/problem+json"
    assert Http.response["Link"] == PROBLEM_LINKS + ", " + hsr_link(front_end, route)
    body = json.loads(Http.response.content)
    assert body["type"] == "about:blank", body
    assert body["title"] == title, body
    assert body["status"] == status, body
    assert body["code"] == code, body
    assert isinstance(body["detail"], str) and body["detail"], body
    if detail is not None:
        assert body["detail"] == detail, body
    return body


@pytest.mark.django_db
@test_context
def test_envelope_link_is_the_detail_link_header(
    front_end, partner_profiles, regular_login
):
    # A list envelope's "link" is the resource's Links that its detail
    # response carries: the schema as rel="describedby" with type
    # application/schema+json, and the JSON-LD context. The detail response
    # then links its route's HTTP Schema Resource (scrudful 0.10), which
    # describes the detail URL's methods, not the resource: the envelope
    # leaves it out.
    url = api_url(
        front_end, RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id}
    )
    href = "http://testserver" + url
    Given.http.force_login(regular_login)
    When.http.get(url)
    Then.http.status_code_is(200)
    header = Http.response["Link"]
    When.http.get(api_url(front_end, RESOURCE_ENDPOINT_LIST_NAME))
    Then.http.status_code_is(200)
    envelopes = {
        entry["href"]: entry for entry in json.loads(Http.response.content)["content"]
    }
    link = envelopes[href]["link"]
    assert header == link + ", " + hsr_link(front_end, RESOURCE_ENDPOINT_DETAIL_NAME)
    schema_url = _detail_url_of(front_end, partner_profiles.resource_type.schema)
    assert link.startswith(
        f'<{schema_url}>; rel="describedby"; type="application/schema+json", '
    ), link


@pytest.mark.django_db
@test_context
def test_failed_if_match_is_412_with_problem_details(
    front_end, partner_profiles, admin_login, partner_profile_post_data
):
    url = api_url(
        front_end, RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id}
    )
    Given.http.force_login(admin_login)
    When.http.put(
        url,
        data=json.dumps(partner_profile_post_data),
        content_type="application/json",
        HTTP_IF_MATCH='"not-the-current-etag"',
    )
    body = assert_precondition_problem(
        front_end,
        412,
        "Precondition Failed",
        "precondition_failed",
        "Precondition failed.",
    )
    assert body == {
        "type": "about:blank",
        "title": "Precondition Failed",
        "status": 412,
        "detail": "Precondition failed.",
        "code": "precondition_failed",
    }
    When.http.delete(url, HTTP_IF_MATCH='"not-the-current-etag"')
    assert_precondition_problem(
        front_end,
        412,
        "Precondition Failed",
        "precondition_failed",
        "Precondition failed.",
    )


@pytest.mark.django_db
@test_context
def test_missing_precondition_is_428_with_problem_details(
    front_end, partner_profiles, admin_login, partner_profile_post_data
):
    url = api_url(
        front_end, RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id}
    )
    Given.http.force_login(admin_login)
    When.http.put(
        url,
        data=json.dumps(partner_profile_post_data),
        content_type="application/json",
    )
    expected = {
        "type": "about:blank",
        "title": "Precondition Required",
        "status": 428,
        "detail": "This request must be conditional.",
        "accepted-precondition-headers": ["If-Match", "If-Unmodified-Since"],
        "code": "precondition_required",
    }
    body = assert_precondition_problem(
        front_end, 428, "Precondition Required", "precondition_required"
    )
    assert body == expected
    When.http.delete(url)
    body = assert_precondition_problem(
        front_end, 428, "Precondition Required", "precondition_required"
    )
    assert body == expected


@pytest.mark.django_db
@test_context
def test_malformed_precondition_is_400_with_problem_details(
    front_end, partner_profiles, admin_login, partner_profile_post_data
):
    url = api_url(
        front_end, RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id}
    )
    Given.http.force_login(admin_login)
    When.http.put(
        url,
        data=json.dumps(partner_profile_post_data),
        content_type="application/json",
        HTTP_IF_MATCH="not-quoted",
    )
    assert_precondition_problem(front_end, 400, "Bad Request", "malformed_precondition")
    When.http.get(url, HTTP_IF_NONE_MATCH="not-quoted")
    assert_precondition_problem(front_end, 400, "Bad Request", "malformed_precondition")
    When.http.get(
        api_url(front_end, RESOURCE_ENDPOINT_LIST_NAME), HTTP_IF_NONE_MATCH="not-quoted"
    )
    assert_precondition_problem(
        front_end,
        400,
        "Bad Request",
        "malformed_precondition",
        route=RESOURCE_ENDPOINT_LIST_NAME,
    )


@pytest.mark.django_db
@test_context
def test_put_with_only_if_match_succeeds(
    front_end, partner_profiles, admin_login, partner_profile_post_data
):
    url = api_url(
        front_end, RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id}
    )
    Given.http.force_login(admin_login)
    When.http.head(url)
    Then.http.status_code_is(200)
    etag = Http.response["ETag"]
    assert etag == quote_etag(partner_profiles.etag)
    When.http.put(
        url,
        data=json.dumps(partner_profile_post_data),
        content_type="application/json",
        HTTP_IF_MATCH=etag,
    )
    Then.http.status_code_is(200)
    And.http.resource_json_is(partner_profile_post_data)


@pytest.mark.django_db
@test_context
def test_put_with_only_if_unmodified_since_succeeds(
    front_end, partner_profiles, admin_login, partner_profile_post_data
):
    url = api_url(
        front_end, RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id}
    )
    Given.http.force_login(admin_login)
    When.http.get(url)
    Then.http.status_code_is(200)
    last_modified = Http.response["Last-Modified"]
    assert last_modified == last_modified_for(partner_profiles)
    When.http.put(
        url,
        data=json.dumps(partner_profile_post_data),
        content_type="application/json",
        HTTP_IF_UNMODIFIED_SINCE=last_modified,
    )
    Then.http.status_code_is(200)
    And.http.resource_json_is(partner_profile_post_data)


@pytest.mark.django_db
@test_context
def test_last_modified_agrees_with_envelope_when_use_tz_is_false(
    front_end, admin_login, partner_profile_post_data
):
    # With USE_TZ=False, modified_at is a naive datetime in TIME_ZONE. scrudful
    # reads it so for the Last-Modified header; the envelope, the search index
    # and last_modified_for must read it the same way, not as UTC.
    from scrud_django.registration import ResourceRegistration
    from scrud_django.search_indexes import ResourceIndex

    with override_settings(USE_TZ=False, TIME_ZONE="America/Chicago"):
        api_url(front_end, RESOURCE_ENDPOINT_LIST_NAME)
        resource = ResourceRegistration.register(
            content=partner_profile_post_data, register_type="partner-profiles"
        )
        resource.refresh_from_db()
        assert timezone.is_naive(resource.modified_at)
        href = "http://testserver" + api_url(
            front_end, RESOURCE_ENDPOINT_DETAIL_NAME, args=[resource.id]
        )
        Given.http.force_login(admin_login)
        When.http.get(href)
        Then.http.status_code_is(200)
        header = Http.response["Last-Modified"]
        # The header is the true instant of the write.
        assert abs(parse_http_date(header) - time.time()) < 60, header

        When.http.get(api_url(front_end, RESOURCE_ENDPOINT_LIST_NAME))
        Then.http.status_code_is(200)
        envelopes = {
            entry["href"]: entry
            for entry in json.loads(Http.response.content)["content"]
        }
        assert envelopes[href]["last_modified"] == header
        assert last_modified_for(resource) == header
        assert ResourceIndex().prepare(resource)["modified_at"] == header
