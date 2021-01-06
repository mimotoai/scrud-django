import pytest
from django.urls import reverse
from rest_framework import status
from safetydance_django.test import *  # noqa: F403, F401
from safetydance_test import Then, When, scripted_test

from .fixtures import *  # noqa: F401, F403
from .fixtures import RESOURCE_ENDPOINT_LIST_NAME


@pytest.mark.django_db
@scripted_test
def test_unauthenticated_fails(partner_profiles):
    url = reverse(RESOURCE_ENDPOINT_LIST_NAME)
    When.http.get(url)
    # Then.http.status_code_is(status.HTTP_401_UNAUTHORIZED)
    # Expecting 403 due to the authentication scheme we're using
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)


@pytest.mark.django_db
@scripted_test
def test_unauthorized_fails(partner_profiles, login_no_roles):
    url = reverse(RESOURCE_ENDPOINT_LIST_NAME)
    When.http.get(url)
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
