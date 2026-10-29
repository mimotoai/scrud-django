import pytest
from django.urls import reverse
from rest_framework import status
from safetydance_django.test import *  # noqa: F403, F401

from .fixtures import *  # noqa: F401, F403
from .fixtures import RESOURCE_ENDPOINT_LIST_NAME


@pytest.mark.django_db
@test_context
def test_unauthenticated_fails(partner_profiles):
    url = reverse(RESOURCE_ENDPOINT_LIST_NAME)
    When.http.get(url)
    # scoped-rbac 0.11's problem-details handler maps an unauthenticated request to 401
    Then.http.status_code_is(status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
@test_context
def test_unauthorized_fails(partner_profiles, login_no_roles):
    url = reverse(RESOURCE_ENDPOINT_LIST_NAME)
    Given.http.force_authenticate(login_no_roles)
    When.http.get(url)
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)


@pytest.mark.django_db
@test_context
def test_unauthenticated_authorized_post(partner_profiles):
    url = reverse(RESOURCE_ENDPOINT_LIST_NAME)
    from django.contrib.auth.models import AnonymousUser
    from rest_framework.test import APIRequestFactory
    from scoped_rbac.permissions import policy_for

    dummy_request = APIRequestFactory().post(url, {})
    dummy_request.user = AnonymousUser()
    policy = policy_for(dummy_request)

    When.http.post(
        url,
        {
            "name": "Some Name",
            "rbac_context": "",
        },
        format="json",  # noqa: E231
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)
