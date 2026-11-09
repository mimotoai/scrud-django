"""Workflow policies apply in every context, to the contexts a resource is in
and the ones a write moves it to.

A context's policy is the stored ``WorkflowPolicy`` for the type in that
context (there is at most one), else the default policy from ``SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]``. A create
must be a valid initial state under the policy of every context it names. An
update must be a valid transition, and the user must be authorized to make it
(the ``scrud_workflow`` operator), under the policy of every context the
resource is in **and** every context the body names.
"""

import json
import logging
from uuid import uuid4

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework import status
from rest_framework.test import APIClient
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.models import Resource, ResourceType
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
WORKFLOW_POLICY_INVALID = (
    "https://api.openteams.com/http-problem/workflow-policy-invalid"
)
JSON_INVALID = "https://api.openteams.com/http-problem/json-invalid"
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


def policy_conditions(policy):
    """The CEL conditions of the policy content `policy`."""
    conditions = []
    for key in ("initial_states", "states"):
        states = policy.get(key) or {}
        if isinstance(states, dict):
            conditions.extend(c for c in states.values() if isinstance(c, str))
    return conditions


def every_known_policy():
    """The settings default policies and every stored ``WorkflowPolicy``."""
    from scrud_django.conf import default_workflow_policies

    policies = [p.policy for p in default_workflow_policies().values()]
    policies.extend(
        r.content
        for r in Resource.objects.filter(resource_type__type_uri=WORKFLOW_POLICY)
        if isinstance(r.content, dict)
    )
    return policies


def strings_and_keys_of(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings_and_keys_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_and_keys_of(item)
    elif isinstance(value, str):
        yield value


def assert_no_policy_content(body):
    """No workflow error body carries a policy's content: none of its CEL
    conditions and no ``initial_states``, ``states`` or ``transitions``,
    whichever policy (default or stored, of any context) caused it."""
    conditions = {c for p in every_known_policy() for c in policy_conditions(p)}
    assert conditions, "no policy to compare with"
    for text in strings_and_keys_of(body):
        assert text not in ("initial_states", "states", "transitions"), body
        for condition in conditions:
            assert condition not in text, (condition, body)


def assert_workflow_error():
    body = Http.response.json()
    assert body["type"] == WORKFLOW_ERROR, body
    assert_no_policy_content(body)


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


def workflow_writer(*transitions):
    """GET ToDos, and PUT one only to make `transitions`."""
    return {
        "http.GET": [TODO],
        "http.PUT": {
            TODO: {
                "condition": {
                    "operator": "scrud_workflow",
                    "authorized_transitions": list(transitions),
                }
            }
        },
    }


@pytest.mark.django_db
@test_context
def test_moving_into_a_context_is_checked_as_a_creation_there():
    # The follow-up's example: __SAME_STATE__ (and __INITIALIZE__) in "v", but
    # not "finish"; a writer in "w". "done" -> "done" is __SAME_STATE__ under
    # "v"'s policy, but "done" is no initial state there: nobody in "v"
    # finished it, so it may not be carried in.
    user = user_with(
        (TODO_WRITER, "w"), (workflow_writer("__SAME_STATE__", "__INITIALIZE__"), "v")
    )
    resource = todo(rbac_context="w", status="done")
    Given.http.force_authenticate(user)
    put_todo(resource, dict(NOT_STARTED, status="done", rbac_context="v"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    body = Http.response.json()
    assert body["message"] == "The request is not a valid initial state.", body
    assert (body["prior_state"], body["proposed_state"]) == (None, None), body
    assert_unchanged(resource)


@pytest.mark.django_db
@test_context
def test_moving_an_initial_state_into_a_context_is_allowed():
    user = user_with(
        (TODO_WRITER, "w"), (workflow_writer("__SAME_STATE__", "__INITIALIZE__"), "v")
    )
    resource = todo(rbac_context="w")
    moved = dict(NOT_STARTED, rbac_context="v")
    Given.http.force_authenticate(user)
    put_todo(resource, moved)
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Resource.objects.get(pk=resource.pk).content == moved


@pytest.mark.django_db
@test_context
def test_moving_into_a_context_needs_initialize_there():
    # __SAME_STATE__ alone in "v": entering "v" is a creation there, which
    # needs __INITIALIZE__.
    user = user_with((TODO_WRITER, "w"), (workflow_writer("__SAME_STATE__"), "v"))
    resource = todo(rbac_context="w")
    Given.http.force_authenticate(user)
    put_todo(resource, dict(NOT_STARTED, rbac_context="v"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    body = Http.response.json()
    assert body["message"] == (
        "The user isn't authorized to perform this state transition on this resource."
    ), body
    assert (body["prior_state"], body["proposed_state"]) == (None, "not started")
    assert_unchanged(resource)


@pytest.mark.django_db
@test_context
def test_staying_in_a_context_is_still_a_transition_there():
    # Stored in "v" and "w" (server-side), the body keeps "v": "v" is not
    # entered, so __SAME_STATE__ there suffices for a "done" ToDo.
    user = user_with((TODO_WRITER, "w"), (workflow_writer("__SAME_STATE__"), "v"))
    resource = todo(rbac_context=["v", "w"], status="done")
    kept = dict(NOT_STARTED, status="done", rbac_context="v", title="Buy oat milk")
    Given.http.force_authenticate(user)
    put_todo(resource, kept)
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Resource.objects.get(pk=resource.pk).content == kept


@pytest.fixture
def reported_transitions():
    """The ``transition_data`` of each ToDo ``scrud_post_save``, in order."""
    from scrud_django.scrud_signals import scrud_post_save

    reported = []

    def record(sender, resource_type_uri=None, transition_data=None, **kwargs):
        if resource_type_uri == TODO:
            reported.append(transition_data)

    scrud_post_save.connect(record, weak=False, dispatch_uid="test-reported")
    try:
        yield reported
    finally:
        scrud_post_save.disconnect(dispatch_uid="test-reported")


@pytest.mark.django_db
@test_context
def test_a_move_into_a_context_is_reported_as_a_creation_there(
    reported_transitions,
):
    # scrud_post_save reports the transition under the policy of the first
    # context the content names: entered, it is a creation there, as the
    # workflow check checked it.
    from scrud_django.workflow import TransitionData

    user = user_with((TODO_WRITER, "w"), (TODO_WRITER, "v"))
    resource = todo(rbac_context="w")
    Given.http.force_authenticate(user)
    reported_transitions.clear()
    put_todo(resource, dict(NOT_STARTED, rbac_context="v"))
    Then.http.status_code_is(status.HTTP_200_OK)
    assert reported_transitions == [
        TransitionData("not started", None, "__INITIALIZE__")
    ]

    # Staying in "v" is a transition there again.
    resource.refresh_from_db()
    reported_transitions.clear()
    put_todo(resource, dict(NOT_STARTED, status="in progress", rbac_context="v"))
    Then.http.status_code_is(status.HTTP_200_OK)
    assert reported_transitions == [
        TransitionData("in progress", "not started", "start")
    ]


# States whose conditions overlap: a "high" priority ToDo that is "in
# progress" meets the conditions of both "published" and "review". Stored
# in a JSON column, the policy's states may come back in either order
# (PostgreSQL's jsonb and MySQL's JSON reorder keys): such content is
# ambiguous (no one state), whatever the database.
OVERLAPPING_POLICY = {
    "resource_type": TODO,
    "initial_states": {"draft": 'status == "not started" && priority != "high"'},
    "states": {
        "published": 'priority == "high"',
        "review": 'status == "in progress"',
    },
    "transitions": {
        "submit": {"from": "draft", "to": "review"},
        "publish": {"from": "review", "to": "published"},
    },
}
AMBIGUOUS = dict(NOT_STARTED, status="in progress", priority="high")


def ambiguous_state_message(content):
    return (
        f"{content} meets the conditions of more than one workflow state "
        "('published', 'review'); a state's condition must exclude the others'."
    )


@pytest.mark.django_db
@test_context
def test_a_transition_is_authorized_into_the_state_the_content_is_stored_in(
    reported_transitions,
):
    # The body is "in progress" ("review", where "submit" leads) and "high"
    # priority ("published"): it is in no one state, so a writer who may
    # "submit" may not store it.
    from scrud_django.workflow import TransitionData

    stored = store_policy(dict(OVERLAPPING_POLICY, rbac_context="w"))
    user = user_with((workflow_writer("submit"), "w"))
    resource = todo(rbac_context="w")
    Given.http.force_authenticate(user)
    reported_transitions.clear()
    put_todo(resource, dict(AMBIGUOUS, rbac_context="w"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    # The writer may not read the policy: the error names neither it nor
    # the states the body meets.
    assert_generic_workflow_error(stored)
    assert_unchanged(resource)
    assert reported_transitions == []

    submitted = dict(NOT_STARTED, status="in progress", rbac_context="w")
    put_todo(resource, submitted)
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Resource.objects.get(pk=resource.pk).content == submitted
    assert reported_transitions == [TransitionData("review", "draft", "submit")]


@pytest.mark.django_db
@test_context
def test_content_in_more_than_one_state_is_refused_naming_them():
    # A writer who may read the policy is told which states the body meets.
    stored = store_policy(dict(OVERLAPPING_POLICY, rbac_context="w"))
    user = user_with((workflow_writer("submit", "publish"), "w"), (READ_POLICIES, "w"))
    resource = todo(rbac_context="w")
    Given.http.force_authenticate(user)
    put_todo(resource, dict(AMBIGUOUS, rbac_context="w"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_references_stored_policy(stored)
    assert_no_policy_content(Http.response.json())
    body = Http.response.json()
    assert body["matching_states"] == ["published", "review"], body
    assert body["message"] == ambiguous_state_message("The request"), body
    assert (body["prior_state"], body["proposed_state"]) == (None, None), body
    assert_unchanged(resource)


@pytest.mark.django_db
@test_context
def test_a_resource_stored_in_more_than_one_state_cannot_be_updated(
    reported_saves,
):
    from scrud_django.exceptions import WorkflowAmbiguousStateError
    from scrud_django.workflow import TransitionData

    stored = store_policy(dict(OVERLAPPING_POLICY, rbac_context="w"))
    # Stored server-side, past the workflow check: reported with the error.
    reported_saves.clear()
    resource = todo(**dict(AMBIGUOUS, rbac_context="w"))
    (reported,) = reported_saves
    assert reported["transition_data"] is None
    error = reported["transition_error"]
    assert isinstance(error, WorkflowAmbiguousStateError), error
    assert error.matching_states == ["published", "review"]
    assert reported["transitions"] == {"w": None}

    # Its state is ambiguous: no update leaves it, not even into a state it
    # meets that a transition the writer may make leads to.
    user = user_with((workflow_writer("submit", "publish"), "w"), (READ_POLICIES, "w"))
    Given.http.force_authenticate(user)
    reported_saves.clear()
    put_todo(
        resource, dict(NOT_STARTED, status="done", priority="high", rbac_context="w")
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_references_stored_policy(stored)
    body = Http.response.json()
    assert body["matching_states"] == ["published", "review"], body
    assert body["message"] == ambiguous_state_message("The stored resource"), body
    assert_unchanged(resource)
    assert reported_saves == []

    # A server-side move out of "w" reports no state left there.
    resource.content = dict(NOT_STARTED, rbac_context="a")
    resource.save()
    (reported,) = reported_saves
    assert reported["transitions"]["w"] is None
    # Entering "a" (the default policy) is a creation there, the context
    # transition_data reports: valid, so no transition_error.
    initialized = TransitionData("not started", None, "__INITIALIZE__")
    assert reported["transitions"]["a"] == initialized
    assert reported["transition_data"] == initialized
    assert reported["transition_error"] is None


def put_moving_an_ambiguous_resource_out_of_its_context(user, reported_saves):
    """A PUT by `user` moving a ToDo stored (server-side) in "w", whose
    content meets two states of "w"'s policy, into "a", where its new
    content is a valid creation: refused, as the stored content's state in
    "w" is ambiguous. Returns the stored policy and the resource."""
    stored = store_policy(dict(OVERLAPPING_POLICY, rbac_context="w"))
    resource = todo(**dict(AMBIGUOUS, rbac_context="w"))
    Given.http.force_authenticate(user)
    reported_saves.clear()
    put_todo(resource, dict(NOT_STARTED, rbac_context="a"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_unchanged(resource)
    assert reported_saves == []
    return stored


@pytest.mark.django_db
@test_context
def test_an_ambiguous_stored_resource_cannot_be_moved_out_of_its_context(
    reported_saves,
):
    # A writer who may read "w"'s policy is told the stored resource is
    # ambiguous, and in which states.
    user = user_with((TODO_WRITER, "w"), (TODO_WRITER, "a"), (READ_POLICIES, "w"))
    stored = put_moving_an_ambiguous_resource_out_of_its_context(user, reported_saves)
    assert_references_stored_policy(stored)
    body = Http.response.json()
    assert body["matching_states"] == ["published", "review"], body
    assert body["message"] == ambiguous_state_message("The stored resource"), body
    assert (body["prior_state"], body["proposed_state"]) == (None, None), body


@pytest.mark.django_db
@test_context
def test_an_ambiguous_stored_resource_moved_out_by_a_non_reader_is_generic(
    reported_saves,
):
    # A writer who may not read "w"'s policy gets the generic error.
    user = user_with((TODO_WRITER, "w"), (TODO_WRITER, "a"))
    stored = put_moving_an_ambiguous_resource_out_of_its_context(user, reported_saves)
    assert_generic_workflow_error(stored)


@pytest.mark.django_db
@test_context
def test_an_ambiguous_stored_resource_is_hidden_from_a_writer_who_cannot_read_it(
    reported_saves,
):
    # The writer may read "w"'s policy, but not the stored resource: the
    # states its stored content meets would tell of that content, so the
    # error is the generic one, naming no state and no policy.
    stored = store_policy(dict(OVERLAPPING_POLICY, rbac_context="w"))
    resource = todo(**dict(AMBIGUOUS, rbac_context="w"))
    put_only = {"http.PUT": workflow_writer("submit", "publish")["http.PUT"]}
    user = user_with((put_only, "w"), (READ_POLICIES, "w"))
    Given.http.force_authenticate(user)
    reported_saves.clear()
    put_todo(
        resource, dict(NOT_STARTED, status="done", priority="high", rbac_context="w")
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_generic_workflow_error(stored)
    body = Http.response.json()
    assert "matching_states" not in body, body
    content = Http.response.content.decode()
    for state in ("published", "review", "draft"):
        assert state not in content, content
    assert_unchanged(resource)
    assert reported_saves == []


# "done" and "canceled" may not change in place: "reopen" leads to "done"
# from both (a self-loop of "done" only through its list), "amend" from
# "canceled" to itself (one state on each side: an edit in place).
RESTRICTED_POLICY = {
    "resource_type": TODO,
    "initial_states": {"not started": 'status == "not started"'},
    "states": {"done": 'status == "done"', "canceled": 'status == "canceled"'},
    "transitions": {
        "reopen": {"from": ["done", "canceled"], "to": "done"},
        "amend": {"from": "canceled", "to": "canceled"},
    },
    "restrict_update": ["done", "canceled"],
}


@pytest.mark.django_db
@test_context
def test_a_restricted_state_is_edited_in_place_only_by_its_own_self_loop():
    store_policy(dict(RESTRICTED_POLICY, rbac_context="w"))
    done = todo(status="done", rbac_context="w")
    # The writer may read the policy, so the error says why.
    reader = ({"http.GET": [WORKFLOW_POLICY]}, "w")
    Given.http.force_authenticate(user_with((workflow_writer("reopen"), "w"), reader))
    put_todo(done, dict(done.content, title="edited"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    assert Http.response.json()["message"] == "Update not allowed on this state"
    assert_unchanged(done)

    # "reopen" still leads from "canceled" to "done".
    canceled = todo(status="canceled", rbac_context="w")
    put_todo(canceled, dict(canceled.content, status="done"))
    Then.http.status_code_is(status.HTTP_200_OK)

    # "amend" names "canceled" alone on each side: its holders edit in place.
    canceled = todo(status="canceled", rbac_context="w")
    edited = dict(canceled.content, title="edited")
    Given.http.force_authenticate(user_with((workflow_writer("amend"), "w")))
    put_todo(canceled, edited)
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Resource.objects.get(pk=canceled.pk).content == edited


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


def policy_writer():
    """May do anything in "", "p" and "q"."""
    return user_with((True, DEFAULT_CONTEXT), (True, "p"), (True, "q"))


def post_policy(content):
    When.http.post(
        "/workflow-policies/", data=json.dumps(content), content_type="application/json"
    )


def put_policy(stored, content):
    When.http.put(
        f"/workflow-policies/{stored.pk}/",
        data=json.dumps(content),
        content_type="application/json",
        HTTP_IF_MATCH=quote_etag(stored.etag),
    )


def assert_policy_refused(reason=None):
    """A ``WorkflowPolicy`` write refused with the policy problem, whose
    ``detail`` says why (and contains `reason`, if given)."""
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    body = Http.response.json()
    assert body["type"] == WORKFLOW_POLICY_INVALID, body
    assert body["title"] == "Invalid workflow policy", body
    assert body["status"] == 400, body
    assert isinstance(body["detail"], str) and body["detail"], body
    if reason is not None:
        assert reason in body["detail"], body


def policy_resources():
    return list(
        Resource.objects.filter(resource_type__type_uri=WORKFLOW_POLICY).order_by("pk")
    )


@pytest.mark.django_db
@pytest.mark.parametrize("context", ["p", ""])
@test_context
def test_second_policy_for_a_type_and_context_is_refused(context):
    first = store_policy(dict(PERMISSIVE_POLICY, rbac_context=context))
    Given.http.force_authenticate(policy_writer())
    post_policy(
        dict(PERMISSIVE_POLICY, rbac_context=context, initial_states={"x": "true"})
    )
    assert_policy_refused(f"already exists in context {context!r}")
    assert policy_resources() == [first]
    assert workflow_policy_for(TODO, context).resource == first


@pytest.mark.django_db
@test_context
def test_second_policy_naming_no_context_is_refused_in_the_default_context():
    first = store_policy(dict(PERMISSIVE_POLICY, rbac_context=DEFAULT_CONTEXT))
    Given.http.force_authenticate(policy_writer())
    post_policy(dict(PERMISSIVE_POLICY))
    assert_policy_refused("already exists in context ''")
    assert policy_resources() == [first]


@pytest.mark.django_db
@test_context
def test_policies_of_other_contexts_or_types_are_independent():
    reverse("todos-list")
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    Given.http.force_authenticate(policy_writer())
    post_policy(dict(PERMISSIVE_POLICY, rbac_context="q"))
    Then.http.status_code_is(status.HTTP_201_CREATED)
    post_policy(
        dict(
            PERMISSIVE_POLICY, rbac_context="p", resource_type="tests://PartnerProfiles"
        )
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)
    assert len(policy_resources()) == 3


@pytest.mark.django_db
@test_context
def test_moving_a_policy_into_a_context_that_has_one_is_refused():
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    other = store_policy(dict(PERMISSIVE_POLICY, rbac_context="q"))
    Given.http.force_authenticate(policy_writer())
    put_policy(other, dict(PERMISSIVE_POLICY, rbac_context="p"))
    assert_policy_refused("already exists in context 'p'")
    assert_unchanged(other)


@pytest.mark.django_db
@test_context
def test_a_policy_may_be_updated_in_its_own_context():
    stored = store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    updated = dict(PERMISSIVE_POLICY, rbac_context="p", initial_states={"x": "true"})
    Given.http.force_authenticate(policy_writer())
    put_policy(stored, updated)
    Then.http.status_code_is(status.HTTP_200_OK)
    assert workflow_policy_for(TODO, "p").policy == updated


@pytest.mark.django_db
@test_context
def test_post_only_writer_cannot_supersede_a_stored_policy():
    # The reviewer's probe: with most-recent-wins, a user who may only POST
    # policies in "x" replaced the admin's policy there with a permissive one.
    admin_policy = store_policy(
        dict(PERMISSIVE_POLICY, rbac_context="x", restrict_update=["any"])
    )
    user = user_with(({"http.POST": [WORKFLOW_POLICY]}, "x"))
    Given.http.force_authenticate(user)
    post_policy(dict(PERMISSIVE_POLICY, rbac_context="x"))
    assert_policy_refused("already exists in context 'x'")
    assert policy_resources() == [admin_policy]
    assert workflow_policy_for(TODO, "x").resource == admin_policy


@pytest.mark.django_db
def test_several_stored_policies_in_a_context_fail_closed():
    # Stored before policies were unique (or past the API): no policy is
    # picked; writes of the type in that context are a configuration error.
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    with pytest.raises(ImproperlyConfigured, match="2 WorkflowPolicy"):
        workflow_policy_for(TODO, "p")
    assert workflow_policy_for(TODO, "q").resource is None


@pytest.mark.django_db
def test_a_server_side_save_fails_closed_on_duplicate_policies_of_any_context():
    # A server-side save reads the policy of every context it concerns (to
    # report its transitions), as an API write does: several policies in
    # a secondary context, or in one it leaves, fail it closed, before it
    # writes, though its first context has a policy of its own.
    secondary = todo(rbac_context=["a", "p"])
    leaving = todo(rbac_context="p")
    staying = todo(rbac_context="a")
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    for resource, rbac_context in ((secondary, ["a", "p"]), (leaving, "a")):
        before = Resource.objects.get(pk=resource.pk)
        resource.content = dict(NOT_STARTED, title="edited", rbac_context=rbac_context)
        with pytest.raises(ImproperlyConfigured, match="2 WorkflowPolicy"):
            resource.save()
        assert_unchanged(before)
    # A resource in no such context is saved.
    staying.content = dict(NOT_STARTED, title="edited", rbac_context="a")
    staying.save()
    assert Resource.objects.get(pk=staying.pk).content["title"] == "edited"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "rbac_context", [["a", "b"], [], None, 5, {"@id": "x"}], ids=repr
)
def test_policy_rbac_context_must_be_a_string(rbac_context):
    from scrud_django.exceptions import WorkflowPolicyInvalidError
    from scrud_django.validators import WorkflowValidator

    with pytest.raises(WorkflowPolicyInvalidError) as excinfo:
        WorkflowValidator().validate_policy(
            dict(PERMISSIVE_POLICY, rbac_context=rbac_context), None
        )
    assert "rbac_context must be a string" in str(excinfo.value.detail)
    assert excinfo.value.detail.code == "workflow_policy_invalid"


@pytest.mark.django_db
@pytest.mark.parametrize("rbac_context", [["a", "b"], None, 5], ids=repr)
@test_context
def test_policy_with_a_non_string_context_is_refused(admin_login, rbac_context):
    # Also when the stored WorkflowPolicy schema does not declare its type. (A
    # superuser: scoped-rbac's request check refuses these contexts to others.)
    workflow_policy_resource_type()
    schema = ResourceType.objects.get(type_uri=WORKFLOW_POLICY).schema
    relaxed = json.loads(json.dumps(schema.content))
    del relaxed["properties"]["rbac_context"]
    ResourceRegistration.update(
        content=relaxed, register_type="json-schema", slug=str(schema.pk)
    )
    before = Resource.objects.count()
    Given.http.force_authenticate(admin_login)
    post_policy(dict(PERMISSIVE_POLICY, rbac_context=rbac_context))
    assert_policy_refused("rbac_context must be a string")
    assert Resource.objects.count() == before


# Shapes the WorkflowPolicy schema refuses; with a schema that lets them
# through, WorkflowPolicy's own checks refuse them, with the reason.
MALFORMED_POLICIES = [
    pytest.param(
        dict(PERMISSIVE_POLICY, initial_states=["x"]),
        "Invalid workflow initial_states: it must be an object",
        id="initial-states-list",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, states="nope"),
        "Invalid workflow states: it must be an object",
        id="states-string",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, transitions=["t"]),
        "Invalid workflow transitions: it must be an object",
        id="transitions-list",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, initial_states={"a": 3}),
        "Invalid workflow state condition for 'a': it must be a CEL expression",
        id="condition-number",
    ),
]


@pytest.mark.django_db
@pytest.mark.parametrize("policy,reason", MALFORMED_POLICIES)
@test_context
def test_a_malformed_policy_is_refused_past_a_relaxed_schema(
    admin_login, policy, reason
):
    workflow_policy_resource_type()
    schema = ResourceType.objects.get(type_uri=WORKFLOW_POLICY).schema
    relaxed = json.loads(json.dumps(schema.content))
    for key in ("initial_states", "states", "transitions"):
        relaxed["properties"][key] = {}
    ResourceRegistration.update(
        content=relaxed, register_type="json-schema", slug=str(schema.pk)
    )
    before = Resource.objects.count()
    Given.http.force_authenticate(admin_login)
    post_policy(policy)
    assert_policy_refused(reason)
    assert Resource.objects.count() == before


@pytest.mark.django_db
@pytest.mark.parametrize(
    "name", ["__SAME_STATE__", "__INITIALIZE__", "__LEAVE__", "__mine"]
)
@test_context
def test_reserved_transition_names_are_refused(regular_login, name):
    policy = dict(
        PERMISSIVE_POLICY,
        states={"done": "false"},
        transitions={name: {"from": "any", "to": "done"}},
    )
    reverse("todos-list")
    before = Resource.objects.count()
    Given.http.force_authenticate(regular_login)
    post_policy(policy)
    assert_policy_refused(f"Invalid workflow transition {name!r}")
    assert Resource.objects.count() == before


def a_policy_admin(context):
    return user_with(
        (
            {m: [WORKFLOW_POLICY] for m in ("http.GET", "http.PUT", "http.DELETE")},
            context,
        )
    )


@pytest.mark.django_db
@test_context
def test_admin_of_one_context_cannot_update_or_delete_anothers_policy():
    policy_b = store_policy(dict(PERMISSIVE_POLICY, rbac_context="b"))
    Given.http.force_authenticate(a_policy_admin("a"))
    put_policy(policy_b, dict(PERMISSIVE_POLICY, rbac_context="a"))
    Then.http.status_code_is(status.HTTP_404_NOT_FOUND)
    # A body naming "b": refused by the request check, before the lookup.
    put_policy(policy_b, dict(PERMISSIVE_POLICY, rbac_context="b"))
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    When.http.delete(
        f"/workflow-policies/{policy_b.pk}/", HTTP_IF_MATCH=quote_etag(policy_b.etag)
    )
    Then.http.status_code_is(status.HTTP_404_NOT_FOUND)
    assert_unchanged(policy_b)


@pytest.mark.django_db
@test_context
def test_admin_of_one_context_cannot_move_its_policy_into_another():
    policy_a = store_policy(dict(PERMISSIVE_POLICY, rbac_context="a"))
    Given.http.force_authenticate(a_policy_admin("a"))
    put_policy(policy_a, dict(PERMISSIVE_POLICY, rbac_context="b"))
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert_unchanged(policy_a)

    When.http.delete(
        f"/workflow-policies/{policy_a.pk}/", HTTP_IF_MATCH=quote_etag(policy_a.etag)
    )
    Then.http.status_code_is(status.HTTP_204_NO_CONTENT)
    assert policy_resources() == []


# Each invalid policy, with what its 400's ``detail`` says about it.
INVALID_POLICIES = [
    pytest.param(
        dict(PERMISSIVE_POLICY, initial_states={"draft": "status == "}),
        "Invalid workflow state condition: ",
        id="invalid-initial-state-condition",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, states={"done": "status == "}),
        "Invalid workflow state condition: ",
        id="invalid-state-condition",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, transitions={"go": {"from": "any", "to": "nowhere"}}),
        "Invalid workflow transition 'go': 'nowhere' is not a defined state.",
        id="transition-to-undefined-state",
    ),
    pytest.param(
        dict(
            PERMISSIVE_POLICY,
            initial_states={"any": "true"},
            states={"any": "false"},
        ),
        "Invalid workflow state 'any': it is named in both initial_states and "
        "states; a state has one condition.",
        id="state-in-initial-states-and-states",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, restrict_update=["nowhere"]),
        "Invalid workflow restrict_update: it must be a list of defined state "
        "names, not ['nowhere'].",
        id="restrict-update-undefined-state",
    ),
    pytest.param(
        dict(
            PERMISSIVE_POLICY,
            transitions={
                "go": {"from": "any", "to": "any"},
                "again": {"from": ["any"], "to": "any"},
            },
        ),
        "Invalid workflow transitions 'go' and 'again': both lead from 'any' to "
        "'any'; a pair of states may have one transition.",
        id="duplicate-pair",
    ),
]

# ``restrict_update`` must be a list of state names, and a transition an
# object (not a list of them); the schema refuses others.
SCHEMA_INVALID_POLICIES = [
    pytest.param(dict(PERMISSIVE_POLICY, restrict_update=value), id=repr(value))
    for value in (5, True, None, "any", [5])
] + [
    pytest.param(
        dict(PERMISSIVE_POLICY, transitions={"go": [{"from": "any", "to": "any"}]}),
        id="transition-list",
    ),
]


@pytest.mark.django_db
@pytest.mark.parametrize("policy,reason", INVALID_POLICIES)
@test_context
def test_invalid_policy_is_refused_on_create(regular_login, policy, reason):
    reverse("todos-list")
    before = Resource.objects.count()
    Given.http.force_authenticate(regular_login)
    When.http.post(
        "/workflow-policies/", data=json.dumps(policy), content_type="application/json"
    )
    # The 400 says why: the problem's detail is the reason the policy fails
    # WorkflowPolicy's checks.
    assert_policy_refused(reason)
    assert Http.response["Content-Type"].startswith("application/problem+json")
    assert Resource.objects.count() == before

    # Writes of the type still work.
    post_todo(dict(NOT_STARTED))
    Then.http.status_code_is(status.HTTP_201_CREATED)


@pytest.mark.django_db
@pytest.mark.parametrize("policy,reason", INVALID_POLICIES)
@test_context
def test_invalid_policy_is_refused_on_update(regular_login, policy, reason):
    stored = store_policy(dict(PERMISSIVE_POLICY))
    Given.http.force_authenticate(regular_login)
    When.http.put(
        f"/workflow-policies/{stored.pk}/",
        data=json.dumps(policy),
        content_type="application/json",
        HTTP_IF_MATCH=quote_etag(stored.etag),
    )
    assert_policy_refused(reason)
    assert_unchanged(stored)


@pytest.mark.django_db
@pytest.mark.parametrize("policy", SCHEMA_INVALID_POLICIES)
@test_context
def test_schema_invalid_policy_is_refused(regular_login, policy):
    reverse("todos-list")
    before = Resource.objects.count()
    Given.http.force_authenticate(regular_login)
    When.http.post(
        "/workflow-policies/", data=json.dumps(policy), content_type="application/json"
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert Http.response.json()["type"] == JSON_INVALID, Http.response.json()
    assert Resource.objects.count() == before


# A secret only context "b" may read: its stored policy must not reach a writer
# who is refused because of it.
SECRET = "SECRET-B"
SECRET_POLICY = {
    "rbac_context": "b",
    "resource_type": TODO,
    "initial_states": {
        "not started": f'status == "not started" || title == "{SECRET}"',
    },
    "states": {"in progress": f'status == "in progress" || title == "{SECRET}"'},
    "transitions": {},
}


def assert_references_stored_policy(stored):
    body = Http.response.json()
    assert_workflow_error()
    url = "http://testserver" + reverse("workflow-policies-detail", args=[stored.pk])
    assert body["policy"] == url, body
    assert SECRET not in Http.response.content.decode(), body


GENERIC_WORKFLOW_MESSAGE = (
    "The request is not allowed by a workflow policy that applies to this resource."
)


def assert_generic_workflow_error(stored):
    """The error names nothing of `stored`, a policy of a context in which the
    writer may not read ``WorkflowPolicy``: not its URL, states or content."""
    body = Http.response.json()
    assert body == {
        "type": WORKFLOW_ERROR,
        "title": "Invalid workflow transition",
        "detail": GENERIC_WORKFLOW_MESSAGE,
        "policy": None,
        "prior_state": None,
        "proposed_state": None,
        "message": GENERIC_WORKFLOW_MESSAGE,
        "status": 400,
    }, body
    content = Http.response.content.decode()
    assert SECRET not in content
    assert reverse("workflow-policies-detail", args=[stored.pk]) not in content


# May read stored policies in "b".
READ_POLICIES = {"http.GET": [WORKFLOW_POLICY]}


@pytest.mark.django_db
@test_context
def test_unauthorized_transition_under_an_unreadable_policy_is_generic():
    # Writer in "a" only; the resource is also in "b", whose stored policy
    # makes the update a same-state change the writer may not make there. The
    # writer may not read policies in "b": the error says nothing of it.
    stored = store_policy(SECRET_POLICY)
    user = user_with((TODO_WRITER, "a"))
    resource = todo(rbac_context=["a", "b"])
    Given.http.force_authenticate(user)
    put_todo(resource, dict(NOT_STARTED, rbac_context="a"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_generic_workflow_error(stored)
    assert_unchanged(resource)


@pytest.mark.django_db
@test_context
def test_unauthorized_transition_error_references_stored_policy_by_url():
    # As above, but the writer may read policies in "b".
    stored = store_policy(SECRET_POLICY)
    user = user_with((TODO_WRITER, "a"), (READ_POLICIES, "b"))
    resource = todo(rbac_context=["a", "b"])
    Given.http.force_authenticate(user)
    put_todo(resource, dict(NOT_STARTED, rbac_context="a"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_references_stored_policy(stored)
    body = Http.response.json()
    assert (body["prior_state"], body["proposed_state"]) == (
        "not started",
        "not started",
    ), body
    assert_unchanged(resource)


@pytest.mark.django_db
@test_context
def test_invalid_transition_under_an_unreadable_policy_is_generic():
    # SECRET_POLICY has no transitions: "not started" -> "in progress" is
    # invalid there. The writer may write in "b" but not read its policies.
    stored = store_policy(SECRET_POLICY)
    user = user_with((TODO_WRITER, "a"), (TODO_WRITER, "b"))
    resource = todo(rbac_context=["a", "b"])
    Given.http.force_authenticate(user)
    put_todo(resource, dict(NOT_STARTED, status="in progress", rbac_context="a"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_generic_workflow_error(stored)
    assert_unchanged(resource)


@pytest.mark.django_db
@test_context
def test_invalid_transition_error_references_stored_policy_by_url():
    # As above, but the writer may read policies in "b".
    stored = store_policy(SECRET_POLICY)
    user = user_with((TODO_WRITER, "a"), (TODO_WRITER, "b"), (READ_POLICIES, "b"))
    resource = todo(rbac_context=["a", "b"])
    Given.http.force_authenticate(user)
    put_todo(resource, dict(NOT_STARTED, status="in progress", rbac_context="a"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_references_stored_policy(stored)
    assert Http.response.json()["detail"] == Http.response.json()["policy"]
    assert_unchanged(resource)


@pytest.mark.django_db
@test_context
def test_invalid_initial_state_under_an_unreadable_policy_is_generic():
    stored = store_policy(SECRET_POLICY)
    user = user_with((TODO_WRITER, "b"))
    before = Resource.objects.count()
    Given.http.force_authenticate(user)
    post_todo(dict(NOT_STARTED, status="done", rbac_context="b"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_generic_workflow_error(stored)
    assert Resource.objects.count() == before


@pytest.mark.django_db
@test_context
def test_errors_under_a_readable_policy_keep_their_body():
    # The writer's own context, where they may read its policy.
    stored = store_policy(SECRET_POLICY)
    user = user_with((TODO_WRITER, "b"), (READ_POLICIES, "b"))
    Given.http.force_authenticate(user)
    post_todo(dict(NOT_STARTED, status="done", rbac_context="b"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_references_stored_policy(stored)
    body = Http.response.json()
    assert body["message"] == "The request is not a valid initial state."
    assert body["detail"] == body["message"]


PP = "tests://PartnerProfiles"
LOCKING_PP_POLICY = {
    "resource_type": PP,
    "initial_states": {"open": 'state == "open"'},
    "states": {"locked": 'state == "locked"', "secret-b-state": "false"},
    "transitions": {"lock": {"from": "open", "to": "locked"}},
    "restrict_update": ["locked"],
}


@pytest.mark.django_db
def test_editing_a_shared_resource_does_not_leak_the_other_contexts_policy():
    # The reviewer's probe: an "a"-only user edits a partner profile in
    # ["a", "b"]; "b"'s stored policy refuses it.
    workflow_policy_resource_type()
    reverse("partner-profiles-list")
    policy_b = store_policy(dict(LOCKING_PP_POLICY, rbac_context="b"))
    shared = ResourceRegistration.register(
        {"name": "S", "state": "open", "rbac_context": ["a", "b"]}, "partner-profiles"
    )
    a_user = user_with(({m: [PP] for m in ("http.GET", "http.PUT")}, "a"))
    client = APIClient()
    client.force_authenticate(a_user)
    response = client.put(
        reverse("partner-profiles-detail", args=[shared.pk]),
        {"name": "S2", "state": "open", "rbac_context": "a"},
        format="json",
        HTTP_IF_MATCH=quote_etag(shared.etag),
    )
    assert response.status_code == 400, response.content
    body = response.json()
    assert body["type"] == WORKFLOW_ERROR
    assert body["message"] == GENERIC_WORKFLOW_MESSAGE
    assert (body["policy"], body["prior_state"], body["proposed_state"]) == (
        None,
        None,
        None,
    )
    assert "secret-b-state" not in response.content.decode()
    assert f"/workflow-policies/{policy_b.pk}/" not in response.content.decode()


# How a workflow error names the settings default policy of the ToDo type: by
# its type, with a marker, never by its content.
DEFAULT_TODO_POLICY_REFERENCE = {"resource_type": TODO, "default": True}


@pytest.mark.django_db
@pytest.mark.parametrize("context", [DEFAULT_CONTEXT, "w"])
@test_context
def test_default_policy_errors_reference_the_policy(context):
    # Was test_default_policy_errors_still_embed_the_policy: the body embedded
    # the default policy (CEL conditions and every transition) in "policy" and
    # "detail". It now names the policy by reference, in "" and elsewhere.
    cancel_only = user_with((CANCEL_ONLY, context))
    resource = todo(rbac_context=context)
    Given.http.force_authenticate(cancel_only)
    put_todo(resource, dict(NOT_STARTED, status="done", rbac_context=context))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)  # "finish": cancel-only
    assert_workflow_error()
    body = Http.response.json()
    assert body["policy"] == DEFAULT_TODO_POLICY_REFERENCE, body
    assert (body["prior_state"], body["proposed_state"]) == ("not started", "done")
    assert body["detail"] == body["message"], body

    Given.http.force_authenticate(user_with((TODO_WRITER, context)))
    done = todo(status="done", rbac_context=context)
    put_todo(done, dict(NOT_STARTED, rbac_context=context))  # out of "done"
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    body = Http.response.json()
    assert body == {
        "type": WORKFLOW_ERROR,
        "title": "Invalid workflow transition",
        "detail": DEFAULT_TODO_POLICY_REFERENCE,
        "policy": DEFAULT_TODO_POLICY_REFERENCE,
        "prior_state": "done",
        "proposed_state": "not started",
        "message": "Invalid workflow transition",
        "status": 400,
    }, body

    post_todo(dict(NOT_STARTED, status="done", rbac_context=context))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    body = Http.response.json()
    assert body["policy"] == DEFAULT_TODO_POLICY_REFERENCE, body
    assert body["message"] == "The request is not a valid initial state."

    put_todo(done, dict(NOT_STARTED, status="nonsense", rbac_context=context))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)  # the schema's enum
    put_todo(done, {"title": "t", "rbac_context": context})  # in no state
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()
    assert Http.response.json()["policy"] == DEFAULT_TODO_POLICY_REFERENCE


@pytest.mark.django_db
@test_context
def test_no_workflow_error_body_carries_cel():
    # Stored (readable and not), default, and other-context policies at once:
    # a ToDo in "a" (default policy), "b" (SECRET_POLICY) and "c" (a stored
    # policy the writer may read).
    store_policy(SECRET_POLICY)
    readable = store_policy(
        dict(SECRET_POLICY, rbac_context="c", initial_states={"x": 'title == "C"'})
    )
    writer = user_with(
        (CANCEL_ONLY, "a"), (TODO_WRITER, "b"), (TODO_WRITER, "c"), (READ_POLICIES, "c")
    )
    url = "http://testserver" + reverse("workflow-policies-detail", args=[readable.pk])
    Given.http.force_authenticate(writer)
    for context, policy in (
        ("a", DEFAULT_TODO_POLICY_REFERENCE),
        ("b", None),  # not readable: a generic error
        ("c", url),
    ):
        post_todo(dict(NOT_STARTED, status="done", rbac_context=context))
        Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
        assert_workflow_error()
        assert Http.response.json()["policy"] == policy
    # A ToDo in all three (stored server-side; the schema takes one context).
    resource = todo(rbac_context=["a", "b", "c"])
    put_todo(resource, dict(NOT_STARTED, status="done", rbac_context="a"))
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert_workflow_error()


@pytest.mark.django_db
@test_context
def test_admin_of_one_context_cannot_update_or_delete_a_shared_legacy_policy():
    # A policy in ["a", "b"] (stored server-side; the API takes one context)
    # applies in both: updating or deleting it needs rights in each.
    shared = store_policy(dict(PERMISSIVE_POLICY, rbac_context=["a", "b"]))
    Given.http.force_authenticate(a_policy_admin("a"))
    put_policy(shared, dict(PERMISSIVE_POLICY, rbac_context="a"))
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    When.http.delete(
        f"/workflow-policies/{shared.pk}/", HTTP_IF_MATCH=quote_etag(shared.etag)
    )
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert_unchanged(shared)


@pytest.mark.django_db
@test_context
def test_admin_of_every_context_may_delete_a_shared_legacy_policy():
    shared = store_policy(dict(PERMISSIVE_POLICY, rbac_context=["a", "b"]))
    admin = a_policy_admin("a")
    assign(admin, {"http.DELETE": [WORKFLOW_POLICY]}, "b")
    Given.http.force_authenticate(admin)
    When.http.delete(
        f"/workflow-policies/{shared.pk}/", HTTP_IF_MATCH=quote_etag(shared.etag)
    )
    Then.http.status_code_is(status.HTTP_204_NO_CONTENT)
    assert policy_resources() == []


# scrud_post_save's ``transitions``: the write's TransitionData in every
# context it concerns (the stored resource's and the content's) where a
# policy applies; ``transition_error``: the workflow error behind a ``None``
# ``transition_data``.


@pytest.fixture
def reported_saves():
    """The keyword arguments of each ToDo ``scrud_post_save``, in order."""
    from scrud_django.scrud_signals import scrud_post_save

    reported = []

    def record(sender, resource_type_uri=None, **kwargs):
        if resource_type_uri == TODO:
            reported.append(kwargs)

    scrud_post_save.connect(record, weak=False, dispatch_uid="test-reported-saves")
    try:
        yield reported
    finally:
        scrud_post_save.disconnect(dispatch_uid="test-reported-saves")


# A policy of its own in "b": its states are not the default policy's.
B_POLICY = {
    "rbac_context": "b",
    "resource_type": TODO,
    "initial_states": {"open": 'status == "not started"'},
    "states": {"working": 'status == "in progress"'},
    "transitions": {"work": {"from": "open", "to": "working"}},
}


@pytest.mark.django_db
@test_context
def test_a_move_reports_leaving_one_context_and_entering_the_other(reported_saves):
    from scrud_django.workflow import TransitionData

    store_policy(B_POLICY)
    user = user_with((TODO_WRITER, "a"), (TODO_WRITER, "b"))
    resource = todo(rbac_context="a")
    Given.http.force_authenticate(user)
    reported_saves.clear()
    put_todo(resource, dict(NOT_STARTED, rbac_context="b"))
    Then.http.status_code_is(status.HTTP_200_OK)
    (reported,) = reported_saves
    # Left "a" (the default policy) from "not started"; entered "b".
    assert reported["transitions"] == {
        "a": TransitionData(None, "not started", "__LEAVE__"),
        "b": TransitionData("open", None, "__INITIALIZE__"),
    }
    # transition_data is unchanged: the first context the content names.
    assert reported["transition_data"] == TransitionData("open", None, "__INITIALIZE__")
    assert reported["transition_error"] is None


@pytest.mark.django_db
@test_context
def test_a_resource_in_two_contexts_reports_each_contexts_transition(
    reported_saves,
):
    from scrud_django.workflow import TransitionData

    store_policy(B_POLICY)
    # In two contexts: stored and updated server-side (the schema takes one).
    resource = todo(rbac_context=["a", "b"])
    reported_saves.clear()
    resource.content = dict(NOT_STARTED, status="in progress", rbac_context=["a", "b"])
    resource.save()
    (reported,) = reported_saves
    assert reported["transitions"] == {
        "a": TransitionData("in progress", "not started", "start"),
        "b": TransitionData("working", "open", "work"),
    }
    assert reported["transition_data"] == TransitionData(
        "in progress", "not started", "start"
    )
    assert reported["transition_error"] is None
    # Created in both: an __INITIALIZE__ in each.
    reported_saves.clear()
    todo(rbac_context=["a", "b"])
    (reported,) = reported_saves
    assert reported["transitions"] == {
        "a": TransitionData("not started", None, "__INITIALIZE__"),
        "b": TransitionData("open", None, "__INITIALIZE__"),
    }


@pytest.mark.django_db
@test_context
def test_an_invalid_server_side_save_reports_its_transition_error(reported_saves):
    from scrud_django.exceptions import WorkflowTransitionError

    # "done" -> "not started" is no transition of the default policy; a
    # server-side save is not checked, and is reported with the error.
    resource = todo(rbac_context="a", status="done")
    reported_saves.clear()
    resource.content = dict(NOT_STARTED, rbac_context="a")
    resource.save()
    (reported,) = reported_saves
    assert reported["transition_data"] is None
    error = reported["transition_error"]
    assert isinstance(error, WorkflowTransitionError), error
    assert (error.prior_state, error.proposed_state) == ("done", "not started")
    # The context's transition is no valid one: None there.
    assert reported["transitions"] == {"a": None}


@pytest.mark.django_db
@test_context
def test_a_server_side_create_in_no_initial_state_reports_its_error(reported_saves):
    from scrud_django.exceptions import WorkflowInvalidInitialStateError

    # "done" is no initial state of the default policy: the API refuses
    # such a create; a server-side one is saved, and reported as an invalid
    # transition is, None with the error, not as an __INITIALIZE__ into no
    # state.
    reported_saves.clear()
    todo(rbac_context="a", status="done")
    (reported,) = reported_saves
    assert reported["transition_data"] is None
    error = reported["transition_error"]
    assert isinstance(error, WorkflowInvalidInitialStateError), error
    assert (error.prior_state, error.proposed_state) == (None, None)
    assert reported["transitions"] == {"a": None}


@pytest.mark.django_db
@test_context
def test_a_server_side_move_into_a_context_in_no_initial_state_reports_its_error(
    reported_saves,
):
    from scrud_django.exceptions import WorkflowInvalidInitialStateError
    from scrud_django.workflow import TransitionData

    # Entering "v" is a create there, and "done" is no initial state.
    resource = todo(rbac_context="w", status="done")
    reported_saves.clear()
    resource.content = dict(resource.content, rbac_context="v")
    resource.save()
    (reported,) = reported_saves
    assert reported["transition_data"] is None
    error = reported["transition_error"]
    assert isinstance(error, WorkflowInvalidInitialStateError), error
    assert reported["transitions"] == {
        "w": TransitionData(None, "done", "__LEAVE__"),
        "v": None,
    }


# A save that skipped the WorkflowValidator (the admin, ResourceRegistration,
# server code) is still saved when it is invalid: each context's workflow
# error is logged on the scrud_django.workflow logger, not only reported.
WORKFLOW_LOGGER = "scrud_django.workflow"


def workflow_warnings(caplog):
    return [
        record
        for record in caplog.records
        if record.name == WORKFLOW_LOGGER and record.levelno == logging.WARNING
    ]


def assert_logs_workflow_error(record, resource, context, message):
    logged = record.getMessage()
    assert f"pk {resource.pk}" in logged, logged
    assert TODO in logged, logged
    assert f"context {context!r}" in logged, logged
    assert message in logged, logged


@pytest.mark.django_db
@test_context
def test_a_save_past_the_workflow_check_logs_each_contexts_error(
    caplog, reported_saves
):
    from scrud_django.workflow import TransitionData

    store_policy(B_POLICY)
    resource = todo(rbac_context=["a", "b"])
    assert workflow_warnings(caplog) == []
    # Valid in "a" ("finish", and the context transition_data reports), but
    # "done" is no state of "b"'s policy: only "b"'s error is logged, though
    # transition_error (the picked context's) is None.
    reported_saves.clear()
    resource.content = dict(NOT_STARTED, status="done", rbac_context=["a", "b"])
    resource.save()
    (reported,) = reported_saves
    assert reported["transition_data"] == TransitionData(
        "done", "not started", "finish"
    )
    assert reported["transition_error"] is None
    assert reported["transitions"]["b"] is None
    (record,) = workflow_warnings(caplog)
    assert_logs_workflow_error(
        record, resource, "b", "The request does not match a valid state"
    )

    # Invalid in both: one warning per context, in the contexts' order.
    caplog.clear()
    created = todo(rbac_context=["a", "b"], status="done")
    first, second = workflow_warnings(caplog)
    initial = "The request is not a valid initial state."
    assert_logs_workflow_error(first, created, "a", initial)
    assert_logs_workflow_error(second, created, "b", initial)


@pytest.mark.django_db
@test_context
def test_an_ambiguous_server_side_save_logs_the_matching_states(caplog):
    store_policy(dict(OVERLAPPING_POLICY, rbac_context="w"))
    created = todo(**dict(AMBIGUOUS, rbac_context="w"))
    (record,) = workflow_warnings(caplog)
    assert_logs_workflow_error(
        record,
        created,
        "w",
        "matching states ['published', 'review']",
    )
    assert ambiguous_state_message("The request") in record.getMessage()


@pytest.mark.django_db
@test_context
def test_a_save_past_the_workflow_check_is_logged_before_the_signal(caplog):
    # A scrud_post_save receiver that raises does not keep the save's
    # workflow errors from being logged: they are logged first. The log
    # does not say the save committed (the receiver's error may roll it
    # back).
    from scrud_django.scrud_signals import scrud_post_save

    class ReceiverFailed(Exception):
        pass

    def failing(sender, **kwargs):
        raise ReceiverFailed

    reverse("todos-list")
    scrud_post_save.connect(failing, weak=False, dispatch_uid="test-failing")
    try:
        with pytest.raises(ReceiverFailed):
            todo(rbac_context="a", status="done")
    finally:
        scrud_post_save.disconnect(dispatch_uid="test-failing")
    (record,) = workflow_warnings(caplog)
    logged = record.getMessage()
    assert "context 'a'" in logged, logged
    assert "The request is not a valid initial state." in logged, logged
    assert "skipped the workflow check" in logged, logged
    assert "was saved" not in logged, logged


@pytest.mark.django_db
@test_context
def test_scrud_post_save_sends_its_transitions_read_only(reported_saves):
    from types import MappingProxyType

    from scrud_django.workflow import TransitionData

    # "done" -> "not started" is no transition of the default policy.
    resource = todo(rbac_context="a", status="done")
    reported_saves.clear()
    resource.content = dict(NOT_STARTED, rbac_context="a")
    errors = resource.get_transitions().errors
    assert isinstance(errors, MappingProxyType), errors
    assert list(errors) == ["a"]
    with pytest.raises(TypeError):
        del errors["a"]
    resource.save()
    (reported,) = reported_saves
    transitions = reported["transitions"]
    assert isinstance(transitions, MappingProxyType), transitions
    assert transitions == {"a": None}
    # The same mapping reaches every receiver (and the on_save action):
    # none can change what the others are given.
    with pytest.raises(TypeError):
        transitions["a"] = TransitionData("not started", "done", "reopen")


@pytest.mark.django_db
@test_context
def test_a_valid_write_logs_no_workflow_error(caplog):
    user = user_with((TODO_WRITER, "a"))
    resource = todo(rbac_context="a")
    Given.http.force_authenticate(user)
    put_todo(resource, dict(NOT_STARTED, status="in progress", rbac_context="a"))
    Then.http.status_code_is(status.HTTP_200_OK)
    resource.refresh_from_db()
    resource.content = dict(resource.content, status="done")
    resource.save()
    assert workflow_warnings(caplog) == []


@pytest.mark.django_db
@test_context
def test_a_write_under_no_policy_reports_no_transition_and_no_error():
    from scrud_django.scrud_signals import scrud_post_save

    reverse("partner-profiles-list")
    reported = []

    def record(sender, **kwargs):
        reported.append(kwargs)

    scrud_post_save.connect(record, weak=False, dispatch_uid="test-no-policy")
    try:
        profile = ResourceRegistration.register(
            {"name": "n", "rbac_context": "a"}, "partner-profiles"
        )
        profile.content = {"name": "m", "rbac_context": "b"}
        profile.save()
    finally:
        scrud_post_save.disconnect(dispatch_uid="test-no-policy")
    assert [
        (r["transition_data"], r["transition_error"], r["transitions"])
        for r in reported
    ] == [(None, None, {}), (None, None, {})]
