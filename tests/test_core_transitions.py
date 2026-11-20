"""`scrud_django.core.transitions` on plain mappings and a type IRI."""

import logging

import pytest

from scrud_django.core.transitions import log_skipped_check, transitions_between
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)
from scrud_django.workflow import INITIALIZE, LEAVE, WorkflowTransitionError

TYPE_IRI = "tests://ToDo"
NO_DEFAULT = "tests://NoDefault"
NOT_STARTED = {"status": "not started"}
DONE = {"status": "done", "priority": "high", "assignee": "me"}


@pytest.mark.django_db
def test_a_create_reports_initialize_in_each_context():
    result = transitions_between(
        TYPE_IRI,
        prior=None,
        proposed=NOT_STARTED,
        contexts_before=[],
        contexts_after=["a"],
    )
    assert result.errors == {}
    data = result.transitions["a"]
    assert (data.proposed_state, data.transition) == ("not started", INITIALIZE)
    assert result.transition_data == data


@pytest.mark.django_db
def test_an_invalid_transition_is_reported_not_raised():
    result = transitions_between(
        TYPE_IRI,
        prior=DONE,
        proposed=NOT_STARTED,
        contexts_before=["a"],
        contexts_after=["a"],
    )
    assert result.transition_data is None
    assert type(result.transition_error) is WorkflowTransitionError
    assert "a" in result.errors


@pytest.mark.django_db
def test_leaving_a_context_with_a_policy_reports_leave():
    result = transitions_between(
        TYPE_IRI,
        prior=NOT_STARTED,
        proposed=NOT_STARTED,
        contexts_before=["a"],
        contexts_after=["b"],
    )
    left = result.transitions["a"]
    assert (left.proposed_state, left.prior_state, left.transition) == (
        None,
        "not started",
        LEAVE,
    )
    entered = result.transitions["b"]
    assert (entered.proposed_state, entered.transition) == ("not started", INITIALIZE)
    assert result.errors == {}


def test_log_skipped_check_keeps_the_resource_wording(caplog):
    error = WorkflowTransitionError(None, "a", "b", message="nope")
    with caplog.at_level(logging.WARNING, logger="scrud_django.workflow"):
        log_skipped_check({"a": error}, subject="resource pk 7", type_iri=TYPE_IRI)
    records = [r for r in caplog.records if r.name == "scrud_django.workflow"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert (
        records[0]
        .getMessage()
        .startswith("Saving resource pk 7 of tests://ToDo skipped the workflow check")
    )


@pytest.mark.django_db
def test_leaving_into_a_context_without_a_policy():
    # A type with no settings default: only its stored policy, in "a", applies.
    workflow_policy_resource_type()
    ResourceRegistration.register(
        {
            "resource_type": NO_DEFAULT,
            "initial_states": {"any": "true"},
            "states": {},
            "transitions": {},
            "rbac_context": "a",
        },
        "workflow-policies",
    )
    result = transitions_between(
        NO_DEFAULT,
        prior={"rbac_context": "a"},
        proposed={"rbac_context": "b"},
        contexts_before=["a"],
        contexts_after=["b"],
    )
    left = result.transitions["a"]
    assert (left.proposed_state, left.prior_state, left.transition) == (
        None,
        "any",
        LEAVE,
    )
    assert "b" not in result.transitions
    assert result.transition_data is None
    assert result.errors == {}
