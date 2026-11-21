"""``SCRUD_WORKFLOW_ACTIONS[type_uri]["on_transition"]`` (#20): an action per
transition name, run once on commit, after ``on_save``, for each distinct
transition a write takes, with the contexts it was taken in."""

import json
import logging
from http import HTTPStatus
from uuid import uuid4

import pytest
from django.test import override_settings
from django.utils.http import quote_etag
from django.utils.timezone import now
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.core.signals import acting_as
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .fixtures import *  # noqa: F401, F403
from .front_ends import api_url, serve_ninja
from .post_commit_helpers import (
    default_context_todo_writer_user,
    todos_moved_after_save,
)
from .steps import *  # noqa: F403, F401
from .workflow_actions import transition_calls

TODO = "tests://ToDo"
ALL_METHODS = ("http.GET", "http.POST", "http.PUT", "http.DELETE")
TODO_WRITER = {method: [TODO] for method in ALL_METHODS}
NOT_STARTED = {
    "title": "Buy milk",
    "status": "not started",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
}
IN_PROGRESS = dict(NOT_STARTED, status="in progress")


def setting_of(name):
    return override_settings(SCRUD_WORKFLOW_ACTIONS=[f"tests.workflow_actions.{name}"])


@pytest.fixture(autouse=True)
def calls():
    transition_calls.clear()
    yield transition_calls
    transition_calls.clear()


def writer(*contexts):
    user = UserFactory()
    role = Role(definition=TODO_WRITER, etag=uuid4().hex, modified_at=now())
    role.save()
    for context in contexts:
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=context,
            etag=uuid4().hex,
            modified_at=now(),
        ).save()
    return user


def todo(**content):
    serve_ninja()  # registers the test types
    return ResourceRegistration.register(dict(NOT_STARTED, **content), "todos")


def put_todo(front_end, resource, content):
    When.http.put(
        api_url(front_end, "todos-detail", args=[resource.id]),
        data=json.dumps(content),
        content_type="application/json",
        HTTP_IF_MATCH=quote_etag(resource.etag),
    )


@pytest.mark.django_db
@test_context
def test_on_transition_runs_once_when_taken(
    front_end, django_capture_on_commit_callbacks
):
    user = writer("a")
    resource = todo(rbac_context="a")
    Given.http.force_login(user)
    with (
        setting_of("recording"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        put_todo(front_end, resource, dict(IN_PROGRESS, rbac_context="a"))
    Then.http.status_code_is(HTTPStatus.OK)
    assert [kind for kind, _ in transition_calls] == ["on_save", "start"]
    _, kwargs = transition_calls[1]
    assert kwargs["transition"] == "start"
    assert kwargs["contexts"] == ("a",)
    assert kwargs["user"] == user
    assert kwargs["decision"].get("a").transition.transition == "start"


@pytest.mark.django_db
@test_context
def test_on_transition_does_not_run_for_other_transitions(
    front_end, django_capture_on_commit_callbacks
):
    resource = todo(rbac_context="a")
    Given.http.force_login(writer("a"))
    with (
        setting_of("recording"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        # Same state: "__SAME_STATE__", which has no action.
        put_todo(front_end, resource, dict(NOT_STARTED, rbac_context="a", title="x"))
    Then.http.status_code_is(HTTPStatus.OK)
    assert [kind for kind, _ in transition_calls] == ["on_save"]


@pytest.mark.django_db
@test_context
def test_on_transition_does_not_run_on_rollback(
    front_end, django_capture_on_commit_callbacks
):
    api_url(front_end, "todos-list")
    resource = ResourceRegistration.register(dict(NOT_STARTED), "todos")
    Given.http.force_login(default_context_todo_writer_user())
    with (
        setting_of("recording"),
        todos_moved_after_save(),
        django_capture_on_commit_callbacks(execute=True),
    ):
        put_todo(front_end, resource, IN_PROGRESS)
    Then.http.status_code_is(HTTPStatus.FORBIDDEN)
    assert transition_calls == []


@pytest.mark.django_db
def test_a_transition_taken_in_two_contexts_runs_once_with_both(
    front_end, django_capture_on_commit_callbacks
):
    resource = todo(rbac_context=["a", "b"])
    user = UserFactory()
    with (
        setting_of("recording"),
        django_capture_on_commit_callbacks(execute=True),
        acting_as(user),
    ):
        ResourceRegistration.update(
            dict(IN_PROGRESS, rbac_context=["a", "b"]), "todos", str(resource.pk)
        )
    starts = [kwargs for kind, kwargs in transition_calls if kind == "start"]
    assert len(starts) == 1
    assert sorted(starts[0]["contexts"]) == ["a", "b"]
    assert starts[0]["user"] == user


@pytest.mark.django_db
def test_a_leaving_transition_fires_with_its_flag(
    front_end, django_capture_on_commit_callbacks
):
    resource = todo(rbac_context=["a", "b"])
    with (
        setting_of("recording"),
        django_capture_on_commit_callbacks(execute=True),
        acting_as(UserFactory()),
    ):
        ResourceRegistration.update(
            dict(IN_PROGRESS, rbac_context=["b"]), "todos", str(resource.pk)
        )
    starts = [kwargs for kind, kwargs in transition_calls if kind == "start"]
    assert len(starts) == 1
    decision = starts[0]["decision"]
    assert decision.get("a").leaving is True
    assert decision.get("b").leaving is False
    assert sorted(starts[0]["contexts"]) == ["a", "b"]


@pytest.mark.django_db
@test_context
def test_an_override_fires_on_transition_override(
    front_end, admin_login, django_capture_on_commit_callbacks
):
    resource = todo(status="nonsense", rbac_context="a")
    Given.http.force_login(admin_login)
    with (
        setting_of("recording"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        put_todo(front_end, resource, dict(NOT_STARTED, rbac_context="a"))
    Then.http.status_code_is(HTTPStatus.OK)
    assert [kind for kind, _ in transition_calls] == ["on_save", "__OVERRIDE__"]
    _, kwargs = transition_calls[1]
    assert kwargs["transition"] == "__OVERRIDE__"
    assert kwargs["contexts"] == ("a",)
    assert kwargs["decision"].get("a").override is True


@pytest.mark.django_db
@test_context
def test_a_failing_transition_action_fails_nothing(
    front_end, caplog, django_capture_on_commit_callbacks
):
    resource = todo(rbac_context="a")
    Given.http.force_login(writer("a"))
    with caplog.at_level(logging.ERROR, logger="scrud_django.workflow_actions"):
        with (
            setting_of("failing"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            put_todo(front_end, resource, dict(IN_PROGRESS, rbac_context="a"))
    Then.http.status_code_is(HTTPStatus.OK)
    assert Resource.objects.get(pk=resource.pk).content["status"] == "in progress"
    assert [kind for kind, _ in transition_calls] == ["on_save", "start"]
    (record,) = [r for r in caplog.records if r.name == "scrud_django.workflow_actions"]
    assert "on_transition[start]" in record.getMessage()


@pytest.mark.django_db
def test_a_save_without_a_decision_runs_no_transition_action(
    django_capture_on_commit_callbacks,
):
    from scrud_django.core.signals import scrud_post_save

    resource = todo()
    with (
        setting_of("recording"),
        django_capture_on_commit_callbacks(execute=True),
    ):
        scrud_post_save.send(
            sender=Resource,
            instance=resource,
            using="default",
            resource_type_uri=TODO,
            action="update",
        )
    assert [kind for kind, _ in transition_calls] == ["on_save"]


@pytest.mark.django_db
def test_a_string_on_transition_is_logged_as_e012_and_runs_nothing(
    caplog, django_capture_on_commit_callbacks
):
    resource = todo(rbac_context="a")
    user = UserFactory()
    with caplog.at_level(logging.ERROR, logger="scrud_django.workflow_actions"):
        with (
            setting_of("transition_string"),
            django_capture_on_commit_callbacks(execute=True),
            acting_as(user),
        ):
            ResourceRegistration.update(
                dict(IN_PROGRESS, rbac_context="a"), "todos", str(resource.pk)
            )
    assert Resource.objects.get(pk=resource.pk).content["status"] == "in progress"
    assert transition_calls == []
    (record,) = [r for r in caplog.records if r.name == "scrud_django.workflow_actions"]
    assert "E012" in record.getMessage()
    assert "E009" not in record.getMessage()
