"""The workflow check without DRF (`scrud_django.core.workflow`): a write's
transition under the policy of every context it concerns, asked with a plain
Django request, a type IRI and plain JSON mappings (no ``ResourceType`` row,
no stored ``Resource``), as an app's own models will ask it."""

import json
from uuid import uuid4

import pytest
from django.test import RequestFactory
from django.urls import reverse
from django.utils.timezone import now
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.core.workflow import NoWorkflowPolicyDenied
from scrud_django.exceptions import (
    HiddenWorkflowTransitionError,
    WorkflowTransitionError,
)
from scrud_django.permissions import (
    _WORKFLOW_AUTHORIZED,
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


# The pk of the stored note `publish` updates (no row: the check reads only
# the content it is given).
NOTE_PK = 1


def publish(request, **kwargs):
    from scrud_django.core.workflow import workflow_check

    return workflow_check(
        request,
        type_iri=NOTE,
        prior=DRAFT,
        proposed=PUBLISHED,
        stored_pk=NOTE_PK,
        **kwargs,
    )


def published(request):
    """Whether `request` carries the mark of `publish`'s write."""
    return _workflow_authorized(request, type_iri=NOTE, stored_pk=NOTE_PK)


def marked(request):
    """Whether `request` carries a workflow mark, of any write."""
    return hasattr(request, _WORKFLOW_AUTHORIZED)


@pytest.mark.django_db
@test_context
def test_allowed_transition_marks_request():
    store_note_policy()
    request = put_request(user_with((may_transition("publish"), "a")))
    assert not marked(request)
    ((context, policy, transition),) = publish(request)
    assert context == "a"
    assert policy.policy["transitions"] == NOTE_POLICY["transitions"]
    assert transition.transition == "publish"
    assert (transition.prior_state, transition.proposed_state) == (
        "draft",
        "published",
    )
    assert published(request)
    assert current_transition() is None


@pytest.mark.django_db
@test_context
def test_the_mark_names_one_write():
    from types import SimpleNamespace

    from scrud_django.core.authz import _consume_workflow_mark
    from scrud_django.core.workflow import workflow_check

    store_note_policy()
    user = user_with((may_transition("publish", "__INITIALIZE__"), "a"))
    # The type the post-save check names, not the policy's type.
    request = put_request(user)
    publish(request, readable=lambda: True, post_save_type_iri="tests://Named")
    assert _workflow_authorized(request, type_iri="tests://Named", stored_pk=NOTE_PK)
    assert not published(request)
    # An update that does not say which resource it writes is not marked.
    request = put_request(user)
    workflow_check(
        request, type_iri=NOTE, prior=DRAFT, proposed=PUBLISHED, readable=lambda: True
    )
    assert not marked(request)
    # A create's mark names no stored resource.
    request = put_request(user)
    workflow_check(request, type_iri=NOTE, prior=None, proposed=DRAFT)
    assert _workflow_authorized(request, type_iri=NOTE, stored_pk=None)
    assert not published(request)
    # Consumed, from a wrapper and the request it wraps alike.
    _consume_workflow_mark(request)
    assert not marked(request)
    publish(request, readable=lambda: True)
    wrapper = SimpleNamespace(_request=request)
    _consume_workflow_mark(wrapper)
    assert not marked(request)
    _consume_workflow_mark(wrapper)  # Nothing left to remove.


@pytest.mark.django_db
@test_context
def test_refused_transition():
    store_note_policy()
    request = put_request(user_with((may_transition("retract"), "a")))
    with pytest.raises(WorkflowTransitionError) as excinfo:
        publish(request, readable=lambda: True)
    # Not hidden: the requester reads the policy and the stored note.
    assert type(excinfo.value) is WorkflowTransitionError
    assert excinfo.value.status_code == 400
    assert (excinfo.value.prior_state, excinfo.value.proposed_state) == (
        "draft",
        "published",
    )
    assert not marked(request)


@pytest.mark.django_db
@test_context
def test_hidden_when_unreadable():
    store_note_policy()
    request = put_request(user_with((may_transition("retract"), "a")))
    with pytest.raises(HiddenWorkflowTransitionError) as excinfo:
        publish(request, readable=lambda: False)
    assert excinfo.value.status_code == 400
    assert (excinfo.value.prior_state, excinfo.value.proposed_state) == (None, None)
    assert not marked(request)


@pytest.mark.django_db
@test_context
def test_context_without_policy():
    from scrud_django.core.workflow import workflow_check

    store_note_policy()
    request = put_request(user_with((may_transition("publish"), "b")))
    with pytest.raises(access.AccessDenied) as excinfo:
        workflow_check(
            request,
            type_iri=NOTE,
            prior=dict(DRAFT, rbac_context="b"),
            proposed=dict(PUBLISHED, rbac_context="b"),
        )
    # The type the DRF adapter maps to its 403 (drf_error).
    assert type(excinfo.value) is NoWorkflowPolicyDenied
    assert not marked(request)


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
    assert not marked(request)


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
    _mark_workflow_authorized(request, type_iri=NOTE, stored_pk=NOTE_PK)
    with authorizing_write(
        request, covered=True, authorized=True, type_iri=NOTE, stored_pk=NOTE_PK
    ):
        assert _transition.get() is _WRITE
    assert _transition.get() is _OUTSIDE
    # Only for the write the mark names.
    for type_iri, stored_pk in ((NOTE, 2), (NOTE, None), ("tests://Other", NOTE_PK)):
        with authorizing_write(
            request,
            covered=True,
            authorized=True,
            type_iri=type_iri,
            stored_pk=stored_pk,
        ):
            assert _transition.get() is _OUTSIDE
    with authorizing_write(request, covered=True, authorized=True):
        assert _transition.get() is _OUTSIDE


# The DRF adapter: a stored resource whose content is JSON null is an update
# over content in no state and in the default context (as content that is
# not an object), never a create.

JSON_LD = "http://www.w3.org/ns/json-ld#context"


def may_transition_json_ld(*transitions):
    """A role reading JSON-LD resources, and writing one only by one of
    `transitions`."""
    return {
        "http.GET": [JSON_LD],
        "http.PUT": {
            JSON_LD: {
                "condition": {
                    "operator": "scrud_workflow",
                    "authorized_transitions": list(transitions),
                }
            }
        },
    }


@pytest.mark.django_db
@pytest.mark.parametrize("stored_content", [{}, None], ids=["empty", "null"])
@test_context
def test_null_stored_content_is_checked_as_an_update(stored_content):
    from django.db.models import JSONField, Value
    from django.utils.http import quote_etag
    from rest_framework import status

    from scrud_django.models import Resource

    reverse("json-ld-list")
    workflow_policy_resource_type()
    ResourceRegistration.register(
        {
            "resource_type": JSON_LD,
            "rbac_context": "a",
            "initial_states": {"any": "true"},
            "states": {},
            "transitions": {},
        },
        "workflow-policies",
    )
    stored = ResourceRegistration.register({"x": 1}, "json-ld")
    Resource.objects.filter(pk=stored.pk).update(
        content=Value(stored_content, JSONField())
    )
    stored.refresh_from_db()
    assert stored.content == stored_content
    # Only scrud_workflow-conditioned grants in "" (where no policy applies)
    # and in "a": moving the resource out of "" is refused there.
    Given.http.force_authenticate(
        user_with(
            (may_transition_json_ld("__INITIALIZE__"), ""),
            (may_transition_json_ld("__INITIALIZE__"), "a"),
        )
    )
    When.http.put(
        reverse("json-ld-detail", args=[stored.pk]),
        data=json.dumps({"rbac_context": "a"}),
        content_type="application/json",
        HTTP_IF_MATCH=quote_etag(stored.etag),
    )
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert Http.response.json()["code"] == "permission_denied"
    assert Resource.objects.get(pk=stored.pk).content == stored_content


@pytest.mark.django_db
@test_context
def test_an_update_without_readable_hides_its_errors():
    # A caller that does not say whether the stored resource is readable
    # (`readable=None`) gets an update's workflow error hidden: it fails
    # closed. A create (no prior) has no stored state to hide.
    from scrud_django.core.workflow import workflow_check

    store_note_policy()
    request = put_request(user_with((may_transition("retract"), "a")))
    with pytest.raises(HiddenWorkflowTransitionError) as excinfo:
        publish(request)
    assert (excinfo.value.prior_state, excinfo.value.proposed_state) == (None, None)
    assert not marked(request)

    creator = put_request(user_with((may_transition("retract"), "a")))
    with pytest.raises(WorkflowTransitionError) as excinfo:
        workflow_check(creator, type_iri=NOTE, prior=None, proposed=DRAFT)
    assert type(excinfo.value) is WorkflowTransitionError
    assert excinfo.value.proposed_state == "draft"
