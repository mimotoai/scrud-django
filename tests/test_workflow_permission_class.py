"""The workflow check authorizes a transition through the view's permission
class (each ``IsAuthorized`` of its permissions), as the object and post-save
checks do, so that an ``IsAuthorized`` subclass's ``has_context_permission``
decides it too: it gets the `TransitionCheck` of the transition in each
context."""

from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory

TODO = "tests://ToDo"
IN_PROGRESS = {
    "title": "Buy milk",
    "status": "in progress",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
}
UNAUTHORIZED = (
    "The user isn't authorized to perform this state transition on this resource."
)
TRANSITION_INVALID = "http://api.openteams.com/http-problem/workflow-transition-invalid"


def a_todo(**content):
    reverse("todos-list")
    return ResourceRegistration.register(dict(IN_PROGRESS, **content), "todos")


def client_with(definition, context):
    user = UserFactory()
    role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
    role.save()
    RoleAssignment(
        user=user, role=role, rbac_context=context, etag=uuid4().hex, modified_at=now()
    ).save()
    client = APIClient()
    client.force_authenticate(user)
    return client


def todo_writer(context):
    return client_with(
        {m: [TODO] for m in ("http.GET", "http.POST", "http.PUT", "http.DELETE")},
        context,
    )


@pytest.mark.django_db
def test_a_subclass_refusing_a_transition_is_asked_by_the_workflow_check():
    # tests/urls.py: NoCancelIsAuthorized never allows "cancel".
    todo = a_todo()
    client = todo_writer("")
    url = reverse("no-cancel-todos-detail", args=[todo.pk])
    response = client.put(
        url,
        dict(IN_PROGRESS, status="canceled"),
        format="json",
        HTTP_IF_MATCH=quote_etag(todo.etag),
    )
    assert response.status_code == 400, response.content
    body = response.json()
    assert body["type"] == TRANSITION_INVALID, body
    assert body["message"] == UNAUTHORIZED, body
    assert (body["prior_state"], body["proposed_state"]) == ("in progress", "canceled")
    assert Resource.objects.get(pk=todo.pk).content == IN_PROGRESS

    # Another transition is the user's grants' to decide.
    response = client.put(
        url,
        dict(IN_PROGRESS, status="done"),
        format="json",
        HTTP_IF_MATCH=quote_etag(todo.etag),
    )
    assert response.status_code == 200, response.content

    # The plain route lets the same user cancel.
    other = a_todo()
    response = client.put(
        reverse("todos-detail", args=[other.pk]),
        dict(IN_PROGRESS, status="canceled"),
        format="json",
        HTTP_IF_MATCH=quote_etag(other.etag),
    )
    assert response.status_code == 200, response.content


@pytest.mark.django_db
def test_a_subclass_refusing_a_context_is_asked_by_the_workflow_check():
    # tests/urls.py: ContextDenyingIsAuthorized never allows the context
    # "denied", where the user's grants allow everything. The workflow check
    # asks it for the creation (__INITIALIZE__) there and refuses the write
    # before it is saved (formerly only the post-save check refused it, 403).
    a_todo()
    client = todo_writer("denied")
    before = Resource.objects.count()
    response = client.post(
        reverse("denying-todos-list"),
        dict(IN_PROGRESS, rbac_context="denied"),
        format="json",
    )
    assert response.status_code == 400, response.content
    body = response.json()
    assert body["type"] == TRANSITION_INVALID, body
    assert body["message"] == UNAUTHORIZED, body
    assert (body["prior_state"], body["proposed_state"]) == (None, "in progress")
    assert Resource.objects.count() == before

    response = client.post(
        reverse("todos-list"), dict(IN_PROGRESS, rbac_context="denied"), format="json"
    )
    assert response.status_code == 201, response.content
