"""Settings default workflow policies (``SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]``)
are built and checked once, an invalid one is reported by ``manage.py check``,
and at request time it affects only writes of its own type."""

import importlib.resources
import json
from unittest import mock

import pytest
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from scrud_django import conf
from scrud_django.checks import check_default_workflow_policies
from scrud_django.workflow import WorkflowPolicy, default_workflow_policy_for

from .factories import UserFactory
from .fixtures import partner_profile_post_data  # noqa: F401

TODO = "tests://ToDo"


def todo_policy():
    text = (
        importlib.resources.files("tests.static.todo_flow")
        .joinpath("workflow.json")
        .read_text()
    )
    return json.loads(text)


# ``restrict_update`` must be a list of state names, not a string.
INVALID_TODO_DEFAULT = dict(todo_policy(), restrict_update="not started")


def invalid_todo_default():
    return override_settings(
        SCRUD_WORKFLOW={"DEFAULT_WORKFLOW_POLICIES": {TODO: INVALID_TODO_DEFAULT}}
    )


def admin_client():
    client = APIClient()
    client.force_authenticate(UserFactory(is_staff=True, is_superuser=True))
    return client


def test_defaults_are_built_once():
    first = conf.default_workflow_policies()
    assert conf.default_workflow_policies() is first
    assert default_workflow_policy_for(TODO) is first[TODO]


def test_overriding_the_setting_rebuilds_the_defaults():
    before = default_workflow_policy_for(TODO)
    permissive = {"initial_states": {"any": "true"}, "states": {}, "transitions": {}}
    with override_settings(
        SCRUD_WORKFLOW={"DEFAULT_WORKFLOW_POLICIES": {TODO: permissive}}
    ):
        policy = default_workflow_policy_for(TODO)
        assert isinstance(policy, WorkflowPolicy)
        assert policy.policy == permissive
    assert default_workflow_policy_for(TODO).policy == before.policy


def test_valid_defaults_pass_the_system_check():
    assert check_default_workflow_policies(None) == []


# run_checks() imports the URLconf, which registers resource types.
@pytest.mark.django_db
def test_invalid_default_is_a_system_check_error():
    with invalid_todo_default():
        errors = check_default_workflow_policies(None)
        assert errors == [
            checks.Error(
                f"Invalid default workflow policy for {TODO!r}: Invalid workflow "
                "restrict_update: it must be a list of defined state names, not "
                "'not started'.",
                hint='Fix SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"].',
                obj=TODO,
                id="scrud_django.E001",
            )
        ]
        # It is registered, so ``manage.py check`` reports it.
        assert errors[0] in checks.run_checks()


@pytest.mark.django_db
def test_a_default_with_two_transitions_for_one_pair_is_a_system_check_error():
    # "abandon" leads from "blocked" to "canceled", as "cancel" does.
    transitions = dict(
        todo_policy()["transitions"], abandon={"from": "blocked", "to": "canceled"}
    )
    duplicated = dict(todo_policy(), transitions=transitions)
    with override_settings(
        SCRUD_WORKFLOW={"DEFAULT_WORKFLOW_POLICIES": {TODO: duplicated}}
    ):
        errors = check_default_workflow_policies(None)
        assert errors == [
            checks.Error(
                f"Invalid default workflow policy for {TODO!r}: Invalid workflow "
                "transitions 'cancel' and 'abandon': both lead from 'blocked' to "
                "'canceled'; a pair of states may have one transition.",
                hint='Fix SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"].',
                obj=TODO,
                id="scrud_django.E001",
            )
        ]
        assert errors[0] in checks.run_checks()


def test_unloadable_defaults_are_a_system_check_error():
    with override_settings(
        SCRUD_WORKFLOW={"DEFAULT_WORKFLOW_POLICIES": "tests.no_such_module.policies"}
    ):
        errors = check_default_workflow_policies(None)
        assert [e.id for e in errors] == ["scrud_django.E001"]
        assert "tests.no_such_module.policies" in errors[0].msg


@pytest.mark.django_db
def test_invalid_default_breaks_only_writes_of_its_type(partner_profile_post_data):
    reverse("todos-list")
    client = admin_client()
    with invalid_todo_default():
        response = client.post(
            reverse("partner-profiles-list"), partner_profile_post_data, format="json"
        )
        assert response.status_code == 201, response.content

        response = client.post(
            reverse("json-ld-list"),
            {"@context": {"x": "http://x.example/"}},
            format="json",
        )
        assert response.status_code == 201, response.content

        with pytest.raises(ImproperlyConfigured):
            client.post(
                reverse("todos-list"),
                {"title": "Buy milk", "status": "not started"},
                format="json",
            )


def test_a_default_may_be_a_workflow_policy():
    # The default is a copy of the configured object that knows its type
    # (workflow errors name a default by type); the object itself is left
    # alone, and the compiled conditions are shared, not compiled again.
    policy = WorkflowPolicy(todo_policy())
    with override_settings(
        SCRUD_WORKFLOW={"DEFAULT_WORKFLOW_POLICIES": {TODO: policy}}
    ):
        default = default_workflow_policy_for(TODO)
        assert isinstance(default, WorkflowPolicy)
        assert default.policy is policy.policy
        assert default.state_expressions is policy.state_expressions
        assert default.allowed_transitions is policy.allowed_transitions
        assert default.default_for == TODO
        assert policy.default_for is None
        assert check_default_workflow_policies(None) == []


def test_one_workflow_policy_object_may_be_the_default_of_two_types():
    from scrud_django.exceptions import DefaultPolicyReference

    other = "tests://Other"
    policy = WorkflowPolicy(todo_policy())
    with override_settings(
        SCRUD_WORKFLOW={"DEFAULT_WORKFLOW_POLICIES": {TODO: policy, other: policy}}
    ):
        assert default_workflow_policy_for(TODO).reference == (
            DefaultPolicyReference(TODO)
        )
        assert default_workflow_policy_for(other).reference == (
            DefaultPolicyReference(other)
        )


def test_test_defaults_are_policy_content():
    from tests.workflow_defaults import workflow_policies

    assert workflow_policies() == {TODO: todo_policy()}


def test_a_callable_setting_is_called_once():
    from django.conf import settings

    import tests.workflow_defaults

    calls = mock.Mock(wraps=tests.workflow_defaults.workflow_policies)
    with (
        mock.patch.object(tests.workflow_defaults, "workflow_policies", calls),
        override_settings(SCRUD_WORKFLOW=dict(settings.SCRUD_WORKFLOW)),
    ):
        for _ in range(3):
            assert default_workflow_policy_for(TODO).policy == todo_policy()
        assert calls.call_count == 1


def raising_workflow_policies():
    return {TODO: WorkflowPolicy(INVALID_TODO_DEFAULT)}


def test_a_callable_that_raises_is_one_error_for_the_setting():
    # It builds WorkflowPolicy objects eagerly: the invalid one cannot be
    # isolated to its type (give mappings instead).
    path = "tests.test_default_workflow_policies.raising_workflow_policies"
    with override_settings(SCRUD_WORKFLOW={"DEFAULT_WORKFLOW_POLICIES": path}):
        errors = check_default_workflow_policies(None)
        assert [e.id for e in errors] == ["scrud_django.E001"]
        assert errors[0].obj is None
        assert "could not be loaded" in errors[0].msg
        with pytest.raises(ImproperlyConfigured):
            default_workflow_policy_for("tests://PartnerProfiles")
