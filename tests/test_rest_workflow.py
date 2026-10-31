import json

import pytest
from rest_framework import status
from rest_framework.reverse import reverse
from safetydance_django.test import *

from scrud_django.registration import ResourceRegistration

from .fixtures import *  # noqa
from .fixtures import role_todo_creator, todo_creator_login  # noqa: F401
from .steps import *


@pytest.mark.django_db
@test_context
def test_invalid_initial_state(admin_login):
    # TODO check the error response body for expected messages
    Given.http.force_authenticate(admin_login)
    When.http.post(
        reverse("todos-list"),
        data=json.dumps(
            {
                "title": "Buy milk",
                "status": "done",
                "priority": "medium",
                "assignee": "http://somewhere.example/someone",
            }
        ),
        content_type="application/json",
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)


@pytest.mark.django_db
@test_context
def test_valid_initial_state(admin_login):
    Given.http.force_authenticate(admin_login)
    When.http.post(
        reverse("todos-list"),
        data=json.dumps(
            {
                "title": "Buy milk",
                "status": "not started",
                "priority": "medium",
                "assignee": "http://somewhere.example/someone",
            }
        ),
        content_type="application/json",
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)


@pytest.mark.django_db
@test_context
def test_invalid_state(admin_login):
    # TODO check the error response body for expected messages
    Given.http.force_authenticate(admin_login)
    When.http.post(
        reverse("todos-list"),
        data=json.dumps(
            {
                "title": "Buy milk",
                "status": "bogus status!!",
                "priority": "medium",
                "assignee": "http://somewhere.example/someone",
            }
        ),
        content_type="application/json",
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)

    When.http.post(
        reverse("todos-list"),
        data=json.dumps(
            {
                "title": "Buy milk",
                "status": "in progress",
                "priority": "not rated",
                "assignee": "http://somewhere.example/someone",
            }
        ),
        content_type="application/json",
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)

    When.http.post(
        reverse("todos-list"),
        data=json.dumps(
            {
                "title": "Buy milk",
                "status": "in progress",
                "priority": None,
                "assignee": "http://somewhere.example/someone",
            }
        ),
        content_type="application/json",
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)


@pytest.mark.django_db
@test_context
def test_invalid_transition(admin_login):
    # TODO check the error response body for expected messages
    data = {
        "title": "Buy milk",
        "status": "not started",
        "priority": "medium",
        "assignee": "http://somewhere.example/someone",
    }
    Given.http.force_authenticate(admin_login)
    When.http.post(
        reverse("todos-list"), data=json.dumps(data), content_type="application/json"
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)

    url = Http.response["Location"]
    Given.http.head(url)
    When.http.put(
        url,
        data=json.dumps(data),  # reflexive state transitions not allowed in this policy
        content_type="application/json",
        HTTP_IF_UNMODIFIED_SINCE=Http.response["Last-Modified"],
        HTTP_IF_MATCH=Http.response["Etag"],
    )
    Then.http.status_code_is(status.HTTP_200_OK)

    data["status"] = "in progress"
    Given.http.head(url)
    When.http.put(  # transition to "in progress"
        url,
        data=json.dumps(data),
        content_type="application/json",
        HTTP_IF_UNMODIFIED_SINCE=Http.response["Last-Modified"],
        HTTP_IF_MATCH=Http.response["Etag"],
    )
    Then.http.status_code_is(status.HTTP_200_OK)

    data["status"] = "not started"
    Given.http.head(url)
    When.http.put(  # invalid transition to "not started" from "in progress"
        url,
        data=json.dumps(data),
        content_type="application/json",
        HTTP_IF_UNMODIFIED_SINCE=Http.response["Last-Modified"],
        HTTP_IF_MATCH=Http.response["Etag"],
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)


@pytest.mark.django_db
@test_context
def test_valid_transition(admin_login):
    data = {
        "title": "Buy milk",
        "status": "not started",
        "priority": "medium",
        "assignee": "http://somewhere.example/someone",
    }
    Given.http.force_authenticate(admin_login)
    When.http.post(
        reverse("todos-list"), data=json.dumps(data), content_type="application/json"
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)
    url = Http.response["Location"]

    data["status"] = "in progress"
    Given.http.head(url)
    When.http.put(  # transition to "in progress"
        url,
        data=json.dumps(data),
        content_type="application/json",
        HTTP_IF_UNMODIFIED_SINCE=Http.response["Last-Modified"],
        HTTP_IF_MATCH=Http.response["Etag"],
    )
    Then.http.status_code_is(status.HTTP_200_OK)

    data["status"] = "blocked"
    data["blockers"] = [{"explanation": "Because I said so, perhaps..."}]
    Given.http.head(url)
    When.http.put(  # invalid transition to "not started" from "in progress"
        url,
        data=json.dumps(data),
        content_type="application/json",
        HTTP_IF_UNMODIFIED_SINCE=Http.response["Last-Modified"],
        HTTP_IF_MATCH=Http.response["Etag"],
    )
    Then.http.status_code_is(status.HTTP_200_OK)


@pytest.mark.django_db
@test_context
def test_unauthorized_transition(todo_creator_login):
    data = {
        "title": "Buy milk",
        "status": "not started",
        "priority": "medium",
        "assignee": "http://somewhere.example/someone",
    }
    Given.http.force_authenticate(todo_creator_login)
    When.http.post(
        reverse("todos-list"), data=json.dumps(data), content_type="application/json"
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)
    url = Http.response["Location"]

    data["status"] = "in progress"
    Given.http.head(url)
    When.http.put(  # transition to "in progress"
        url,
        data=json.dumps(data),
        content_type="application/json",
        HTTP_IF_UNMODIFIED_SINCE=Http.response["Last-Modified"],
        HTTP_IF_MATCH=Http.response["Etag"],
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)


@pytest.mark.django_db
@test_context
def test_authorized_transition(todo_creator_login):
    data = {
        "title": "Buy milk",
        "status": "not started",
        "priority": "medium",
        "assignee": "http://somewhere.example/someone",
    }
    Given.http.force_authenticate(todo_creator_login)
    When.http.post(
        reverse("todos-list"), data=json.dumps(data), content_type="application/json"
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)
    url = Http.response["Location"]

    data["status"] = "canceled"
    Given.http.head(url)
    When.http.put(  # transition to "in progress"
        url,
        data=json.dumps(data),
        content_type="application/json",
        HTTP_IF_UNMODIFIED_SINCE=Http.response["Last-Modified"],
        HTTP_IF_MATCH=Http.response["Etag"],
    )
    Then.http.status_code_is(status.HTTP_200_OK)


# ``"rbac_context": ""`` names the default context, exactly as leaving the key
# out does: the default workflow policy and its transition checks apply to both.
EMPTY_CONTEXT_TODO = {
    "title": "Buy milk",
    "status": "not started",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
    "rbac_context": "",
}


@pytest.mark.django_db
@test_context
def test_invalid_initial_state_with_empty_rbac_context(admin_login):
    Given.http.force_authenticate(admin_login)
    When.http.post(
        reverse("todos-list"),
        data=json.dumps(dict(EMPTY_CONTEXT_TODO, status="done")),
        content_type="application/json",
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)


@pytest.mark.django_db
@test_context
def test_unauthorized_transition_with_empty_rbac_context(todo_creator_login):
    data = dict(EMPTY_CONTEXT_TODO)
    Given.http.force_authenticate(todo_creator_login)
    When.http.post(
        reverse("todos-list"), data=json.dumps(data), content_type="application/json"
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)
    url = Http.response["Location"]

    data["status"] = "done"  # "finish": this role may only "cancel"
    Given.http.head(url)
    When.http.put(
        url,
        data=json.dumps(data),
        content_type="application/json",
        HTTP_IF_UNMODIFIED_SINCE=Http.response["Last-Modified"],
        HTTP_IF_MATCH=Http.response["Etag"],
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)


@pytest.mark.django_db
@test_context
def test_invalid_transition_with_empty_rbac_context(admin_login):
    done = ResourceRegistration.register(
        dict(EMPTY_CONTEXT_TODO, status="done"), "todos"
    )
    url = reverse("todos-detail", args=[done.id])
    Given.http.force_authenticate(admin_login)
    Given.http.head(url)
    When.http.put(  # no transition leads from "done" back to "not started"
        url,
        data=json.dumps(EMPTY_CONTEXT_TODO),
        content_type="application/json",
        HTTP_IF_UNMODIFIED_SINCE=Http.response["Last-Modified"],
        HTTP_IF_MATCH=Http.response["Etag"],
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
