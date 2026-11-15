"""A grant conditioned on ``scrud_workflow`` grants workflow transitions and
nothing else (fail closed). It allows the checks of a POST or PUT that the
``WorkflowValidator`` authorizes (the request check, the PUT's object check
and the post-save check) and, in the workflow check, the transitions it
lists. Everything else is refused: GET, HEAD, lists, DELETE, and a write the
workflow check does not cover (such as a POST ``@action`` of a viewset). A
role that should read needs its own ``http.GET`` grant."""

import json
from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework import status
from rest_framework.test import APIClient
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .front_ends import api_url
from .steps import *  # noqa: F403, F401

TODO = "tests://ToDo"
TODO_LIST = (
    "https://api.openteams.com/json-schema/ResourceCollection?contents_type=" + TODO
)
PLAN = {
    "title": "private plan",
    "status": "not started",
    "priority": "high",
    "assignee": "http://somewhere.example/someone",
    "rbac_context": "team",
}


def scrud_workflow(transitions):
    return {
        "condition": {
            "operator": "scrud_workflow",
            "authorized_transitions": transitions,
        }
    }


def user_with(*grants):
    """A user holding each ``(definition, context)`` of `grants`."""
    user = UserFactory()
    for definition, context in grants:
        role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
        role.save()
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=context,
            etag=uuid4().hex,
            modified_at=now(),
        ).save()
    return user


def client_with(*grants):
    """A client whose user holds each ``(definition, context)`` of `grants`,
    logged in through the session (both front ends)."""
    client = APIClient()
    client.force_login(user_with(*grants))
    return client


def todo(content=PLAN):
    reverse("todos-list", urlconf="tests.urls")
    return ResourceRegistration.register(dict(content), "todos")


def detail(front_end, resource):
    return api_url(front_end, "todos-detail", args=[resource.pk])


def if_match(resource):
    return {"HTTP_IF_MATCH": quote_etag(Resource.objects.get(pk=resource.pk).etag)}


def listed_titles(response):
    assert response.status_code == 200, response.content
    return sorted(entry["content"]["title"] for entry in response.json()["content"])


# The review's "may only cancel ToDos in team" role: every action on ToDo
# conditioned on the cancel transition, and the list type unconditionally.
CANCELLER = {"*": {TODO: scrud_workflow(["cancel"]), TODO_LIST: True}}


@pytest.mark.django_db
def test_a_cancel_only_role_cannot_read_list_or_delete(front_end):
    stored = todo()
    client = client_with((CANCELLER, "team"))
    assert client.get(detail(front_end, stored)).status_code == 403
    assert client.head(detail(front_end, stored)).status_code == 403
    response = client.get(api_url(front_end, "todos-list"))
    assert listed_titles(response) == []
    assert response.json()["count"] == 0
    assert client.head(api_url(front_end, "todos-list")).status_code == 200
    response = client.delete(detail(front_end, stored), **if_match(stored))
    assert response.status_code == 403, response.content
    assert Resource.objects.filter(pk=stored.pk).exists()


@pytest.mark.django_db
def test_a_cancel_only_role_still_cancels_and_nothing_else(front_end):
    stored = todo()
    client = client_with((CANCELLER, "team"))
    # A create is __INITIALIZE__, not listed.
    before = Resource.objects.count()
    response = client.post(api_url(front_end, "todos-list"), PLAN, format="json")
    assert response.status_code == 400, response.content
    assert Resource.objects.count() == before
    # "start" is not listed.
    started = dict(PLAN, status="in progress")
    response = client.put(
        detail(front_end, stored), started, format="json", **if_match(stored)
    )
    assert response.status_code == 400, response.content
    assert Resource.objects.get(pk=stored.pk).content == PLAN
    # "cancel" is.
    canceled = dict(PLAN, status="canceled")
    response = client.put(
        detail(front_end, stored), canceled, format="json", **if_match(stored)
    )
    assert response.status_code == 200, response.content
    assert Resource.objects.get(pk=stored.pk).content == canceled


@pytest.mark.django_db
def test_a_cancel_only_role_with_its_own_get_grant_reads(front_end):
    # What the Upgrading line tells adopters: a role that should read needs
    # its own http.GET grant.
    stored = todo()
    client = client_with((CANCELLER, "team"), ({"http.GET": [TODO]}, "team"))
    assert client.get(detail(front_end, stored)).status_code == 200
    assert listed_titles(client.get(api_url(front_end, "todos-list"))) == [
        "private plan"
    ]
    response = client.delete(detail(front_end, stored), **if_match(stored))
    assert response.status_code == 403, response.content


@pytest.mark.django_db
def test_per_method_grants_conditioned_on_scrud_workflow_allow_no_get_or_delete(
    front_end,
):
    stored = todo()
    client = client_with(
        (
            {
                "http.GET": {TODO: scrud_workflow([]), TODO_LIST: True},
                "http.DELETE": {TODO: scrud_workflow(["cancel"])},
            },
            "team",
        )
    )
    assert client.get(detail(front_end, stored)).status_code == 403
    assert listed_titles(client.get(api_url(front_end, "todos-list"))) == []
    response = client.delete(detail(front_end, stored), **if_match(stored))
    assert response.status_code == 403, response.content
    assert Resource.objects.filter(pk=stored.pk).exists()


@pytest.mark.django_db
def test_the_list_and_the_detail_route_agree(front_end):
    # GET outright in "open", only through scrud_workflow in "team": the list
    # shows the "open" ToDo alone, and the detail route hides the "team" one
    # (404: it is not readable), as the list leaves it out.
    hidden = todo()
    shown = todo(dict(PLAN, title="open plan", rbac_context="open"))
    client = client_with(
        ({"http.GET": [TODO, TODO_LIST]}, "open"),
        ({"http.GET": {TODO: scrud_workflow(["cancel"])}}, "team"),
    )
    assert listed_titles(client.get(api_url(front_end, "todos-list"))) == ["open plan"]
    assert client.get(detail(front_end, shown)).status_code == 200
    assert client.get(detail(front_end, hidden)).status_code == 404


@pytest.mark.django_db
def test_a_put_refused_by_its_object_check_hides_what_scrud_workflow_would_read(
    front_end,
):
    # The PUT's own checks run with the write's allowance; whether a refused
    # resource is readable (403) or hidden (404) is asked outside it, so a
    # GET grant conditioned on scrud_workflow does not make it readable.
    stored = todo()
    client = client_with(
        (
            {
                "http.GET": {TODO: scrud_workflow(["cancel"])},
                "http.PUT": {TODO: {"condition": 'title == "never"'}},
            },
            "team",
        )
    )
    canceled = dict(PLAN, status="canceled", title="never")
    response = client.put(
        detail(front_end, stored), canceled, format="json", **if_match(stored)
    )
    assert response.status_code == 404, response.content
    assert Resource.objects.get(pk=stored.pk).content == PLAN


# A POST @action of an item that writes the resource itself
# (tests.urls.ArchivingToDoViewSet.archive): no WorkflowValidator runs, so
# the workflow check does not cover it and scrud_workflow refuses it, as for
# any other route but a create or an update.


def archive(resource):
    When.http.post(
        reverse("archiving-todos-archive", args=[resource.pk]),
        data=json.dumps({"rbac_context": "team"}),
        content_type="application/json",
    )


# DRF-only: archiving-todos/ is a DRF @action of tests/urls.py only.
@pytest.mark.django_db
@pytest.mark.parametrize(
    "definition",
    [
        {"http.POST": {TODO: scrud_workflow([])}},
        {"http.POST": {TODO: scrud_workflow(["__SAME_STATE__"])}},
        {"*": {TODO: scrud_workflow(["__INITIALIZE__", "__SAME_STATE__", "cancel"])}},
    ],
    ids=["none", "same-state", "every-action"],
)
@test_context
def test_a_scrud_workflow_grant_does_not_authorize_a_custom_write_action(definition):
    stored = todo()
    Given.http.force_authenticate(user_with((definition, "team")))
    archive(stored)
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert Resource.objects.get(pk=stored.pk).content == PLAN

    # Its OPTIONS does not offer the POST either.
    When.http.options(reverse("archiving-todos-archive", args=[stored.pk]))
    Then.http.status_code_is(status.HTTP_204_NO_CONTENT)
    assert "POST" not in Http.response["Allow"], Http.response["Allow"]


# DRF-only: archiving-todos/ is a DRF @action of tests/urls.py only.
@pytest.mark.django_db
@test_context
def test_a_plain_post_grant_authorizes_a_custom_write_action():
    stored = todo()
    Given.http.force_authenticate(user_with(({"http.POST": [TODO]}, "team")))
    When.http.options(reverse("archiving-todos-archive", args=[stored.pk]))
    Then.http.status_code_is(status.HTTP_204_NO_CONTENT)
    assert "POST" in Http.response["Allow"], Http.response["Allow"]
    archive(stored)
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Resource.objects.get(pk=stored.pk).content == dict(PLAN, status="done")


# A PUT that is not the viewset's update: an @action
# (tests.urls.ReopeningToDoViewSet.reopen, view.action "reopen"), and a
# view that is not a viewset (tests.urls.ToDoReopenView, view.action None).
# Neither runs the WorkflowValidator: scrud_workflow refuses them.

REOPEN_ROUTES = ["reopening-todos-reopen", "todo-reopen"]


def reopen(route, resource):
    When.http.put(
        reverse(route, args=[resource.pk]),
        data=json.dumps(dict(PLAN, status="not started")),
        content_type="application/json",
        **if_match(resource),
    )


# DRF-only: the reopen routes are DRF views of tests/urls.py only.
@pytest.mark.django_db
@pytest.mark.parametrize("route", REOPEN_ROUTES)
@pytest.mark.parametrize(
    "definition",
    [
        {"http.PUT": {TODO: scrud_workflow([])}},
        {"http.PUT": {TODO: scrud_workflow(["__SAME_STATE__"])}},
        {"*": {TODO: scrud_workflow(["__INITIALIZE__", "__SAME_STATE__", "cancel"])}},
    ],
    ids=["none", "same-state", "every-action"],
)
@test_context
def test_a_scrud_workflow_grant_does_not_authorize_another_put(route, definition):
    stored = todo(dict(PLAN, status="done"))
    Given.http.force_authenticate(user_with((definition, "team")))
    reopen(route, stored)
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert Resource.objects.get(pk=stored.pk).content == dict(PLAN, status="done")

    # Its OPTIONS does not offer the PUT either.
    When.http.options(reverse(route, args=[stored.pk]))
    Then.http.status_code_is(status.HTTP_204_NO_CONTENT)
    assert "PUT" not in Http.response["Allow"], Http.response["Allow"]


# DRF-only: the reopen routes are DRF views of tests/urls.py only.
@pytest.mark.django_db
@pytest.mark.parametrize("route", REOPEN_ROUTES)
@test_context
def test_a_plain_put_grant_authorizes_another_put(route):
    stored = todo(dict(PLAN, status="done"))
    Given.http.force_authenticate(
        user_with(({"http.GET": [TODO], "http.PUT": [TODO]}, "team"))
    )
    When.http.options(reverse(route, args=[stored.pk]))
    Then.http.status_code_is(status.HTTP_204_NO_CONTENT)
    assert "PUT" in Http.response["Allow"], Http.response["Allow"]
    reopen(route, stored)
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Resource.objects.get(pk=stored.pk).content == PLAN


@pytest.mark.django_db
@test_context
def test_a_scrud_workflow_grant_still_offers_its_create_and_update(front_end):
    # The workflow check covers a scrud viewset's create (POST to the list)
    # and update (PUT to the detail route): its OPTIONS offers them to a
    # role conditioned on scrud_workflow alone, which the workflow check
    # then decides by the transition.
    stored = todo()
    Given.http.force_login(user_with((CANCELLER, "team")))
    When.http.options(detail(front_end, stored))
    Then.http.status_code_is(status.HTTP_204_NO_CONTENT)
    assert "PUT" in Http.response["Allow"], Http.response["Allow"]
    When.http.options(api_url(front_end, "todos-list"))
    Then.http.status_code_is(status.HTTP_204_NO_CONTENT)
    assert "POST" in Http.response["Allow"], Http.response["Allow"]


# authorized_transitions must be a list of transition names: anything else
# is an invalid condition, which denies everywhere (as a malformed
# restrict_update is refused), rather than a substring or key test.
MALFORMED = [
    "cancel finish",
    "cancel",
    {"cancel": False},
    ["cancel", 1],
    [["cancel"]],
    None,
]


@pytest.mark.parametrize("transitions", MALFORMED, ids=repr)
def test_a_malformed_authorized_transitions_denies_everywhere(
    transitions, caplog, fresh_malformed_log
):
    from scrud_django.permissions import (
        _checking_transition,
        _checking_write,
        authorized_transitions,
    )

    configuration = {"authorized_transitions": transitions}
    with caplog.at_level("ERROR", logger="scrud_django.permissions"):
        with _checking_transition("cancel"):
            assert authorized_transitions({}, configuration) is False
        with _checking_transition(""):
            assert authorized_transitions({}, configuration) is False
        with _checking_write():
            assert authorized_transitions({}, configuration) is False
        assert authorized_transitions({}, configuration) is False
    assert "authorized_transitions must be a list" in caplog.text


@pytest.mark.django_db
@pytest.mark.parametrize("transitions", MALFORMED, ids=repr)
def test_a_put_under_a_malformed_authorized_transitions_is_refused(
    front_end, transitions
):
    stored = todo()
    client = client_with(
        (
            {"http.GET": [TODO], "http.PUT": {TODO: scrud_workflow(transitions)}},
            "team",
        )
    )
    canceled = dict(PLAN, status="canceled")
    response = client.put(
        detail(front_end, stored), canceled, format="json", **if_match(stored)
    )
    assert response.status_code == 403, response.content
    assert Resource.objects.get(pk=stored.pk).content == PLAN


@pytest.mark.django_db
def test_a_put_under_a_well_formed_authorized_transitions_cancels(front_end):
    stored = todo()
    client = client_with(
        (
            {"http.GET": [TODO], "http.PUT": {TODO: scrud_workflow(["cancel"])}},
            "team",
        )
    )
    canceled = dict(PLAN, status="canceled")
    response = client.put(
        detail(front_end, stored), canceled, format="json", **if_match(stored)
    )
    assert response.status_code == 200, response.content
    assert Resource.objects.get(pk=stored.pk).content == canceled


# Workflow errors do not give a resource's stored state to a writer who may
# not read the resource: they are the generic error (same status, type and
# title; no policy, no states), as under a policy the writer may not read.

WFP = "http://api.openteams.com/json-ld/WorkflowPolicy"
WORKFLOW_ERROR = "http://api.openteams.com/http-problem/workflow-transition-invalid"
# A policy in "team" the writers below may read (http.GET on WorkflowPolicy
# there), so that only the resource's readability decides.
TEAM_POLICY = {
    "resource_type": TODO,
    "rbac_context": "team",
    "initial_states": {"not started": 'status == "not started"'},
    "states": {
        "in progress": 'status == "in progress"',
        "canceled": 'status == "canceled"',
        "done": 'status == "done"',
    },
    "transitions": {
        "start": {"from": "not started", "to": "in progress"},
        "cancel": {"from": ["not started", "in progress"], "to": "canceled"},
    },
    "restrict_update": ["done"],
}
# (stored status, body status, the error's message, its prior_state)
STATE_ERRORS = {
    "invalid transition": ("done", "canceled", None, "done"),
    "restricted update": ("done", "done", "Update not allowed on this state", "done"),
    "no valid state": (
        "done",
        "blocked",
        "The request does not match a valid state",
        None,
    ),
    "transition not authorized": (
        "not started",
        "in progress",
        "The user isn't authorized to perform this state transition on this resource.",
        "not started",
    ),
}


def team_policy():
    from scrud_django.registration import workflow_policy_resource_type

    workflow_policy_resource_type()
    reverse("todos-list", urlconf="tests.urls")
    return ResourceRegistration.register(dict(TEAM_POLICY), "workflow-policies")


def put_state(front_end, client, stored, status):
    body = dict(PLAN, status=status, title="changed")
    return client.put(detail(front_end, stored), body, format="json", HTTP_IF_MATCH="*")


@pytest.mark.django_db
@pytest.mark.parametrize("shape", list(STATE_ERRORS))
def test_a_workflow_error_hides_the_stored_state_from_a_writer_who_cannot_read(
    front_end,
    shape,
):
    stored_status, status, _message, _prior = STATE_ERRORS[shape]
    team_policy()
    stored = todo(dict(PLAN, status=stored_status))
    canceller = client_with(
        (dict(CANCELLER, **{"http.GET": [WFP]}), "team"),
    )
    assert canceller.get(detail(front_end, stored)).status_code == 403
    response = put_state(front_end, canceller, stored, status)
    assert response.status_code == 400, response.content
    problem = response.json()
    assert problem["type"] == WORKFLOW_ERROR, problem
    assert problem["title"] == "Invalid workflow transition", problem
    assert problem["prior_state"] is None, problem
    assert problem["proposed_state"] is None, problem
    assert problem["policy"] is None, problem
    assert stored_status not in response.content.decode(), problem
    assert Resource.objects.get(pk=stored.pk).content == stored.content


@pytest.mark.django_db
@pytest.mark.parametrize("shape", list(STATE_ERRORS))
def test_a_workflow_error_gives_the_stored_state_to_a_writer_who_reads(
    front_end, shape
):
    stored_status, status, message, prior = STATE_ERRORS[shape]
    policy = team_policy()
    stored = todo(dict(PLAN, status=stored_status))
    reader = client_with(
        (dict(CANCELLER, **{"http.GET": [TODO, WFP]}), "team"),
    )
    response = put_state(front_end, reader, stored, status)
    assert response.status_code == 400, response.content
    problem = response.json()
    assert problem["type"] == WORKFLOW_ERROR, problem
    assert problem["prior_state"] == prior, problem
    assert problem["policy"].endswith(f"/{policy.pk}/"), problem
    if message is not None:
        assert problem["message"] == message, problem


# A malformed authorized_transitions is logged once per configuration per
# process, not on every evaluation (a list evaluates it once per row).


@pytest.fixture
def fresh_malformed_log():
    from scrud_django import permissions

    permissions._MALFORMED_REPORTED.clear()
    yield permissions
    permissions._MALFORMED_REPORTED.clear()


def malformed_records(caplog):
    return [
        r
        for r in caplog.records
        if r.name == "scrud_django.permissions"
        and "authorized_transitions must be a list" in r.getMessage()
    ]


def test_a_malformed_configuration_is_logged_once(fresh_malformed_log, caplog):
    from scrud_django.permissions import (
        _checking_transition,
        _checking_write,
        authorized_transitions,
    )

    first = {"authorized_transitions": "cancel finish"}
    second = {"authorized_transitions": {"cancel": False}}
    with caplog.at_level("ERROR", logger="scrud_django.permissions"):
        for _ in range(50):
            assert authorized_transitions({}, first) is False
            with _checking_write():
                assert authorized_transitions({}, first) is False
            with _checking_transition("cancel"):
                assert authorized_transitions({}, first) is False
        assert len(malformed_records(caplog)) == 1
        assert authorized_transitions({}, second) is False
        assert authorized_transitions({}, second) is False
        assert authorized_transitions({}, first) is False
    assert len(malformed_records(caplog)) == 2


def test_the_malformed_configurations_remembered_are_bounded(
    fresh_malformed_log, caplog
):
    from scrud_django.permissions import authorized_transitions

    limit = fresh_malformed_log._MALFORMED_REPORTED_MAX
    with caplog.at_level("ERROR", logger="scrud_django.permissions"):
        for n in range(limit + 10):
            assert (
                authorized_transitions({}, {"authorized_transitions": str(n)}) is False
            )
    assert len(fresh_malformed_log._MALFORMED_REPORTED) == limit
    assert len(malformed_records(caplog)) == limit + 10


@pytest.mark.django_db
def test_a_list_under_a_malformed_condition_logs_once(
    front_end, fresh_malformed_log, caplog
):
    for n in range(5):
        todo(dict(PLAN, title=f"plan {n}"))
    client = client_with(
        (
            {"http.GET": {TODO: scrud_workflow("cancel finish"), TODO_LIST: True}},
            "team",
        )
    )
    with caplog.at_level("ERROR", logger="scrud_django.permissions"):
        response = client.get(api_url(front_end, "todos-list"))
    assert listed_titles(response) == []
    assert len(malformed_records(caplog)) == 1
