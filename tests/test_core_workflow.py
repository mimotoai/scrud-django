"""The workflow check without DRF (`scrud_django.core.workflow`): a write's
transition under the policy of every context it concerns, asked with a plain
Django request, a type IRI and plain JSON mappings (no ``ResourceType`` row,
no stored ``Resource``), as an app's own models will ask it."""

from uuid import uuid4

import pytest
from django.test import RequestFactory
from django.urls import reverse
from django.utils.timezone import now
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.exceptions import (
    HiddenWorkflowTransitionError,
    WorkflowTransitionError,
)
from scrud_django.permissions import (
    _workflow_authorized,
    current_transition,
)
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)

from .factories import UserFactory
from .steps import *  # noqa: F403, F401

NOTE = "tests://Note"
WORKFLOW_POLICY = "http://api.openteams.com/json-ld/WorkflowPolicy"

# Two states in context "a": a draft is published by "publish".
NOTE_POLICY = {
    "resource_type": NOTE,
    "rbac_context": "a",
    "initial_states": {"draft": 'state == "draft"'},
    "states": {"published": 'state == "published"'},
    "transitions": {"publish": {"from": "draft", "to": "published"}},
}

DRAFT = {"state": "draft", "rbac_context": "a"}
PUBLISHED = {"state": "published", "rbac_context": "a"}


def may_transition(*transitions):
    """A role reading notes and their policies, and writing a note only by
    one of `transitions`."""
    return {
        "http.GET": [NOTE, WORKFLOW_POLICY],
        "http.PUT": {
            NOTE: {
                "condition": {
                    "operator": "scrud_workflow",
                    "authorized_transitions": list(transitions),
                }
            }
        },
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


def put_request(user):
    request = RequestFactory().put("/")
    request.user = user
    return request


def store_note_policy():
    reverse("todos-list")
    workflow_policy_resource_type()
    return ResourceRegistration.register(dict(NOTE_POLICY), "workflow-policies")


def publish(request, **kwargs):
    from scrud_django.core.workflow import workflow_check

    return workflow_check(
        request, type_iri=NOTE, prior=DRAFT, proposed=PUBLISHED, **kwargs
    )


@pytest.mark.django_db
@test_context
def test_allowed_transition_marks_request():
    store_note_policy()
    request = put_request(user_with((may_transition("publish"), "a")))
    assert not _workflow_authorized(request)
    ((context, policy, transition),) = publish(request)
    assert context == "a"
    assert policy.policy["transitions"] == NOTE_POLICY["transitions"]
    assert transition.transition == "publish"
    assert (transition.prior_state, transition.proposed_state) == (
        "draft",
        "published",
    )
    assert _workflow_authorized(request)
    assert current_transition() is None


@pytest.mark.django_db
@test_context
def test_refused_transition():
    store_note_policy()
    request = put_request(user_with((may_transition("retract"), "a")))
    with pytest.raises(WorkflowTransitionError) as excinfo:
        publish(request)
    # Not hidden: the requester reads the policy, and the stored note is
    # treated as readable (no `readable`).
    assert type(excinfo.value) is WorkflowTransitionError
    assert excinfo.value.status_code == 400
    assert (excinfo.value.prior_state, excinfo.value.proposed_state) == (
        "draft",
        "published",
    )
    assert not _workflow_authorized(request)


@pytest.mark.django_db
@test_context
def test_hidden_when_unreadable():
    store_note_policy()
    request = put_request(user_with((may_transition("retract"), "a")))
    with pytest.raises(HiddenWorkflowTransitionError) as excinfo:
        publish(request, readable=lambda: False)
    assert excinfo.value.status_code == 400
    assert (excinfo.value.prior_state, excinfo.value.proposed_state) == (None, None)
    assert not _workflow_authorized(request)


@pytest.mark.django_db
@test_context
def test_context_without_policy():
    from scrud_django.core.workflow import workflow_check

    store_note_policy()
    request = put_request(user_with((may_transition("publish"), "b")))
    with pytest.raises(access.AccessDenied):
        workflow_check(
            request,
            type_iri=NOTE,
            prior=dict(DRAFT, rbac_context="b"),
            proposed=dict(PUBLISHED, rbac_context="b"),
        )
    assert not _workflow_authorized(request)


@pytest.mark.django_db
@test_context
def test_narrow_refuses():
    store_note_policy()
    request = put_request(user_with((may_transition("publish"), "a")))
    asked = []

    def refuse(request, body, context):
        asked.append((body, context, current_transition()))
        return False

    with pytest.raises(WorkflowTransitionError):
        publish(request, narrow=[refuse])
    assert asked == [(PUBLISHED, "a", "publish")]
    assert not _workflow_authorized(request)


@pytest.mark.django_db
@test_context
def test_a_delete_is_not_checked_yet():
    from scrud_django.core.workflow import workflow_check

    request = put_request(UserFactory())
    with pytest.raises(NotImplementedError, match="delete-no-workflow-check"):
        workflow_check(request, type_iri=NOTE, prior=DRAFT, proposed=None)


@pytest.mark.django_db
@test_context
def test_authorizing_write_lets_scrud_workflow_allow_a_covered_write():
    from scrud_django.core.authz import _transition
    from scrud_django.core.workflow import authorizing_write
    from scrud_django.permissions import _OUTSIDE, _WRITE, _mark_workflow_authorized

    request = put_request(UserFactory())
    with authorizing_write(request, covered=True):
        assert _transition.get() is _WRITE
    with authorizing_write(request, covered=False):
        assert _transition.get() is _OUTSIDE
    # The post-save check: only once the workflow check authorized the write.
    with authorizing_write(request, covered=True, authorized=True):
        assert _transition.get() is _OUTSIDE
    _mark_workflow_authorized(request)
    with authorizing_write(request, covered=True, authorized=True):
        assert _transition.get() is _WRITE
    assert _transition.get() is _OUTSIDE
