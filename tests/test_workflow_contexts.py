"""Workflow policies apply in every context, to the contexts a resource is in
and the ones a write moves it to.

A context's policy is the stored ``WorkflowPolicy`` for the type in that
context (the most recently modified one if there are several), else the
default policy from ``SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]``. A create
must be a valid initial state under the policy of every context it names. An
update must be a valid transition, and the user must be authorized to make it
(the ``scrud_workflow`` operator), under the policy of every context the
resource is in **and** every context the body names.
"""

import json
from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework import status
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)
from scrud_django.workflow import workflow_policy_for

from .factories import UserFactory
from .fixtures import *  # noqa: F401, F403
from .fixtures import role_todo_creator, todo_creator_login  # noqa: F401
from .steps import *  # noqa: F403, F401

TODO = "tests://ToDo"
WORKFLOW_POLICY = "http://api.openteams.com/json-ld/WorkflowPolicy"
WORKFLOW_ERROR = "http://api.openteams.com/http-problem/workflow-transition-invalid"
ALL_METHODS = ("http.GET", "http.POST", "http.PUT", "http.DELETE")

NOT_STARTED = {
    "title": "Buy milk",
    "status": "not started",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
}

# Any content is a valid initial state, and stays in it.
PERMISSIVE_POLICY = {
    "resource_type": TODO,
    "initial_states": {"any": "true"},
    "states": {},
    "transitions": {},
}

# The ``role_todo_creator`` role: GET and POST ToDos, PUT only to "cancel".
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

TODO_WRITER = {method: [TODO] for method in ALL_METHODS}


def assign(user, definition, context):
    role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
    role.save()
    RoleAssignment(
        user=user, role=role, rbac_context=context, etag=uuid4().hex, modified_at=now()
    ).save()
    return user


def user_with(*grants):
    """A user holding each ``(role definition, context)`` of `grants`."""
    user = UserFactory()
    for definition, context in grants:
        assign(user, definition, context)
    return user


def todo(**content):
    reverse("todos-list")  # registers the test types' routes
    return ResourceRegistration.register(dict(NOT_STARTED, **content), "todos")


def store_policy(content):
    workflow_policy_resource_type()
    return ResourceRegistration.register(content, "workflow-policies")


def put_todo(resource, content):
    When.http.put(
        reverse("todos-detail", args=[resource.id]),
        data=json.dumps(content),
        content_type="application/json",
        HTTP_IF_MATCH=quote_etag(resource.etag),
    )


def post_todo(content):
    When.http.post(
        reverse("todos-list"), data=json.dumps(content), content_type="application/json"
    )


def assert_workflow_error():
    assert Http.response.json()["type"] == WORKFLOW_ERROR, Http.response.json()


def assert_unchanged(resource):
    stored = Resource.objects.get(pk=resource.pk)
    assert stored.content == resource.content
    assert stored.etag == resource.etag


@pytest.mark.django_db
@test_context
def test_context_hop_is_refused(todo_creator_login):
    # Cancel-only in "", but a ToDo writer in "w".
    assign(todo_creator_login, TODO_WRITER, "w")
    resource = todo()
    Given.http.force_authenticate(todo_creator_login)
    put_todo(resource, dict(NOT_STARTED, status="done", rbac_context="w"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    assert_unchanged(resource)


@pytest.mark.django_db
@test_context
def test_context_hop_into_a_restricted_context_is_refused():
    # A ToDo writer in "a", cancel-only in "b": finishing it while moving it
    # into "b" needs "finish" in "b" too.
    user = user_with((TODO_WRITER, "a"), (CANCEL_ONLY, "b"))
    resource = todo(rbac_context="a")
    Given.http.force_authenticate(user)
    put_todo(resource, dict(NOT_STARTED, status="done", rbac_context="b"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    assert_unchanged(resource)


@pytest.mark.django_db
@test_context
def test_move_between_contexts_that_allow_the_transition():
    user = user_with((TODO_WRITER, "a"), (TODO_WRITER, "b"))
    resource = todo(rbac_context="a")
    moved = dict(NOT_STARTED, status="in progress", rbac_context="b")
    Given.http.force_authenticate(user)
    put_todo(resource, moved)
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Resource.objects.get(pk=resource.pk).content == moved


@pytest.mark.django_db
@test_context
def test_default_policy_authorizes_transitions_in_other_contexts():
    user = user_with((CANCEL_ONLY, "w"))
    resource = todo(rbac_context="w")
    Given.http.force_authenticate(user)
    put_todo(resource, dict(NOT_STARTED, status="done", rbac_context="w"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    assert_unchanged(resource)

    canceled = dict(NOT_STARTED, status="canceled", rbac_context="w")
    put_todo(resource, canceled)
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Resource.objects.get(pk=resource.pk).content == canceled


@pytest.mark.django_db
@test_context
def test_default_policy_checks_initial_state_in_other_contexts():
    user = user_with((CANCEL_ONLY, "w"))
    before = Resource.objects.count()
    Given.http.force_authenticate(user)
    post_todo(dict(NOT_STARTED, status="done", rbac_context="w"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    assert Resource.objects.count() == before

    post_todo(dict(NOT_STARTED, rbac_context="w"))
    Then.http.status_code_is(status.HTTP_201_CREATED)


@pytest.mark.django_db
@test_context
def test_default_policy_checks_transitions_in_other_contexts():
    user = user_with((TODO_WRITER, "w"))
    resource = todo(rbac_context="w", status="done")
    Given.http.force_authenticate(user)
    put_todo(resource, dict(NOT_STARTED, rbac_context="w"))  # done -> not started
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    assert_unchanged(resource)


@pytest.mark.django_db
@test_context
def test_stored_policy_applies_only_in_its_own_context(regular_login):
    reverse("todos-list")
    attacker = user_with((True, "attacker"))
    Given.http.force_authenticate(attacker)
    When.http.post(
        "/workflow-policies/",
        data=json.dumps(dict(PERMISSIVE_POLICY, rbac_context="attacker")),
        content_type="application/json",
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)

    # It applies in "attacker": "done" is an initial state there.
    post_todo(dict(NOT_STARTED, status="done", rbac_context="attacker"))
    Then.http.status_code_is(status.HTTP_201_CREATED)

    # It does not apply in "": the default policy still forbids "done".
    before = Resource.objects.count()
    Given.http.force_authenticate(regular_login)
    post_todo(dict(NOT_STARTED, status="done"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    post_todo(dict(NOT_STARTED, status="done", rbac_context=""))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    assert Resource.objects.count() == before

    post_todo(dict(NOT_STARTED))
    Then.http.status_code_is(status.HTTP_201_CREATED)


@pytest.mark.django_db
@test_context
def test_writes_in_other_contexts_work_once_a_policy_is_stored():
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="attacker"))
    user = user_with((TODO_WRITER, "other"))
    Given.http.force_authenticate(user)
    post_todo(dict(NOT_STARTED, rbac_context="other"))
    Then.http.status_code_is(status.HTTP_201_CREATED)
    resource = Resource.objects.order_by("-pk").first()

    progressing = dict(NOT_STARTED, status="in progress", rbac_context="other")
    put_todo(resource, progressing)
    Then.http.status_code_is(status.HTTP_200_OK)

    post_todo(dict(NOT_STARTED, status="done", rbac_context="other"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()


@pytest.mark.django_db
@test_context
def test_hop_out_of_a_context_validates_against_its_policy():
    # A permissive policy in "attacker" does not let a resource in "" leave a
    # state the default policy has no transition out of.
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="attacker"))
    user = user_with((True, DEFAULT_CONTEXT), (True, "attacker"))
    resource = todo(status="done")
    Given.http.force_authenticate(user)
    put_todo(resource, dict(NOT_STARTED, rbac_context="attacker"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    assert_unchanged(resource)


@pytest.mark.django_db
def test_policy_lookup_is_per_context():
    reverse("todos-list")
    default = workflow_policy_for(TODO, DEFAULT_CONTEXT)
    assert default is not None
    assert workflow_policy_for(TODO, "attacker").policy == default.policy
    assert workflow_policy_for("tests://NoWorkflow", "attacker") is None

    store_policy(dict(PERMISSIVE_POLICY, rbac_context="attacker"))
    attacker = workflow_policy_for(TODO, "attacker")
    assert attacker.policy["initial_states"] == {"any": "true"}
    assert workflow_policy_for(TODO, DEFAULT_CONTEXT).policy == default.policy
    assert workflow_policy_for(TODO, "other").policy == default.policy


@pytest.mark.django_db
def test_policy_lookup_reads_the_policy_contexts_as_resources_do():
    # A stored policy with no ``rbac_context`` is in the default context.
    store_policy(dict(PERMISSIVE_POLICY))
    assert workflow_policy_for(TODO, DEFAULT_CONTEXT).policy["initial_states"] == {
        "any": "true"
    }
    assert workflow_policy_for(TODO, "other").policy["initial_states"] != {
        "any": "true"
    }


@pytest.mark.django_db
def test_most_recently_modified_policy_wins():
    older = store_policy(
        dict(PERMISSIVE_POLICY, rbac_context="p", initial_states={"older": "true"})
    )
    store_policy(
        dict(PERMISSIVE_POLICY, rbac_context="p", initial_states={"newer": "true"})
    )
    assert workflow_policy_for(TODO, "p").policy["initial_states"] == {"newer": "true"}

    ResourceRegistration.update(dict(older.content), "workflow-policies", str(older.pk))
    assert workflow_policy_for(TODO, "p").policy["initial_states"] == {"older": "true"}


INVALID_POLICIES = [
    pytest.param(
        dict(PERMISSIVE_POLICY, initial_states={"draft": "status == "}),
        id="invalid-initial-state-condition",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, states={"done": "status == "}),
        id="invalid-state-condition",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, transitions={"go": {"from": "any", "to": "nowhere"}}),
        id="transition-to-undefined-state",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, transitions={"go": [{"from": "any", "to": "any"}]}),
        id="transition-list",
    ),
]


@pytest.mark.django_db
@pytest.mark.parametrize("policy", INVALID_POLICIES)
@test_context
def test_invalid_policy_is_refused_on_create(regular_login, policy):
    reverse("todos-list")
    before = Resource.objects.count()
    Given.http.force_authenticate(regular_login)
    When.http.post(
        "/workflow-policies/", data=json.dumps(policy), content_type="application/json"
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert "workflow_policy" in Http.response.json()["errors"], Http.response.json()
    assert Resource.objects.count() == before

    # Writes of the type still work.
    post_todo(dict(NOT_STARTED))
    Then.http.status_code_is(status.HTTP_201_CREATED)


@pytest.mark.django_db
@pytest.mark.parametrize("policy", INVALID_POLICIES)
@test_context
def test_invalid_policy_is_refused_on_update(regular_login, policy):
    stored = store_policy(dict(PERMISSIVE_POLICY))
    Given.http.force_authenticate(regular_login)
    When.http.put(
        f"/workflow-policies/{stored.pk}/",
        data=json.dumps(policy),
        content_type="application/json",
        HTTP_IF_MATCH=quote_etag(stored.etag),
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert "workflow_policy" in Http.response.json()["errors"], Http.response.json()
    assert_unchanged(stored)
