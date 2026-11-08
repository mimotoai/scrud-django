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


# State conditions compile without type checking, so one that reads a
# property the governed type's JSON Schema does not define (a misspelling)
# compiles and makes its state unreachable: a database check warns.


@pytest.mark.parametrize(
    "condition, properties, undeclared",
    [
        ('status == "x"', {"status"}, []),
        ('stauts == "x" || status == "y"', {"status"}, ["stauts"]),
        ('stauts == "x" && stauts != "y" || sise > 1', {"status"}, ["sise", "stauts"]),
        # A qualified name is a property's field.
        ("a.b.c == 1", {"status"}, ["a"]),
        ("has(assignee.name)", {"status"}, ["assignee"]),
        # Functions (unknown ones too) and macro variables are not properties.
        ("foo(status)", {"status"}, []),
        ("status.foo()", {"status"}, []),
        ("size(status) > 0 && status.startsWith('a')", {"status"}, []),
        ("blockers.exists(b, b.note != null)", {"blockers"}, []),
        ("type(status) == string && status != null", {"status"}, []),
        # A type error is not a missing property.
        ('status == "x" && 1 == "1"', {"status"}, []),
        # A property name that is not a CEL identifier is never read.
        ('status == "x"', {"status", "first-name", "in"}, []),
        # One that does not compile is E001's.
        ('stauts == "x" &&', {"status"}, []),
    ],
)
def test_undeclared_properties(condition, properties, undeclared):
    from scrud_django.workflow import undeclared_properties

    assert undeclared_properties(condition, properties) == undeclared


@pytest.mark.parametrize(
    "schema, properties",
    [
        ({"properties": {"a": {}, "b": {}}}, {"a", "b"}),
        ({"properties": {"a": {}}, "additionalProperties": False}, {"a"}),
        # Other properties are allowed, or may be defined elsewhere.
        ({"properties": {"a": {}}, "additionalProperties": True}, None),
        ({"properties": {"a": {}}, "additionalProperties": {"type": "string"}}, None),
        ({"properties": {"a": {}}, "patternProperties": {"^x": {}}}, None),
        ({"properties": {"a": {}}, "allOf": [{"properties": {"b": {}}}]}, None),
        ({"properties": {"a": {}}, "$ref": "#/definitions/Other"}, None),
        ({"type": "object"}, None),
        ({"properties": ["a"]}, None),
        (["a"], None),
    ],
)
def test_schema_properties(schema, properties):
    from scrud_django.checks import schema_properties

    assert schema_properties(schema) == properties


@pytest.mark.django_db
def test_default_policy_conditions_pass_the_database_check():
    from scrud_django.checks import check_default_policy_conditions

    reverse("todos-list")
    assert check_default_policy_conditions(None, databases=["default"]) == []


@pytest.mark.django_db
def test_a_condition_reading_a_property_the_schema_lacks_is_a_warning():
    from scrud_django.checks import check_default_policy_conditions

    reverse("todos-list")
    states = dict(todo_policy()["states"], done='stauts == "done" && assignee != null')
    misspelled = dict(todo_policy(), states=states)
    with override_settings(
        SCRUD_WORKFLOW={"DEFAULT_WORKFLOW_POLICIES": {TODO: misspelled}}
    ):
        # A valid policy: no E001.
        assert check_default_workflow_policies(None) == []
        found = check_default_policy_conditions(None, databases=["default"])
        assert found == [
            checks.Warning(
                f"The condition of state 'done' of the default workflow policy "
                f"for {TODO!r} reads 'stauts', which the JSON Schema of "
                f"{TODO!r} does not define: content that lacks it never meets "
                "the condition.",
                hint=(
                    "Fix the condition (a misspelled property?) or define the "
                    "property in the schema; a schema that expects other "
                    'properties says so with "additionalProperties": true.'
                ),
                obj=TODO,
                id="scrud_django.W005",
            )
        ]
        # Registered as a database check: `check --database` and `migrate`
        # run it.
        assert found[0] in checks.run_checks(databases=["default"])
        assert found[0] not in checks.run_checks()
        assert check_default_policy_conditions(None) == []


@pytest.mark.django_db
def test_a_condition_may_read_rbac_context(monkeypatch):
    # Every resource may have one, whether its schema defines it or not.
    from scrud_django import checks as scrud_checks

    reverse("todos-list")
    original = scrud_checks.schema_properties

    def without_rbac_context(schema):
        properties = original(schema)
        assert "rbac_context" in properties
        return properties - {"rbac_context"}

    monkeypatch.setattr(scrud_checks, "schema_properties", without_rbac_context)
    initial_states = dict(
        todo_policy()["initial_states"],
        mine='rbac_context == "a" && title == "x"',
    )
    policy = dict(todo_policy(), initial_states=initial_states)
    with override_settings(
        SCRUD_WORKFLOW={"DEFAULT_WORKFLOW_POLICIES": {TODO: policy}}
    ):
        assert (
            scrud_checks.check_default_policy_conditions(None, databases=["default"])
            == []
        )
