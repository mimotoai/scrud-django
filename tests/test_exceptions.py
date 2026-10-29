import json
from uuid import uuid4

import fastjsonschema
import pytest
from django.test import override_settings
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.exceptions import ErrorDetail, NotFound
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework.views import APIView
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.exceptions import (
    JsonValidationError,
    StoredPolicyReference,
    WorkflowInvalidStateError,
    scrud_exception_handler,
)
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)

from .factories import UserFactory


@pytest.fixture
def api_context():
    view = APIView()
    request = Request(APIRequestFactory().get("/"))
    request.accepted_renderer = JSONRenderer()
    request.accepted_media_type = "application/json"
    view.request = request
    return {"request": request, "view": view}


def test_scrud_errors_keep_their_body(api_context):
    exc = WorkflowInvalidStateError(policy="todo")
    response = scrud_exception_handler(exc, api_context)
    assert response.status_code == 400
    assert (
        response.data["type"]
        == "http://api.openteams.com/http-problem/workflow-transition-invalid"
    )
    assert response.data["message"] == "The request does not match a valid state"
    assert response["Content-Type"].startswith("application/problem+json")


def test_other_errors_use_scoped_rbac_problem_details(api_context):
    response = scrud_exception_handler(NotFound(), api_context)
    assert response.status_code == 404
    assert set(response.data) >= {"type", "title", "status"}


# Every scrud error answers 400 under any of these exception handlers: DRF's own,
# scoped-rbac's, and scrud's.
HANDLERS = [
    "rest_framework.views.exception_handler",
    "scoped_rbac.rest.exception_handler",
    "scrud_django.exceptions.scrud_exception_handler",
]

TODO = "tests://ToDo"
NOT_STARTED = {
    "title": "Buy milk",
    "status": "not started",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
}
# A stored policy with no transitions: any change of state is invalid.
NO_TRANSITIONS_POLICY = {
    "resource_type": TODO,
    "initial_states": {"not started": 'status == "not started"'},
    "states": {"in progress": 'status == "in progress"'},
    "transitions": {},
}
# Lets anyone create a ToDo in "", and PUT one only to "cancel".
CANCEL_ONLY = {
    "http.GET": [TODO],
    "http.POST": [TODO],
    "http.PUT": {
        TODO: {
            "condition": {
                "operator": "scrud_workflow",
                "authorized_transitions": ["cancel"],
            }
        }
    },
}


def with_handler(handler):
    from django.conf import settings

    return override_settings(
        REST_FRAMEWORK=dict(settings.REST_FRAMEWORK, EXCEPTION_HANDLER=handler)
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def cancel_only_user():
    role = Role(definition=CANCEL_ONLY, etag=uuid4().hex, modified_at=now())
    role.save()
    user = UserFactory()
    RoleAssignment(
        user=user, role=role, rbac_context="", etag=uuid4().hex, modified_at=now()
    ).save()
    return user


def a_todo():
    reverse("todos-list")
    return ResourceRegistration.register(dict(NOT_STARTED), "todos")


def store_policy(content):
    workflow_policy_resource_type()
    return ResourceRegistration.register(content, "workflow-policies")


def put(user, resource, content):
    return client_for(user).put(
        reverse("todos-detail", args=[resource.pk]),
        content,
        format="json",
        HTTP_IF_MATCH=quote_etag(resource.etag),
    )


@pytest.mark.django_db
@pytest.mark.parametrize("handler", HANDLERS)
def test_json_validation_error_is_400_under_any_handler(handler):
    reverse("todos-list")
    admin = UserFactory(is_staff=True, is_superuser=True)
    with with_handler(handler):
        response = client_for(admin).post(
            reverse("todos-list"), dict(NOT_STARTED, title=5), format="json"
        )
    assert response.status_code == 400, response.content
    json.loads(response.content)


@pytest.mark.django_db
@pytest.mark.parametrize("handler", HANDLERS)
def test_invalid_transition_under_stored_policy_is_400_under_any_handler(handler):
    # The error's detail used to be the policy itself, here a stored one.
    store_policy(NO_TRANSITIONS_POLICY)
    resource = a_todo()
    admin = UserFactory(is_staff=True, is_superuser=True)
    with with_handler(handler):
        response = put(admin, resource, dict(NOT_STARTED, status="in progress"))
    assert response.status_code == 400, response.content
    json.loads(response.content)


@pytest.mark.django_db
@pytest.mark.parametrize("handler", HANDLERS)
def test_unauthorized_transition_is_400_under_any_handler(handler):
    resource = a_todo()
    with with_handler(handler):
        response = put(cancel_only_user(), resource, dict(NOT_STARTED, status="done"))
    assert response.status_code == 400, response.content
    json.loads(response.content)


@pytest.mark.django_db
def test_scrud_handler_body_for_a_stored_policy_error():
    stored = store_policy(NO_TRANSITIONS_POLICY)
    resource = a_todo()
    admin = UserFactory(is_staff=True, is_superuser=True)
    response = put(admin, resource, dict(NOT_STARTED, status="in progress"))
    assert response.status_code == 400
    url = "http://testserver" + reverse("workflow-policies-detail", args=[stored.pk])
    assert response.json() == {
        "type": "http://api.openteams.com/http-problem/workflow-transition-invalid",
        "title": "Invalid workflow transition",
        "detail": url,
        "policy": url,
        "prior_state": "not started",
        "proposed_state": "in progress",
        "message": "Invalid workflow transition",
        "status": 400,
    }


@pytest.mark.django_db
def test_scrud_handler_body_for_a_json_validation_error():
    reverse("todos-list")
    admin = UserFactory(is_staff=True, is_superuser=True)
    response = client_for(admin).post(
        reverse("todos-list"), dict(NOT_STARTED, title=5), format="json"
    )
    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "https://api.openteams.com/http-problem/json-invalid"
    assert body["title"] == "JSON Validation Error"
    assert body["detail"] == "data.title must be string"
    assert body["rule"] == "type"
    assert body["status"] == 400


def test_scrud_errors_carry_an_error_detail_with_a_code():
    exc = WorkflowInvalidStateError(policy=StoredPolicyReference(None))
    assert isinstance(exc.detail, ErrorDetail)
    assert exc.detail.code == "workflow_transition_invalid"
    assert exc.get_codes() == "workflow_transition_invalid"
    json_exc = JsonValidationError(
        fastjsonschema.JsonSchemaValueException(
            "data must be object", value=5, name="data", rule="type"
        )
    )
    assert isinstance(json_exc.detail, ErrorDetail)
    assert json_exc.detail.code == "json_invalid"
    assert str(json_exc.detail) == "data must be object"
