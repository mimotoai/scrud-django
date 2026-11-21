"""`scrud_django.core.decisions`: the decision types and the per-context
computation every workflow check shares."""

import dataclasses

import pytest

from scrud_django.core.decisions import (
    NULL_CONTENT,
    ContextDecision,
    WorkflowDecision,
    concerned_policies,
    context_transition,
    decision_key,
    is_entering,
    normalized_contexts_after,
    same_transitions,
    snapshot_prior,
)
from scrud_django.exceptions import DefaultPolicyReference, WorkflowTransitionError
from scrud_django.workflow import INITIALIZE, LEAVE, SAME_STATE, TransitionData

TYPE_IRI = "tests://ToDo"
REF = DefaultPolicyReference(TYPE_IRI)
NOT_STARTED = {"status": "not started"}


def make(contexts_after=("a",), contexts=(), **kwargs):
    fields = dict(
        type_iri=TYPE_IRI,
        stored_pk=None,
        actor=None,
        checked=True,
        prior=None,
        proposed=NOT_STARTED,
    )
    fields.update(kwargs)
    return WorkflowDecision(contexts_after=contexts_after, contexts=contexts, **fields)


@pytest.mark.django_db
def test_contexts_follow_stored_then_new_array_order():
    pairs = concerned_policies(
        TYPE_IRI, contexts_before=["b", "a"], contexts_after=["c", "a"]
    )
    assert [c for c, _ in pairs] == ["b", "a", "c"]


@pytest.mark.django_db
def test_no_context_is_the_settings_default_under_none():
    pairs = concerned_policies(TYPE_IRI, contexts_before=[], contexts_after=[])
    assert [c for c, _ in pairs] == [None]


@pytest.mark.django_db
def test_a_type_without_a_policy_is_dropped():
    assert (
        concerned_policies("tests://NoDefault", contexts_before=[], contexts_after=[])
        == []
    )


@pytest.mark.django_db
def test_context_transition_of_a_create_is_initialize():
    policy = concerned_policies(TYPE_IRI, contexts_before=[], contexts_after=[])[0][1]
    data = context_transition(policy, None, NOT_STARTED, entering=True)
    assert data.transition == INITIALIZE
    assert data.prior_state is None
    done = {"status": "done", "priority": "high", "assignee": "me"}
    assert context_transition(policy, NULL_CONTENT, NOT_STARTED, entering=True) == data
    moved = context_transition(policy, NOT_STARTED, done, entering=False)
    assert moved.prior_state == "not started"


def test_a_create_enters_every_context():
    assert is_entering("a", prior=None, contexts_before=[])
    assert is_entering(None, prior=None, contexts_before=[])


def test_an_update_enters_only_a_new_named_context():
    assert is_entering("b", prior=NOT_STARTED, contexts_before=["a"])
    assert not is_entering("a", prior=NOT_STARTED, contexts_before=["a"])
    assert not is_entering(None, prior=NOT_STARTED, contexts_before=["a"])


def test_decision_key_is_the_context_in_a_tuple():
    assert decision_key("a") == ("a",)
    assert decision_key(None) == (None,)


def test_snapshot_prior_drops_null_content():
    assert snapshot_prior(None) is None
    assert snapshot_prior(NULL_CONTENT) is None
    assert snapshot_prior(NOT_STARTED) is NOT_STARTED


def test_snapshots_do_not_follow_later_changes():
    body = {"status": "not started"}
    prior = {"status": "x"}
    decision = make(proposed=body, prior=prior)
    body["status"] = "done"
    prior["status"] = "y"
    assert decision.proposed == {"status": "not started"}
    assert decision.prior == {"status": "x"}


def test_lists_become_tuples_and_no_context_is_none():
    decision = make(contexts_after=["a"], contexts=[])
    assert decision.contexts_after == ("a",)
    assert decision.contexts == ()
    # A decision holds the contexts its builder gives it; no context at all
    # is None (the settings default) by `normalized_contexts_after`.
    assert make(contexts_after=[]).contexts_after == ()
    assert make(contexts_after=normalized_contexts_after([], [])).contexts_after == (
        None,
    )


def test_normalized_contexts_after():
    assert normalized_contexts_after([], []) == (None,)
    assert normalized_contexts_after([], ["a"]) == ("a",)
    # An update out of every context concerns those it leaves alone.
    assert normalized_contexts_after(["a"], []) == ()
    assert normalized_contexts_after(["a"], ["b", "a"]) == ("b", "a")


@pytest.mark.django_db
def test_an_update_out_of_every_context_concerns_no_default():
    pairs = concerned_policies(TYPE_IRI, contexts_before=["a"], contexts_after=[])
    assert [c for c, _ in pairs] == ["a"]


def entry(context, state="a"):
    return ContextDecision(
        context=context,
        policy=REF,
        transition=TransitionData(state, None, INITIALIZE),
        error=None,
        entering=True,
    )


def test_transition_data_is_the_first_new_context_with_an_entry():
    a_entry, b_entry = entry("a", "sa"), entry("b", "sb")
    decision = make(contexts_after=("x", "b"), contexts=(a_entry, b_entry))
    assert decision.transition_data == b_entry.transition
    assert decision.transition_error is None
    assert decision.transitions == {"a": a_entry.transition, "b": b_entry.transition}
    assert decision.get("a") is a_entry
    assert decision.get("zzz") is None
    with pytest.raises(TypeError):
        decision.transitions["a"] = None


def test_errors_and_transition_error():
    error = WorkflowTransitionError(REF, None, None)
    bad = ContextDecision(
        context="a", policy=REF, transition=None, error=error, entering=False
    )
    decision = make(contexts_after=("a",), contexts=(bad,))
    assert decision.errors == {"a": error}
    assert decision.transitions == {"a": None}
    assert decision.transition_data is None
    assert decision.transition_error is error


def test_a_context_decision_has_a_transition_or_an_error():
    with pytest.raises(ValueError):
        ContextDecision(
            context="a", policy=REF, transition=None, error=None, entering=False
        )
    with pytest.raises(ValueError):
        ContextDecision(
            context="a",
            policy=REF,
            transition=TransitionData("s", None, INITIALIZE),
            error=WorkflowTransitionError(REF, None, None),
            entering=False,
        )


def test_decisions_are_frozen():
    decision = make()
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.checked = False


def test_null_content_moved_and_re_exported():
    from scrud_django.core import workflow

    assert workflow.NULL_CONTENT is NULL_CONTENT
    assert workflow.NULL_STORED_CONTENT is NULL_CONTENT


IN_PROGRESS = {"status": "in progress", "priority": "high", "assignee": "me"}


def request_by(method, user):
    from django.test import RequestFactory

    request = getattr(RequestFactory(), method)("/")
    request.user = user
    return request


@pytest.mark.django_db
def test_a_checked_and_an_unchecked_decision_agree():
    from scrud_django.core.transitions import transitions_between
    from scrud_django.core.workflow import Create, Update, workflow_check

    from .factories import UserFactory

    # A superuser is allowed every transition: the check refuses nothing.
    user = UserFactory(is_superuser=True)
    checked = workflow_check(
        request_by("post", user),
        type_iri=TYPE_IRI,
        write=Create(proposed=NOT_STARTED, contexts_after=["a"]),
    )
    unchecked = transitions_between(
        TYPE_IRI,
        prior=None,
        proposed=NOT_STARTED,
        contexts_before=[],
        contexts_after=["a"],
    )
    assert checked.contexts == unchecked.contexts
    assert [entry.context for entry in checked.contexts] == ["a"]
    assert checked.contexts[0].transition.transition == INITIALIZE
    assert checked.contexts[0].entering
    # An update that stays in "a" and moves into "b" (it leaves none).
    checked = workflow_check(
        request_by("put", user),
        type_iri=TYPE_IRI,
        write=Update(
            prior=NOT_STARTED,
            proposed=IN_PROGRESS,
            readable=lambda: True,
            stored_pk=7,
            contexts_before=["a"],
            contexts_after=["a", "b"],
        ),
    )
    unchecked = transitions_between(
        TYPE_IRI,
        prior=NOT_STARTED,
        proposed=IN_PROGRESS,
        contexts_before=["a"],
        contexts_after=["a", "b"],
    )
    assert checked.contexts == unchecked.contexts
    stayed, entered = checked.contexts
    assert (stayed.context, stayed.transition.transition, stayed.entering) == (
        "a",
        "start",
        False,
    )
    assert (entered.context, entered.transition.transition, entered.entering) == (
        "b",
        INITIALIZE,
        True,
    )
    assert checked.checked and not unchecked.checked
    # Under a stored policy in "a" (and the settings default in "b"): the
    # two decisions name the same version of the stored policy.
    from scrud_django.exceptions import StoredPolicyReference
    from scrud_django.registration import (
        ResourceRegistration,
        workflow_policy_resource_type,
    )

    from .workflow_defaults import read_workflow_policy

    workflow_policy_resource_type()
    stored = ResourceRegistration.register(
        dict(
            read_workflow_policy("tests.static.todo_flow", "workflow.json"),
            rbac_context="a",
        ),
        "workflow-policies",
    )
    checked = workflow_check(
        request_by("put", user),
        type_iri=TYPE_IRI,
        write=Update(
            prior=NOT_STARTED,
            proposed=IN_PROGRESS,
            readable=lambda: True,
            stored_pk=7,
            contexts_before=["a"],
            contexts_after=["a", "b"],
        ),
    )
    unchecked = transitions_between(
        TYPE_IRI,
        prior=NOT_STARTED,
        proposed=IN_PROGRESS,
        contexts_before=["a"],
        contexts_after=["a", "b"],
    )
    assert checked.contexts == unchecked.contexts
    stayed, entered = checked.contexts
    assert isinstance(stayed.policy, StoredPolicyReference)
    assert stayed.policy.resource.pk == stored.pk
    assert stayed.transition.transition == "start"
    assert entered.policy == DefaultPolicyReference(TYPE_IRI)


@pytest.mark.django_db
def test_stored_policy_references_compare_by_version():
    from scrud_django.exceptions import StoredPolicyReference
    from scrud_django.registration import (
        ResourceRegistration,
        workflow_policy_resource_type,
    )

    from .workflow_defaults import read_workflow_policy

    workflow_policy_resource_type()
    policy = dict(
        read_workflow_policy("tests.static.todo_flow", "workflow.json"),
        rbac_context="a",
    )
    stored = ResourceRegistration.register(policy, "workflow-policies")
    again = type(stored).objects.get(pk=stored.pk)
    assert StoredPolicyReference(stored) == StoredPolicyReference(again)
    assert hash(StoredPolicyReference(stored)) == hash(StoredPolicyReference(again))
    assert StoredPolicyReference(stored) != DefaultPolicyReference(TYPE_IRI)
    assert StoredPolicyReference(stored) != stored
    # Another version of the policy (a new etag) is another reference.
    again.etag = "another"
    assert StoredPolicyReference(stored) != StoredPolicyReference(again)


@pytest.mark.django_db
def test_an_update_out_of_every_context_decides_alike_with_no_default():
    from scrud_django.core.transitions import transitions_between
    from scrud_django.core.workflow import Update, workflow_check
    from scrud_django.workflow import LEAVE, SAME_STATE

    from .factories import UserFactory

    # An own model's update from "a" into no context: only "a" is
    # concerned, by both paths (no None entry, no settings default).
    user = UserFactory(is_superuser=True)
    checked = workflow_check(
        request_by("put", user),
        type_iri=TYPE_IRI,
        write=Update(
            prior=NOT_STARTED,
            proposed=NOT_STARTED,
            readable=lambda: True,
            stored_pk=7,
            contexts_before=["a"],
            contexts_after=[],
        ),
    )
    unchecked = transitions_between(
        TYPE_IRI,
        prior=NOT_STARTED,
        proposed=NOT_STARTED,
        contexts_before=["a"],
        contexts_after=[],
    )
    assert [e.context for e in checked.contexts] == ["a"]
    assert [e.context for e in unchecked.contexts] == ["a"]
    assert checked.get(None) is None and unchecked.get(None) is None
    assert checked.contexts_after == unchecked.contexts_after == ()
    (checked_a,) = checked.contexts
    (unchecked_a,) = unchecked.contexts
    assert (checked_a.policy, checked_a.entering, checked_a.error) == (
        unchecked_a.policy,
        unchecked_a.entering,
        unchecked_a.error,
    )
    # "a" is left: the check decides it as an ordinary transition, the
    # unchecked path still reports __LEAVE__ until #36 (Task 6) makes the
    # two entries equal.
    assert checked_a.transition.transition == SAME_STATE
    assert unchecked_a.transition.transition == LEAVE


@pytest.mark.django_db
def test_a_create_in_no_context_decides_the_settings_default():
    from scrud_django.core.transitions import transitions_between
    from scrud_django.core.workflow import Create, workflow_check

    from .factories import UserFactory

    user = UserFactory(is_superuser=True)
    checked = workflow_check(
        request_by("post", user),
        type_iri=TYPE_IRI,
        write=Create(proposed=NOT_STARTED, contexts_after=[]),
    )
    unchecked = transitions_between(
        TYPE_IRI,
        prior=None,
        proposed=NOT_STARTED,
        contexts_before=[],
        contexts_after=[],
    )
    assert checked.contexts == unchecked.contexts
    (entry,) = checked.contexts
    assert entry.context is None
    assert entry.policy == DefaultPolicyReference(TYPE_IRI)
    assert entry.transition.transition == INITIALIZE
    assert checked.contexts_after == unchecked.contexts_after == (None,)
    assert checked.transition_data == unchecked.transition_data == entry.transition


@pytest.mark.django_db
def test_workflow_check_returns_the_decision_with_its_actor():
    from scrud_django.core.workflow import Update, workflow_check

    from .factories import UserFactory

    user = UserFactory(is_superuser=True)
    decision = workflow_check(
        request_by("put", user),
        type_iri=TYPE_IRI,
        write=Update(
            prior=NOT_STARTED,
            proposed=IN_PROGRESS,
            readable=lambda: True,
            stored_pk=7,
            contexts_before=["a"],
            contexts_after=["a"],
        ),
    )
    assert isinstance(decision, WorkflowDecision)
    assert decision.actor == user
    assert decision.checked
    assert decision.stored_pk == 7
    assert decision.type_iri == TYPE_IRI
    assert decision.prior == NOT_STARTED
    assert decision.proposed == IN_PROGRESS
    assert decision.contexts_after == ("a",)
    assert decision.transitions["a"].transition == "start"


@pytest.mark.django_db
def test_an_anonymous_or_absent_requester_is_no_actor():
    from django.contrib.auth.models import AnonymousUser

    from scrud_django.core.workflow import Create, workflow_check

    # No policy applies to this type: no request is needed, nor any user.
    decision = workflow_check(
        None, type_iri="tests://NoDefault", write=Create(proposed={})
    )
    assert decision.contexts == ()
    assert decision.actor is None
    assert decision.stored_pk is None
    assert decision.checked
    decision = workflow_check(
        request_by("post", AnonymousUser()),
        type_iri="tests://NoDefault",
        write=Create(proposed={}),
    )
    assert decision.actor is None


def leaving(transition):
    return ContextDecision("a", REF, transition, None, False)


def test_same_transitions_accepts_a_left_context_leave_from_the_same_state():
    checked = make(
        contexts_after=("b",),
        contexts=[leaving(TransitionData("draft", "draft", SAME_STATE))],
    )
    saved = make(
        checked=False,
        contexts_after=("b",),
        contexts=[leaving(TransitionData(None, "draft", LEAVE))],
    )
    assert same_transitions(checked, saved)


def test_same_transitions_refuses_a_left_context_leave_from_another_state():
    checked = make(
        contexts_after=("b",),
        contexts=[leaving(TransitionData("draft", "draft", SAME_STATE))],
    )
    saved = make(
        checked=False,
        contexts_after=("b",),
        contexts=[leaving(TransitionData(None, "published", LEAVE))],
    )
    assert not same_transitions(checked, saved)
