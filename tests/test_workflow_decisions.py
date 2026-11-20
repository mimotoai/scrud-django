"""The workflow check's decision is the one ``scrud_post_save`` sends.

A write the workflow check authorized saves with its `WorkflowDecision`
pending (`core.decisions.applying`): ``Resource.save()`` takes it when it
matches the save (the type, the stored pk and the content checked), and
sends it as ``decision`` instead of computing the transitions again. A save
it does not match, a second save in the same block, and a save no check
ran for compute their own, unchecked (``checked=False``).
"""

import dataclasses
import json
from http import HTTPStatus
from uuid import uuid4

import pytest
from django.utils.http import quote_etag
from django.utils.timezone import now
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.core.decisions import applying, take_pending
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .fixtures import *  # noqa: F401, F403
from .front_ends import api_url, serve_ninja
from .steps import *  # noqa: F403, F401

TODO = "tests://ToDo"
ALL_METHODS = ("http.GET", "http.POST", "http.PUT", "http.DELETE")
TODO_WRITER = {method: [TODO] for method in ALL_METHODS}

NOT_STARTED = {
    "title": "Buy milk",
    "status": "not started",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
}
IN_PROGRESS = dict(NOT_STARTED, status="in progress")


def writer():
    """A user who may read and write ToDos in the default context."""
    user = UserFactory()
    role = Role(definition=TODO_WRITER, etag=uuid4().hex, modified_at=now())
    role.save()
    RoleAssignment(
        user=user, role=role, rbac_context="", etag=uuid4().hex, modified_at=now()
    ).save()
    return user


def todo(**content):
    serve_ninja()  # registers the test types
    return ResourceRegistration.register(dict(NOT_STARTED, **content), "todos")


def put_todo(front_end, resource, content):
    When.http.put(
        api_url(front_end, "todos-detail", args=[resource.id]),
        data=json.dumps(content),
        content_type="application/json",
        HTTP_IF_MATCH=quote_etag(resource.etag),
    )


def post_todo(front_end, content):
    When.http.post(
        api_url(front_end, "todos-list"),
        data=json.dumps(content),
        content_type="application/json",
    )


@pytest.fixture
def reported_saves():
    """The keyword arguments of each ToDo ``scrud_post_save``, in order."""
    from scrud_django.core.signals import scrud_post_save

    reported = []

    def record(sender, resource_type_uri=None, **kwargs):
        if resource_type_uri == TODO:
            reported.append(kwargs)

    scrud_post_save.connect(record, weak=False, dispatch_uid="test-decision-saves")
    try:
        yield reported
    finally:
        scrud_post_save.disconnect(dispatch_uid="test-decision-saves")


def assert_sent_from(reported):
    """The signal's transition kwargs are the decision's."""
    decision = reported["decision"]
    assert reported["transitions"] == decision.transitions
    assert reported["transition_data"] == decision.transition_data
    assert reported["transition_error"] == decision.transition_error


@pytest.mark.django_db
@test_context
def test_an_api_write_sends_the_checked_decision(front_end, reported_saves):
    user = writer()
    resource = todo()
    Given.http.force_login(user)
    reported_saves.clear()
    put_todo(front_end, resource, IN_PROGRESS)
    Then.http.status_code_is(HTTPStatus.OK)
    (reported,) = reported_saves
    decision = reported["decision"]
    assert decision.checked is True
    assert decision.actor == user
    assert decision.stored_pk == resource.pk
    assert decision.type_iri == TODO
    assert decision.proposed == IN_PROGRESS
    assert reported["transitions"] == decision.transitions
    assert_sent_from(reported)
    assert decision.transition_data.transition == "start"


@pytest.mark.django_db
@test_context
def test_an_api_create_sends_the_checked_decision(front_end, reported_saves):
    user = writer()
    serve_ninja()  # registers the test types
    Given.http.force_login(user)
    reported_saves.clear()
    post_todo(front_end, NOT_STARTED)
    Then.http.status_code_is(HTTPStatus.CREATED)
    (reported,) = reported_saves
    decision = reported["decision"]
    assert decision.checked is True
    assert decision.actor == user
    assert decision.stored_pk is None
    assert decision.prior is None
    assert decision.proposed == NOT_STARTED
    assert_sent_from(reported)
    assert decision.transition_data.transition == "__INITIALIZE__"


@pytest.mark.django_db
@test_context
def test_a_checked_save_does_not_recompute(front_end, monkeypatch):
    user = writer()
    resource = todo()
    Given.http.force_login(user)

    def recomputed(*args, **kwargs):
        raise AssertionError("a checked save recomputed its transitions")

    monkeypatch.setattr(
        "scrud_django.mixins.resource_mixin.transitions_between", recomputed
    )
    put_todo(front_end, resource, IN_PROGRESS)
    Then.http.status_code_is(HTTPStatus.OK)
    assert Resource.objects.get(pk=resource.pk).content == IN_PROGRESS


@pytest.mark.django_db
@test_context
def test_a_server_side_save_computes_an_unchecked_decision(reported_saves):
    resource = todo()
    reported_saves.clear()
    ResourceRegistration.update(IN_PROGRESS, "todos", resource.pk)
    (reported,) = reported_saves
    decision = reported["decision"]
    assert decision.checked is False
    assert decision.actor is None
    assert decision.stored_pk == resource.pk
    assert_sent_from(reported)
    assert decision.transition_data.transition == "start"


def checked_decision_of(resource):
    """A checked decision of saving `resource`'s current content, as the
    workflow check would build it."""
    return dataclasses.replace(resource.get_transitions(), checked=True)


@pytest.mark.django_db
@test_context
def test_a_second_save_in_the_block_computes_its_own(reported_saves):
    resource = todo()
    resource.content = dict(IN_PROGRESS)
    decision = checked_decision_of(resource)
    reported_saves.clear()
    with applying(decision):
        resource.save()
        resource.save()
    first, second = reported_saves
    assert first["decision"] is decision
    assert second["decision"] is not decision
    assert second["decision"].checked is False
    assert_sent_from(first)
    assert_sent_from(second)


@pytest.mark.django_db
@test_context
def test_a_save_of_other_content_does_not_take_the_decision(reported_saves):
    resource = todo()
    resource.content = dict(IN_PROGRESS)
    decision = checked_decision_of(resource)
    resource.content = dict(IN_PROGRESS, priority="high")
    assert decision.proposed != resource.content
    reported_saves.clear()
    with applying(decision):
        resource.save()
    (reported,) = reported_saves
    assert reported["decision"] is not decision
    assert reported["decision"].checked is False


@pytest.mark.django_db
@test_context
def test_a_save_of_another_resource_does_not_take_the_decision(reported_saves):
    resource = todo()
    other = todo()
    resource.content = dict(IN_PROGRESS)
    other.content = dict(IN_PROGRESS)
    decision = checked_decision_of(resource)
    reported_saves.clear()
    with applying(decision):
        other.save()
        resource.save()
    unmatched, matched = reported_saves
    assert unmatched["decision"].checked is False
    assert matched["decision"] is decision


@pytest.mark.django_db
@test_context
def test_a_pending_decision_ends_with_its_block():
    resource = todo()
    resource.content = dict(IN_PROGRESS)
    decision = checked_decision_of(resource)
    match = dict(type_iri=TODO, pk=resource.pk, content=resource.content)
    with pytest.raises(RuntimeError):
        with applying(decision):
            raise RuntimeError("the save failed")
    assert take_pending(**match) is None
    with applying(decision):
        assert take_pending(**match) is decision
        assert take_pending(**match) is None
    with applying(None):
        assert take_pending(**match) is None
