import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils.http import quote_etag
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource

from .fixtures import *  # noqa: F401, F403
from .fixtures import RESOURCE_ENDPOINT_DETAIL_NAME, RESOURCE_ENDPOINT_LIST_NAME

# A project whose DRF default lets everyone through, and whose anonymous users
# have no RBAC grants (scoped-rbac's default POLICY_FOR_UNAUTHENTICATED).
PERMISSIVE_REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "scrud_django.exceptions.scrud_exception_handler",
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
}
NO_ANONYMOUS_GRANTS = {"POLICY_FOR_STAFF": "tests.rbac_defaults.policy_for_staff"}


@pytest.fixture
def permissive_default(monkeypatch):
    """Make the DRF default permission AllowAny. ``override_settings`` alone
    reloads ``api_settings`` but not ``APIView.permission_classes``, which DRF
    reads from the settings once, at import; already-imported views inherit that
    class attribute, so it is patched too."""
    monkeypatch.setattr(APIView, "permission_classes", [AllowAny])
    with override_settings(
        REST_FRAMEWORK=PERMISSIVE_REST_FRAMEWORK, SCOPED_RBAC=NO_ANONYMOUS_GRANTS
    ):
        yield


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
    When.http.post(
        url,
        {
            "name": "Some Name",
            "rbac_context": "",
        },
        format="json",  # noqa: E231
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)


@pytest.mark.django_db
@test_context
def test_anonymous_post_fails_closed_with_permissive_default(
    partner_profiles, permissive_default
):
    # The scrud viewsets set permission_classes = [IsAuthorized] themselves, so a
    # permissive project default does not open them to anonymous writes.
    When.http.post(
        reverse(RESOURCE_ENDPOINT_LIST_NAME),
        {"name": "Some Name", "rbac_context": ""},
        format="json",
    )
    Then.http.status_code_is(status.HTTP_401_UNAUTHORIZED)


@pytest.mark.django_db
@test_context
def test_anonymous_delete_fails_closed_with_permissive_default(
    partner_profiles, permissive_default
):
    url = reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id})
    When.http.delete(url, HTTP_IF_MATCH=quote_etag(partner_profiles.etag))
    assert Http.response.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
    ), Http.response.status_code
    assert Resource.objects.filter(pk=partner_profiles.pk).exists()


def test_scrud_viewsets_set_is_authorized():
    # Both scrud viewsets carry the permission themselves, whatever the project's
    # DEFAULT_PERMISSION_CLASSES (ResourceViewSetGenericFilter has no test route).
    from scoped_rbac.permissions import IsAuthorized

    from scrud_django.views import ResourceViewSet, ResourceViewSetGenericFilter

    for viewset in (ResourceViewSet, ResourceViewSetGenericFilter):
        assert viewset.__dict__["permission_classes"] == [IsAuthorized], viewset
