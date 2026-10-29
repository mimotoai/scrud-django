"""Adopters' own operators see the request body, as scoped-rbac's request check
gives it, also where the workflow check authorizes a transition: the
`TransitionCheck` it passes is a read-only mapping over the body."""

from uuid import uuid4

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.models import Resource
from scrud_django.permissions import TransitionCheck

from .factories import UserFactory

TODO = "tests://ToDo"
PP = "tests://PartnerProfiles"
NOT_STARTED = {
    "title": "ok",
    "status": "not started",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
    "rbac_context": "q",
}


def field_is(resource, configuration):
    """An adopter's operator: allows a body whose `field` is `value`."""
    return resource.get(configuration["field"]) == configuration["value"]


@pytest.fixture(autouse=True)
def field_is_operator():
    with override_settings(
        SCOPED_RBAC=dict(
            settings.SCOPED_RBAC,
            OPERATORS={"field_is": field_is},
        )
    ):
        yield


def field_is_grant(type_uri, field):
    return {
        type_uri: {"condition": {"operator": "field_is", "field": field, "value": "ok"}}
    }


def user_with(definition, context="q"):
    role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
    role.save()
    user = UserFactory()
    RoleAssignment(
        user=user, role=role, rbac_context=context, etag=uuid4().hex, modified_at=now()
    ).save()
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_transition_check_is_a_read_only_mapping_over_the_body():
    check = TransitionCheck("start", {"name": "ok"})
    assert check.transition == "start"
    assert check.get("name") == "ok"
    assert dict(check) == {"name": "ok"}
    with pytest.raises(TypeError):
        check["name"] = "x"
    with pytest.raises(AttributeError):
        check.transition = "finish"
    assert TransitionCheck(None).get("name") is None


@pytest.mark.django_db
def test_body_operator_on_a_type_without_a_workflow():
    reverse("todos-list")
    client = user_with({"http.POST": field_is_grant(PP, "name")})
    before = Resource.objects.count()
    response = client.post(
        reverse("partner-profiles-list"),
        {"name": "no", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 403, response.content
    assert Resource.objects.count() == before
    response = client.post(
        reverse("partner-profiles-list"),
        {"name": "ok", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_body_operator_on_a_workflow_type():
    reverse("todos-list")
    definition = {
        "http.GET": [TODO],
        "http.POST": field_is_grant(TODO, "title"),
        "http.PUT": {
            TODO: {
                "condition": {
                    "operator": "scrud_workflow",
                    "authorized_transitions": ["cancel"],
                }
            }
        },
    }
    client = user_with(definition)
    response = client.post(
        reverse("todos-list"), dict(NOT_STARTED, title="no"), format="json"
    )
    assert response.status_code == 403, response.content
    response = client.post(reverse("todos-list"), NOT_STARTED, format="json")
    assert response.status_code == 201, response.content

    # The transitions are still those of the scrud_workflow grant.
    todo = Resource.objects.latest("pk")
    detail = reverse("todos-detail", args=[todo.pk])
    response = client.put(
        detail,
        dict(NOT_STARTED, status="done"),
        format="json",
        HTTP_IF_MATCH=quote_etag(todo.etag),
    )
    assert response.status_code == 400, response.content
    canceled = dict(NOT_STARTED, status="canceled")
    response = client.put(
        detail, canceled, format="json", HTTP_IF_MATCH=quote_etag(todo.etag)
    )
    assert response.status_code == 200, response.content
    assert Resource.objects.get(pk=todo.pk).content == canceled
