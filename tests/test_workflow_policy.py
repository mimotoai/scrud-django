import pytest
from django.core.exceptions import ImproperlyConfigured
from scoped_rbac.cel import InvalidExpression

from scrud_django.exceptions import (
    WorkflowInvalidInitialStateError,
    WorkflowInvalidStateError,
    WorkflowTransitionError,
)
from scrud_django.workflow import WorkflowPolicy

workflow_policy = WorkflowPolicy(
    {
        'rbac_context': 'test',
        'resource_type': 'test',
        'initial_states': {'start': 'status == "START"'},
        'states': {
            'middle': 'status == "MIDDLE"',
            'end': 'status == "END"',
            'unreachable': 'status == "UNREACHABLE"',
        },
        'transitions': {
            'move to middle': {'from': 'start', 'to': 'middle'},
            'move to end': {'from': ['start', 'middle'], 'to': 'end'},
        },
        'restrict_update': ['start', 'end'],
    }
)


start = {"status": "START"}
middle = {"status": "MIDDLE"}
end = {"status": "END"}
unreachable = {"status": "UNREACHABLE"}


def test_invalid_initial_state():
    with pytest.raises(WorkflowInvalidInitialStateError):
        workflow_policy.validate({"status": "Not a valid initial status"})


def test_valid_initial_state():
    assert workflow_policy.validate(start) == start


def test_invalid_state():
    with pytest.raises(WorkflowInvalidStateError):
        workflow_policy.validate({"bogus": "not a valid state!"}, start)


@pytest.mark.parametrize(
    "proposed_state, prior_state",
    [
        # (start, start),   # reflexive transitions must be explicit
        (start, middle),
        (start, end),
        (middle, end),
        (unreachable, start),
        (unreachable, middle),
        (unreachable, end),
    ],
)
def test_invalid_transition(proposed_state, prior_state):
    with pytest.raises(WorkflowTransitionError):
        workflow_policy.validate(proposed_state, prior_state)


@pytest.mark.parametrize(
    "proposed_state, prior_state", [(middle, start), (end, start), (end, middle)]
)
def test_valid_transition(proposed_state, prior_state):
    assert workflow_policy.validate(proposed_state, prior_state) == proposed_state


@pytest.mark.parametrize("proposed_state, prior_state", [(middle, middle)])
def test_same_state_transition(proposed_state, prior_state):
    assert workflow_policy.validate(proposed_state, prior_state) == proposed_state


@pytest.mark.parametrize("proposed_state, prior_state", [(start, start), (end, end)])
def test_same_state_update_restricted(proposed_state, prior_state):
    with pytest.raises(WorkflowTransitionError):
        workflow_policy.validate(proposed_state, prior_state)


def test_invalid_state_expression_is_a_configuration_error():
    policy = {
        "initial_states": {"draft": "status == "},
        "states": {},
        "transitions": {},
    }
    with pytest.raises(ImproperlyConfigured) as excinfo:
        WorkflowPolicy(policy)
    assert isinstance(excinfo.value.__cause__, InvalidExpression)


def test_condition_on_missing_property_is_no_match():
    # "priority" is absent from the data: evaluating the first condition fails,
    # which counts as "no match" (not an error), so the next state still matches.
    policy = WorkflowPolicy(
        {
            "initial_states": {
                "urgent": 'priority == "HIGH"',
                "open": 'status == "OPEN"',
            },
            "states": {},
            "transitions": {},
        }
    )
    data = {"status": "OPEN"}
    assert policy.determine_state(data, policy.initial_state_expressions) == "open"
    assert policy.determine_state({}, policy.initial_state_expressions) is None
    assert policy.validate(data) == data


@pytest.mark.parametrize(
    "restrict_update", [5, True, None, "start", ["unknown"], [5], {"start": 1}]
)
def test_invalid_restrict_update_is_a_configuration_error(restrict_update):
    policy = {
        "initial_states": {"start": 'status == "START"'},
        "states": {},
        "transitions": {},
        "restrict_update": restrict_update,
    }
    with pytest.raises(ImproperlyConfigured):
        WorkflowPolicy(policy)


def test_restrict_update_may_be_absent_or_a_list_of_states():
    policy = {
        "initial_states": {"start": 'status == "START"'},
        "states": {},
        "transitions": {},
    }
    WorkflowPolicy(policy)
    WorkflowPolicy(dict(policy, restrict_update=[]))
    WorkflowPolicy(dict(policy, restrict_update=["start"]))
