"""The scrud views' errors follow ``SCOPED_RBAC["UNIFORM_ERRORS"]``, as
scoped-rbac's own viewsets do: on (the default), every error, scrudful's own
400/412/428 included, is a problem details object served as
``application/problem+json`` with the problem-details Links; off, every error
keeps DRF's (and scrudful's) native ``{"detail": ...}`` as
``application/json``, with no problem-details Link."""

from uuid import uuid4

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.http import quote_etag
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .rbac_defaults import TYPE_URI_PARTNER_PROFILES as PP

PROBLEM_SCHEMA = "https://w3id.org/semantic-http/0.1/problem-details.schema.json"


def error_responses():
    """One response for each error status, from the partner-profiles views."""
    reverse("partner-profiles-list")
    resource = ResourceRegistration.register(
        {"name": "x", "rbac_context": "a"}, "partner-profiles"
    )
    url = reverse("partner-profiles-detail", args=[resource.pk])
    superuser = APIClient()
    superuser.force_authenticate(UserFactory(is_superuser=True, is_staff=True))
    reader_user = UserFactory()
    role = Role(
        definition={"http.GET": [PP]}, etag=uuid4().hex, modified_at=timezone.now()
    )
    role.save()
    RoleAssignment(
        user=reader_user,
        role=role,
        rbac_context="a",
        etag=uuid4().hex,
        modified_at=timezone.now(),
    ).save()
    reader = APIClient()
    reader.force_authenticate(reader_user)
    return {
        401: APIClient().get(url),
        403: reader.delete(url, HTTP_IF_MATCH=quote_etag(resource.etag)),
        404: superuser.get(reverse("partner-profiles-detail", args=[999999])),
        412: superuser.put(url, {"name": "y"}, format="json", HTTP_IF_MATCH='"no"'),
        415: superuser.post(
            reverse("partner-profiles-list"),
            "a=b",
            content_type="application/x-www-form-urlencoded",
        ),
        428: superuser.put(url, {"name": "y"}, format="json"),
    }


@pytest.mark.django_db
def test_errors_are_problem_details_with_uniform_errors_on():
    for status, response in error_responses().items():
        assert response.status_code == status, response.content
        assert response["Content-Type"] == "application/problem+json", status
        assert PROBLEM_SCHEMA in response.get("Link", ""), status
        body = response.json()
        assert body["type"] == "about:blank", (status, body)
        assert body["status"] == status, (status, body)
        assert isinstance(body["code"], str), (status, body)


@pytest.mark.django_db
def test_errors_keep_their_native_shape_with_uniform_errors_off():
    off = dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=False)
    with override_settings(SCOPED_RBAC=off):
        responses = error_responses()
    for status, response in responses.items():
        assert response.status_code == status, response.content
        assert response["Content-Type"] == "application/json", status
        assert PROBLEM_SCHEMA not in response.get("Link", ""), status
        body = response.json()
        assert set(body) >= {"detail"}, (status, body)
        assert "type" not in body and "code" not in body, (status, body)
    assert responses[412].json() == {"detail": "Precondition failed."}


@pytest.mark.django_db
def test_the_service_lists_conditional_400_follows_uniform_errors():
    # services/ is a scrudful function view: its malformed-precondition 400
    # is problem details while uniform errors are on, as the resource views'.
    reverse("partner-profiles-list")
    response = APIClient().get(reverse("services"), HTTP_IF_NONE_MATCH="abc")
    assert response.status_code == 400, response.content
    assert response["Content-Type"] == "application/problem+json"
    assert PROBLEM_SCHEMA in response.get("Link", "")
    body = response.json()
    assert body["status"] == 400, body
    assert isinstance(body["code"], str), body
    off = dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=False)
    with override_settings(SCOPED_RBAC=off):
        response = APIClient().get(reverse("services"), HTTP_IF_NONE_MATCH="abc")
    assert response.status_code == 400, response.content
    assert response["Content-Type"] == "application/json"
    assert response.json() == {"detail": "Malformed If-None-Match header."}
