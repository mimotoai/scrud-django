"""Superusers override the workflow policy where it would refuse (#31).

A superuser (``is_active and is_superuser``, `is_workflow_superuser`) may
make a write the policy of a context would refuse there: from content in no
state, or in more than one, with no transition between the states, into a
``restrict_update`` state edited in place, or a create (or move in) in no
initial state. The context's transition is then ``__OVERRIDE__``
(`OVERRIDE`), from and to the one state each content is in (``None``: none,
or several), and the decision keeps the error it overrode. Where the policy
allows the write, it is reported as its own transition: no override.

What still applies: the type's JSON Schema, the RBAC checks, and the type's
own hook checks, which are asked about the transition ``__OVERRIDE__``.
"""

import json
import logging
from contextlib import contextmanager
from http import HTTPStatus
from uuid import uuid4

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.utils.http import quote_etag
from django.utils.timezone import now
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.core.authz import current_transition, is_workflow_superuser
from scrud_django.core.types import TypeOptions, register_type_options, type_options
from scrud_django.exceptions import (
    WorkflowAmbiguousStateError,
    WorkflowInvalidInitialStateError,
    WorkflowTransitionError,
)
from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)
from scrud_django.workflow import OVERRIDE, RESERVED_TRANSITIONS, TransitionData

from .factories import UserFactory
from .fixtures import *  # noqa: F401, F403
from .front_ends import api_url, serve_ninja
from .steps import *  # noqa: F403, F401

TODO = "tests://ToDo"
WORKFLOW_ERROR = "http://api.openteams.com/http-problem/workflow-transition-invalid"
JSON_INVALID = "https://api.openteams.com/http-problem/json-invalid"
ALL_METHODS = ("http.GET", "http.POST", "http.PUT", "http.DELETE")
TODO_WRITER = {method: [TODO] for method in ALL_METHODS}

NOT_STARTED = {
    "title": "Buy milk",
    "status": "not started",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
}

# Two states whose conditions overlap: "in progress" with "high" priority
# meets both "published" and "review".
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

# "done" may not change in place: no transition names it alone on each side.
RESTRICTED_POLICY = {
    "resource_type": TODO,
    "initial_states": {"not started": 'status == "not started"'},
    "states": {"done": 'status == "done"'},
    "transitions": {"finish": {"from": "not started", "to": "done"}},
    "restrict_update": ["done"],
}


def user_with(*grants):
    """A user holding each ``(role definition, context)`` of `grants`."""
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


def todo(**content):
    """A ToDo stored server-side, past the workflow check."""
    serve_ninja()  # registers the test types
    return ResourceRegistration.register(dict(NOT_STARTED, **content), "todos")


def store_policy(content):
    workflow_policy_resource_type()
    return ResourceRegistration.register(content, "workflow-policies")


def put_todo(front_end, resource, content):
    When.http.put(
        api_url(front_end, "todos-detail", args=[resource.id]),
        data=json.dumps(content),
        content_type="application/json",
        HTTP_IF_MATCH=quote_etag(resource.etag),
    )


def post_todo(front_end, content):
    When.http.post(
        api_url(front_end, "todos-list"),
        data=json.dumps(content),
        content_type="application/json",
    )


def assert_unchanged(resource):
    stored = Resource.objects.get(pk=resource.pk)
    assert stored.content == resource.content
    assert stored.etag == resource.etag


@pytest.fixture
def reported_saves():
    """The keyword arguments of each ToDo ``scrud_post_save``, in order."""
    from scrud_django.core.signals import scrud_post_save

    reported = []

    def record(sender, resource_type_uri=None, **kwargs):
        if resource_type_uri == TODO:
            reported.append(kwargs)

    scrud_post_save.connect(record, weak=False, dispatch_uid="test-override-saves")
    try:
        yield reported
    finally:
        scrud_post_save.disconnect(dispatch_uid="test-override-saves")


def the_decision(reported_saves):
    (reported,) = reported_saves
    decision = reported["decision"]
    assert decision.checked is True
    assert reported["transitions"] == decision.transitions
    return decision


@pytest.mark.django_db
@test_context
def test_a_superuser_updates_content_stored_in_no_state(
    front_end, admin_login, reported_saves
):
    resource = todo(status="nonsense", rbac_context="a")
    Given.http.force_login(admin_login)
    reported_saves.clear()
    proposed = dict(NOT_STARTED, rbac_context="a")
    put_todo(front_end, resource, proposed)
    Then.http.status_code_is(HTTPStatus.OK)
    assert Resource.objects.get(pk=resource.pk).content == proposed
    decision = the_decision(reported_saves)
    assert decision.transitions["a"] == TransitionData(
        "not started", None, "__OVERRIDE__"
    )
    assert decision.overrides == ("a",)
    entry = decision.get("a")
    assert entry.override is True
    assert entry.error is None
    assert isinstance(entry.overridden, WorkflowTransitionError)
    assert (entry.overridden.prior_state, entry.overridden.proposed_state) == (
        None,
        "not started",
    )
    assert decision.actor == admin_login


@pytest.mark.django_db
@test_context
def test_a_regular_writer_is_still_refused_from_no_state(front_end, reported_saves):
    resource = todo(status="nonsense", rbac_context="a")
    Given.http.force_login(user_with((TODO_WRITER, "a")))
    reported_saves.clear()
    put_todo(front_end, resource, dict(NOT_STARTED, rbac_context="a"))
    Then.http.status_code_is(HTTPStatus.BAD_REQUEST)
    assert Http.response.json()["type"] == WORKFLOW_ERROR
    assert_unchanged(resource)
    assert reported_saves == []


@pytest.mark.django_db
@test_context
def test_a_superuser_updates_ambiguous_stored_content(
    front_end, admin_login, reported_saves
):
    store_policy(dict(OVERLAPPING_POLICY, rbac_context="w"))
    resource = todo(**dict(AMBIGUOUS, rbac_context="w"))
    Given.http.force_login(admin_login)
    reported_saves.clear()
    proposed = dict(NOT_STARTED, status="done", priority="high", rbac_context="w")
    put_todo(front_end, resource, proposed)
    Then.http.status_code_is(HTTPStatus.OK)
    assert Resource.objects.get(pk=resource.pk).content == proposed
    decision = the_decision(reported_saves)
    data = decision.transitions["w"]
    assert data.transition == "__OVERRIDE__"
    assert data.prior_state is None
    assert data == TransitionData("published", None, OVERRIDE)
    assert decision.overrides == ("w",)
    overridden = decision.get("w").overridden
    assert isinstance(overridden, WorkflowAmbiguousStateError), overridden
    assert overridden.matching_states == ["published", "review"]


@pytest.mark.django_db
@test_context
def test_a_superuser_edits_a_restricted_state_in_place(
    front_end, admin_login, reported_saves
):
    store_policy(dict(RESTRICTED_POLICY, rbac_context="w"))
    done = todo(status="done", rbac_context="w")
    Given.http.force_login(admin_login)
    reported_saves.clear()
    edited = dict(done.content, title="edited")
    put_todo(front_end, done, edited)
    Then.http.status_code_is(HTTPStatus.OK)
    assert Resource.objects.get(pk=done.pk).content == edited
    decision = the_decision(reported_saves)
    assert decision.transitions["w"] == TransitionData("done", "done", OVERRIDE)
    assert decision.overrides == ("w",)
    assert decision.get("w").overridden.message == "Update not allowed on this state"


@pytest.mark.django_db
@test_context
def test_a_superuser_moves_content_in_no_state_out_of_a_context(
    front_end, admin_login, reported_saves
):
    resource = todo(status="nonsense", rbac_context="a")
    Given.http.force_login(admin_login)
    reported_saves.clear()
    put_todo(front_end, resource, dict(NOT_STARTED, rbac_context="b"))
    Then.http.status_code_is(HTTPStatus.OK)
    decision = the_decision(reported_saves)
    left = decision.transitions["a"]
    assert left.leaving is True
    assert left.transition == "__OVERRIDE__"
    assert left == TransitionData("not started", None, OVERRIDE, leaving=True)
    assert decision.get("a").leaving is True
    # Entering "b" is a valid creation there: no override.
    assert decision.transitions["b"] == TransitionData(
        "not started", None, "__INITIALIZE__"
    )
    assert decision.overrides == ("a",)


@pytest.mark.django_db
@test_context
def test_a_superuser_creates_content_in_no_initial_state(
    front_end, admin_login, reported_saves
):
    serve_ninja()  # registers the test types
    Given.http.force_login(admin_login)
    reported_saves.clear()
    post_todo(front_end, dict(NOT_STARTED, status="done", rbac_context="a"))
    Then.http.status_code_is(HTTPStatus.CREATED)
    decision = the_decision(reported_saves)
    assert decision.transitions["a"] == TransitionData("done", None, OVERRIDE)
    assert decision.get("a").entering is True
    assert isinstance(decision.get("a").overridden, WorkflowInvalidInitialStateError)
    assert decision.overrides == ("a",)


@pytest.mark.django_db
@test_context
def test_a_valid_superuser_write_is_its_own_transition(
    front_end, admin_login, reported_saves, caplog
):
    resource = todo(rbac_context="a")
    Given.http.force_login(admin_login)
    reported_saves.clear()
    with caplog.at_level(logging.DEBUG, logger="scrud_django.workflow"):
        put_todo(
            front_end,
            resource,
            dict(NOT_STARTED, status="in progress", rbac_context="a"),
        )
    Then.http.status_code_is(HTTPStatus.OK)
    decision = the_decision(reported_saves)
    assert decision.transitions["a"] == TransitionData(
        "in progress", "not started", "start"
    )
    entry = decision.get("a")
    assert entry.override is False
    assert entry.overridden is None
    assert decision.overrides == ()
    warnings = [
        record
        for record in caplog.records
        if record.name == "scrud_django.workflow" and record.levelno >= logging.WARNING
    ]
    assert warnings == []


@pytest.mark.django_db
@test_context
def test_a_superuser_body_failing_the_schema_is_still_refused(
    front_end, admin_login, reported_saves
):
    resource = todo(status="nonsense", rbac_context="a")
    Given.http.force_login(admin_login)
    reported_saves.clear()
    put_todo(
        front_end, resource, dict(NOT_STARTED, status="whatever", rbac_context="a")
    )
    Then.http.status_code_is(HTTPStatus.BAD_REQUEST)
    assert Http.response.json()["type"] == JSON_INVALID, Http.response.json()
    assert_unchanged(resource)
    assert reported_saves == []


def refuses_override_and_cancel(request, resource_type, body, context):
    """A ToDo check refusing the transitions ``__OVERRIDE__`` and
    ``cancel`` (``current_transition`` is ``None`` outside the workflow
    check: the request, object and post-save checks allow)."""
    return current_transition() not in (OVERRIDE, "cancel")


@contextmanager
def todo_checks(*checks):
    """The block in which the ToDo type has the hook `checks`; its hooks as
    they were registered before once it ends."""
    before = type_options(TODO)
    register_type_options(TODO, TypeOptions(checks=checks), slug="todos")
    try:
        yield
    finally:
        register_type_options(TODO, before, slug="todos")


@pytest.mark.django_db
@test_context
def test_the_types_own_checks_still_refuse_a_superusers_override(
    front_end, admin_login, reported_saves
):
    in_no_state = todo(status="nonsense", rbac_context="a")
    not_started = todo(rbac_context="a")
    with todo_checks(refuses_override_and_cancel):
        # A writer's transition the check refuses.
        Given.http.force_login(user_with((TODO_WRITER, "a")))
        put_todo(
            front_end,
            not_started,
            dict(NOT_STARTED, status="canceled", rbac_context="a"),
        )
        Then.http.status_code_is(HTTPStatus.BAD_REQUEST)
        refused = Http.response.json()
        assert refused["type"] == WORKFLOW_ERROR, refused
        assert_unchanged(not_started)
        # The superuser's override, which the check refuses alike.
        Given.http.force_login(admin_login)
        reported_saves.clear()
        put_todo(front_end, in_no_state, dict(NOT_STARTED, rbac_context="a"))
        Then.http.status_code_is(HTTPStatus.BAD_REQUEST)
        body = Http.response.json()
        assert body["type"] == refused["type"], body
        assert body["message"] == refused["message"], body
        assert (body["prior_state"], body["proposed_state"]) == (None, "not started")
        assert_unchanged(in_no_state)
        assert reported_saves == []
    # Without the check, the same override is made.
    put_todo(front_end, in_no_state, dict(NOT_STARTED, rbac_context="a"))
    Then.http.status_code_is(HTTPStatus.OK)


@pytest.mark.django_db
def test_is_workflow_superuser():
    def request_by(user):
        request = RequestFactory().put("/")
        request.user = user
        return request

    assert is_workflow_superuser(request_by(UserFactory(is_superuser=True))) is True
    inactive = UserFactory(is_superuser=True, is_active=False)
    assert is_workflow_superuser(request_by(inactive)) is False
    assert is_workflow_superuser(request_by(UserFactory(is_staff=True))) is False
    assert is_workflow_superuser(request_by(AnonymousUser())) is False
    assert is_workflow_superuser(RequestFactory().put("/")) is False


def test_override_is_reserved_and_takes_any_states():
    assert OVERRIDE == "__OVERRIDE__"
    assert OVERRIDE in RESERVED_TRANSITIONS
    for proposed, prior in [(None, None), ("a", None), (None, "b"), ("a", "b")]:
        for leaving in (False, True):
            data = TransitionData(proposed, prior, OVERRIDE, leaving=leaving)
            assert (data.proposed_state, data.prior_state) == (proposed, prior)
            assert data.leaving is leaving


def test_a_context_decision_overrides_exactly_when_it_names_the_error():
    from scrud_django.core.decisions import ContextDecision
    from scrud_django.exceptions import DefaultPolicyReference

    policy = DefaultPolicyReference(TODO)
    error = WorkflowTransitionError(policy, None, "done")
    override = TransitionData("done", None, OVERRIDE)
    entry = ContextDecision("a", policy, override, None, False, overridden=error)
    assert entry.override is True
    with pytest.raises(ValueError):
        ContextDecision("a", policy, override, None, False)
    with pytest.raises(ValueError):
        ContextDecision(
            "a",
            policy,
            TransitionData("done", "not started", "finish"),
            None,
            False,
            overridden=error,
        )
