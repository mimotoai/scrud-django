import pytest
from django.core.exceptions import ImproperlyConfigured
from scoped_rbac.cel import InvalidExpression

from scrud_django.exceptions import (
    WorkflowInvalidInitialStateError,
    WorkflowInvalidStateError,
    WorkflowTransitionError,
)
from scrud_django.workflow import TransitionData, WorkflowPolicy

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


# States whose conditions overlap: content that is "review" and published is
# stored in "published", the first state it matches.
overlapping_policy = {
    "initial_states": {"draft": 'status == "draft" && published == false'},
    "states": {
        "published": "published == true",
        "review": 'status == "review"',
    },
    "transitions": {
        "submit": {"from": "draft", "to": "review"},
        "publish": {"from": "review", "to": "published"},
    },
}
draft = {"status": "draft", "published": False}


def test_transition_is_authorized_into_the_state_the_content_is_stored_in():
    policy = WorkflowPolicy(overlapping_policy)
    proposed = {"status": "review", "published": True}
    assert policy.determine_state(proposed, policy.state_expressions) == "published"
    # "submit" leads from "draft" to "review", which the content also matches,
    # but it would be stored in "published": no transition leads there.
    with pytest.raises(WorkflowTransitionError) as excinfo:
        policy.determine_transition(proposed, draft)
    error = excinfo.value
    assert (error.prior_state, error.proposed_state) == ("draft", "published")
    with pytest.raises(WorkflowTransitionError):
        policy.validate(proposed, draft)

    submitted = {"status": "review", "published": False}
    assert policy.determine_transition(submitted, draft) == TransitionData(
        "review", "draft", "submit"
    )
    published = {"status": "review", "published": True}
    assert policy.determine_transition(published, submitted) == TransitionData(
        "published", "review", "publish"
    )


def test_content_staying_in_its_state_is_a_same_state_update():
    # "archive" leads from "review" to "archived", which the content also
    # matches; it is stored in "review", its first match, so it stays there.
    policy = {
        "initial_states": {"review": 'status == "review"'},
        "states": {"archived": "archived == true"},
        "transitions": {"archive": {"from": "review", "to": "archived"}},
    }
    prior = {"status": "review", "archived": False}
    proposed = {"status": "review", "archived": True}
    assert WorkflowPolicy(policy).determine_transition(
        proposed, prior
    ) == TransitionData("review", "review", "__SAME_STATE__")

    restricted = WorkflowPolicy(dict(policy, restrict_update=["review"]))
    with pytest.raises(WorkflowTransitionError) as excinfo:
        restricted.determine_transition(proposed, prior)
    error = excinfo.value
    assert (error.prior_state, error.proposed_state) == ("review", "review")
    assert error.message == "Update not allowed on this state"


def test_a_state_in_both_initial_states_and_states_is_refused():
    # Two conditions for one state: creates would read one, updates the
    # other.
    with pytest.raises(ImproperlyConfigured) as excinfo:
        WorkflowPolicy(
            {
                "initial_states": {"draft": 'status == "draft"'},
                "states": {
                    "published": "published == true",
                    "draft": 'status == "draft" && published == false',
                },
                "transitions": {"publish": {"from": "draft", "to": "published"}},
            }
        )
    assert str(excinfo.value) == (
        "Invalid workflow state 'draft': it is named in both initial_states and "
        "states; a state has one condition."
    )


def test_initial_state_is_the_state_the_content_is_stored_in():
    # Content that meets an initial state's condition and a later state's is
    # created in, and stored in, the initial state: the first match.
    policy = WorkflowPolicy(
        {
            "initial_states": {"draft": 'status == "draft"'},
            "states": {"published": "published == true"},
            "transitions": {"publish": {"from": "draft", "to": "published"}},
        }
    )
    published_draft = {"status": "draft", "published": True}
    assert policy.state(published_draft) == "draft"
    assert policy.initial_state(published_draft) == "draft"
    assert policy.validate(published_draft) == published_draft
    assert policy.initial_transition(published_draft) == TransitionData(
        "draft", None, "__INITIALIZE__"
    )
    # Content in a later state only is no initial state.
    published = {"status": "live", "published": True}
    assert policy.state(published) == "published"
    assert policy.initial_state(published) is None
    with pytest.raises(WorkflowInvalidInitialStateError):
        policy.initial_transition(published)


# "published" may not change in place; "republish" leads to it from it and
# from "archived" (a self-loop only through its list), "shelve" to
# "archived" from it and from "published".
republish_policy = {
    "initial_states": {"draft": 'status == "draft"'},
    "states": {
        "published": 'status == "published"',
        "archived": 'status == "archived"',
    },
    "transitions": {
        "publish": {"from": "draft", "to": "published"},
        "republish": {"from": ["published", "archived"], "to": ["published"]},
        "shelve": {"from": ["published", "archived"], "to": "archived"},
    },
    "restrict_update": ["published"],
}
published = {"status": "published", "title": "a"}
archived = {"status": "archived", "title": "a"}


@pytest.mark.parametrize(
    "transition",
    [
        {"from": ["published", "archived"], "to": ["published"]},
        {"from": "published", "to": ["published", "archived"]},
        {"from": ["published", "archived"], "to": ["published", "archived"]},
    ],
    ids=["from-list", "to-list", "both-lists"],
)
def test_a_restricted_state_is_not_updated_in_place_by_a_list_self_loop(transition):
    policy = WorkflowPolicy(
        dict(
            republish_policy,
            transitions={
                "publish": {"from": "draft", "to": "published"},
                "republish": transition,
            },
        )
    )
    edited = dict(published, title="edited")
    with pytest.raises(WorkflowTransitionError) as excinfo:
        policy.determine_transition(edited, published)
    error = excinfo.value
    assert (error.prior_state, error.proposed_state) == ("published", "published")
    assert error.message == "Update not allowed on this state"
    with pytest.raises(WorkflowTransitionError):
        policy.validate(edited, published)


def test_a_list_self_loop_still_names_its_other_pairs_and_unrestricted_states():
    policy = WorkflowPolicy(republish_policy)
    # The pairs of "republish" that are not a restricted self-loop.
    assert policy.determine_transition(published, archived) == TransitionData(
        "published", "archived", "republish"
    )
    # "archived" is not restricted: its self-loop through "shelve"'s list
    # names "shelve", as before.
    edited = dict(archived, title="edited")
    assert policy.determine_transition(edited, archived) == TransitionData(
        "archived", "archived", "shelve"
    )
    assert policy.determine_transition(archived, published) == TransitionData(
        "archived", "published", "shelve"
    )


@pytest.mark.parametrize(
    "edit",
    [
        {"from": "published", "to": "published"},
        {"from": ["published"], "to": ["published"]},
    ],
    ids=["names", "single-element-lists"],
)
def test_a_restricted_state_is_updated_in_place_by_its_own_self_loop(edit):
    policy = WorkflowPolicy(
        dict(
            republish_policy,
            transitions=dict(
                republish_policy["transitions"],
                republish={"from": "archived", "to": "published"},
                edit=edit,
            ),
        )
    )
    edited = dict(published, title="edited")
    assert policy.determine_transition(edited, published) == TransitionData(
        "published", "published", "edit"
    )
    assert policy.validate(edited, published) == edited


def test_two_transitions_covering_one_pair_are_a_configuration_error():
    # "publish" and "republish" both lead from "archived" to "published":
    # which one a writer needs would depend on their order.
    policy = dict(
        republish_policy,
        transitions={
            "publish": {"from": ["draft", "archived"], "to": "published"},
            "republish": {"from": ["published", "archived"], "to": ["published"]},
        },
    )
    with pytest.raises(ImproperlyConfigured) as excinfo:
        WorkflowPolicy(policy)
    assert str(excinfo.value) == (
        "Invalid workflow transitions 'publish' and 'republish': both lead from "
        "'archived' to 'published'; a pair of states may have one transition."
    )


def test_one_transition_naming_a_state_twice_is_no_duplicate():
    policy = WorkflowPolicy(
        dict(
            republish_policy,
            transitions={"publish": {"from": ["draft", "draft"], "to": "published"}},
        )
    )
    assert policy.determine_transition(published, {"status": "draft"}) == (
        TransitionData("published", "draft", "publish")
    )
