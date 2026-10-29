"""Conditional requests (RFC 9110 §13) on scrud resources, served by scrudful."""

import json

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from .fixtures import *  # noqa
from .fixtures import RESOURCE_ENDPOINT_DETAIL_NAME
from .steps import *  # noqa: F403, F401


@pytest.mark.django_db
@test_context
def test_conditional_write_on_unreadable_resource_is_404(
    partner_profiles, login_other_context
):
    # The user passes the view-level check (grants in context "other") but can't
    # read the resource (in ""): get_object() answers 404 before any validator
    # is computed, so no 412/428/304 reveals that the resource exists.
    url = reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id})
    Given.http.force_authenticate(login_other_context)
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
def test_view_level_denial_does_not_reveal_existence(partner_profiles, login_no_roles):
    existing = reverse(
        RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id}
    )
    missing = reverse(
        RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id + 1000}
    )
    Given.http.force_authenticate(login_no_roles)
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
    partner_profiles, admin_login, partner_profile_post_data
):
    url = reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id})
    Given.http.force_authenticate(admin_login)
    When.http.put(
        url,
        data=json.dumps(partner_profile_post_data),
        content_type="application/json",
    )
    Then.http.status_code_is(428)


@pytest.mark.django_db
@test_context
def test_patch_is_not_routed(partner_profiles, admin_login, partner_profile_post_data):
    url = reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id})
    Given.http.force_authenticate(admin_login)
    # safetydance-django has no http.patch step.
    response = Http.api_client.patch(
        url,
        data=json.dumps(partner_profile_post_data),
        content_type="application/json",
    )
    assert response.status_code == 405, response.status_code


@pytest.mark.django_db
@test_context
def test_if_modified_since_current_is_304(partner_profiles, admin_login):
    url = reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id})
    Given.http.force_authenticate(admin_login)
    When.http.get(url)
    Then.http.status_code_is(200)
    last_modified = Http.response["Last-Modified"]
    When.http.get(url, HTTP_IF_MODIFIED_SINCE=last_modified)
    Then.http.status_code_is(304)
