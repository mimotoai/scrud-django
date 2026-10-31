"""A grant conditioned on ``scrud_workflow`` grants workflow transitions and
nothing else (fail closed). It allows the checks of a POST or PUT that the
``WorkflowValidator`` authorizes (the request check, the PUT's object check
and the post-save check) and, in the workflow check, the transitions it
lists. Everything else is refused: GET, HEAD, lists, DELETE, and a write the
workflow check does not cover. A role that should read needs its own
``http.GET`` grant."""

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


def client_with(*grants):
    """A client whose user holds each ``(definition, context)`` of `grants`."""
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
    client = APIClient()
    client.force_authenticate(user)
    return client


def todo(content=PLAN):
    reverse("todos-list")
    return ResourceRegistration.register(dict(content), "todos")


def detail(resource):
    return reverse("todos-detail", args=[resource.pk])


def if_match(resource):
    return {"HTTP_IF_MATCH": quote_etag(Resource.objects.get(pk=resource.pk).etag)}


def listed_titles(response):
    assert response.status_code == 200, response.content
    return sorted(entry["content"]["title"] for entry in response.json()["content"])


# The review's "may only cancel ToDos in team" role: every action on ToDo
# conditioned on the cancel transition, and the list type unconditionally.
CANCELLER = {"*": {TODO: scrud_workflow(["cancel"]), TODO_LIST: True}}


@pytest.mark.django_db
def test_a_cancel_only_role_cannot_read_list_or_delete():
    stored = todo()
    client = client_with((CANCELLER, "team"))
    assert client.get(detail(stored)).status_code == 403
    assert client.head(detail(stored)).status_code == 403
    response = client.get(reverse("todos-list"))
    assert listed_titles(response) == []
    assert response.json()["count"] == 0
    assert client.head(reverse("todos-list")).status_code == 200
    response = client.delete(detail(stored), **if_match(stored))
    assert response.status_code == 403, response.content
    assert Resource.objects.filter(pk=stored.pk).exists()


@pytest.mark.django_db
def test_a_cancel_only_role_still_cancels_and_nothing_else():
    stored = todo()
    client = client_with((CANCELLER, "team"))
    # A create is __INITIALIZE__, not listed.
    before = Resource.objects.count()
    response = client.post(reverse("todos-list"), PLAN, format="json")
    assert response.status_code == 400, response.content
    assert Resource.objects.count() == before
    # "start" is not listed.
    started = dict(PLAN, status="in progress")
    response = client.put(detail(stored), started, format="json", **if_match(stored))
    assert response.status_code == 400, response.content
    assert Resource.objects.get(pk=stored.pk).content == PLAN
    # "cancel" is.
    canceled = dict(PLAN, status="canceled")
    response = client.put(detail(stored), canceled, format="json", **if_match(stored))
    assert response.status_code == 200, response.content
    assert Resource.objects.get(pk=stored.pk).content == canceled


@pytest.mark.django_db
def test_a_cancel_only_role_with_its_own_get_grant_reads():
    # What the Upgrading line tells adopters: a role that should read needs
    # its own http.GET grant.
    stored = todo()
    client = client_with((CANCELLER, "team"), ({"http.GET": [TODO]}, "team"))
    assert client.get(detail(stored)).status_code == 200
    assert listed_titles(client.get(reverse("todos-list"))) == ["private plan"]
    response = client.delete(detail(stored), **if_match(stored))
    assert response.status_code == 403, response.content


@pytest.mark.django_db
def test_per_method_grants_conditioned_on_scrud_workflow_allow_no_get_or_delete():
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
    assert client.get(detail(stored)).status_code == 403
    assert listed_titles(client.get(reverse("todos-list"))) == []
    response = client.delete(detail(stored), **if_match(stored))
    assert response.status_code == 403, response.content
    assert Resource.objects.filter(pk=stored.pk).exists()


@pytest.mark.django_db
def test_the_list_and_the_detail_route_agree():
    # GET outright in "open", only through scrud_workflow in "team": the list
    # shows the "open" ToDo alone, and the detail route hides the "team" one
    # (404: it is not readable), as the list leaves it out.
    hidden = todo()
    shown = todo(dict(PLAN, title="open plan", rbac_context="open"))
    client = client_with(
        ({"http.GET": [TODO, TODO_LIST]}, "open"),
        ({"http.GET": {TODO: scrud_workflow(["cancel"])}}, "team"),
    )
    assert listed_titles(client.get(reverse("todos-list"))) == ["open plan"]
    assert client.get(detail(shown)).status_code == 200
    assert client.get(detail(hidden)).status_code == 404


@pytest.mark.django_db
def test_a_put_refused_by_its_object_check_hides_what_scrud_workflow_would_read():
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
    response = client.put(detail(stored), canceled, format="json", **if_match(stored))
    assert response.status_code == 404, response.content
    assert Resource.objects.get(pk=stored.pk).content == PLAN


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
def test_a_malformed_authorized_transitions_denies_everywhere(transitions, caplog):
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
def test_a_put_under_a_malformed_authorized_transitions_is_refused(transitions):
    stored = todo()
    client = client_with(
        (
            {"http.GET": [TODO], "http.PUT": {TODO: scrud_workflow(transitions)}},
            "team",
        )
    )
    canceled = dict(PLAN, status="canceled")
    response = client.put(detail(stored), canceled, format="json", **if_match(stored))
    assert response.status_code == 403, response.content
    assert Resource.objects.get(pk=stored.pk).content == PLAN


@pytest.mark.django_db
def test_a_put_under_a_well_formed_authorized_transitions_cancels():
    stored = todo()
    client = client_with(
        (
            {"http.GET": [TODO], "http.PUT": {TODO: scrud_workflow(["cancel"])}},
            "team",
        )
    )
    canceled = dict(PLAN, status="canceled")
    response = client.put(detail(stored), canceled, format="json", **if_match(stored))
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
    reverse("todos-list")
    return ResourceRegistration.register(dict(TEAM_POLICY), "workflow-policies")


def put_state(client, stored, status):
    body = dict(PLAN, status=status, title="changed")
    return client.put(detail(stored), body, format="json", HTTP_IF_MATCH="*")


@pytest.mark.django_db
@pytest.mark.parametrize("shape", list(STATE_ERRORS))
def test_a_workflow_error_hides_the_stored_state_from_a_writer_who_cannot_read(
    shape,
):
    stored_status, status, _message, _prior = STATE_ERRORS[shape]
    team_policy()
    stored = todo(dict(PLAN, status=stored_status))
    canceller = client_with(
        (dict(CANCELLER, **{"http.GET": [WFP]}), "team"),
    )
    assert canceller.get(detail(stored)).status_code == 403
    response = put_state(canceller, stored, status)
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
def test_a_workflow_error_gives_the_stored_state_to_a_writer_who_reads(shape):
    stored_status, status, message, prior = STATE_ERRORS[shape]
    policy = team_policy()
    stored = todo(dict(PLAN, status=stored_status))
    reader = client_with(
        (dict(CANCELLER, **{"http.GET": [TODO, WFP]}), "team"),
    )
    response = put_state(reader, stored, status)
    assert response.status_code == 400, response.content
    problem = response.json()
    assert problem["type"] == WORKFLOW_ERROR, problem
    assert problem["prior_state"] == prior, problem
    assert problem["policy"].endswith(f"/{policy.pk}/"), problem
    if message is not None:
        assert problem["message"] == message, problem
