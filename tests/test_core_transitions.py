"""`scrud_django.core.transitions` on plain mappings and a type IRI."""

import logging

import pytest

from scrud_django.core.decisions import NULL_CONTENT, WorkflowDecision
from scrud_django.core.transitions import (
    Transitions,
    log_skipped_check,
    transitions_between,
)
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


@pytest.mark.django_db
def test_null_stored_content_is_an_update_from_no_state():
    # Stored content that is JSON null (NULL_CONTENT) is decided as the
    # workflow check decides it: an update from no state (no transition
    # leads from there), not a create. transitions_between already decided
    # NULL_CONTENT so; what changed is that Resource.get_transitions now
    # passes it for a stored null row, which it reported as a create
    # (test_a_resource_stored_with_null_content_is_an_update_from_no_state).
    result = transitions_between(
        TYPE_IRI,
        prior=NULL_CONTENT,
        proposed=NOT_STARTED,
        contexts_before=[""],
        contexts_after=[""],
    )
    assert result.transitions[""] is None
    assert type(result.errors[""]) is WorkflowTransitionError
    assert (result.errors[""].prior_state, result.errors[""].proposed_state) == (
        None,
        "not started",
    )
    assert result.prior is None
    assert not result.checked
    assert not result.get("").entering


@pytest.mark.django_db
def test_transitions_between_builds_an_unchecked_decision():
    result = transitions_between(
        TYPE_IRI,
        prior=NOT_STARTED,
        proposed=NOT_STARTED,
        contexts_before=["a"],
        contexts_after=["a"],
        stored_pk=7,
        actor="someone",
    )
    assert isinstance(result, WorkflowDecision)
    assert isinstance(result, Transitions)
    assert (result.type_iri, result.stored_pk, result.actor) == (
        TYPE_IRI,
        7,
        "someone",
    )
    assert not result.checked
    assert result.transitions["a"].prior_state == "not started"


@pytest.mark.django_db
def test_a_resource_stored_with_null_content_is_an_update_from_no_state():
    from django.db.models import JSONField, Value
    from django.urls import reverse

    from scrud_django.models import Resource

    reverse("todos-list", urlconf="tests.urls")  # registers the test types
    resource = ResourceRegistration.register(dict(NOT_STARTED), "todos")
    # A row whose content is JSON null (only stored before 0.12.0) is an
    # update from no state in the default context, as the workflow check
    # decides it; it was reported as a create (__INITIALIZE__). So is
    # content stored then that is not an object.
    for stored in (Value(None, output_field=JSONField()), ["x"]):
        Resource.objects.filter(pk=resource.pk).update(content=stored)
        resource.content = dict(NOT_STARTED)
        result = resource.get_transitions()
        assert result.transitions[""] is None
        assert type(result.errors[""]) is WorkflowTransitionError
        assert result.stored_pk == resource.pk
        assert result.prior is None
    assert Resource.objects.get(pk=resource.pk).content == ["x"]
    # A create (no row) is still a create.
    created = Resource(resource_type=resource.resource_type, content=NOT_STARTED)
    result = created.get_transitions()
    assert result.transitions[""].transition == INITIALIZE
    assert result.stored_pk is None
