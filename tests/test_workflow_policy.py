import pytest
from scrud_django.exceptions import (
    WorkflowInvalidInitialStateError,
    WorkflowInvalidStateError,
    WorkflowTransitionError,
)
from scrud_django.workflow import WorkflowPolicy


workflow_policy = WorkflowPolicy(
    {
        'context': 'test',
        'resource_type': 'test',
        'initial_states': {'start': 'status == "START"'},
        'states': {
            'middle': 'status == "MIDDLE"',
            'end': 'status == "END"',
            'unreachable': 'status == "UNREACHABLE"',
        },
        'transitions': {'start': ['middle', 'end'], 'middle': 'end'},
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


def test_invalid_transition():
    with pytest.raises(WorkflowTransitionError):
        workflow_policy.validate(start, start) # reflexive transitions must be explicit
        workflow_policy.validate(unreachable, start)
        workflow_policy.validate(unreachable, middle)
        workflow_policy.validate(unreachable, end)
        workflow_policy.validate(end, start)
        workflow_policy.validate(end, middle)
        workflow_policy.validate(end, unreachable)


def test_valid_transition():
    assert workflow_policy.validate(middle, start) == middle
    assert workflow_policy.validate(end, start) == end
    assert workflow_policy.validate(end, middle) == end