"""The opt-in transactional outbox, ``scrud_django.outbox`` (#20): one
`OutboxEvent` per write of a type listed in ``SCRUD_OUTBOX["TYPES"]``,
written in the write's transaction, and `send_events` /
``manage.py scrud_outbox_send`` to deliver them."""

import json
import logging
from http import HTTPStatus
from io import StringIO
from uuid import uuid4

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core import checks
from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import override_settings
from django.utils.http import quote_etag
from django.utils.timezone import now
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.core.signals import acting_as, scrud_post_save
from scrud_django.models import Resource
from scrud_django.outbox import send_events
from scrud_django.outbox.checks import check_outbox_types
from scrud_django.outbox.conf import recorded_type
from scrud_django.outbox.models import OutboxEvent
from scrud_django.outbox.receivers import store_event
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .fixtures import *  # noqa: F401, F403
from .fixtures import assign_role
from .front_ends import api_url, serve_ninja
from .steps import *  # noqa: F403, F401
from .workflow_defaults import ARTICLE

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
ARTICLE_WRITER = {
    "http.GET": [ARTICLE, f"{ARTICLE}/list"],
    "http.POST": [ARTICLE],
    "http.PUT": [ARTICLE],
    "http.DELETE": [ARTICLE],
}

RECORD_TODOS = override_settings(SCRUD_OUTBOX={"TYPES": [TODO]})

#: What `record_delivery` delivered, in order (the command's ``DELIVER``).
delivered = []


def record_delivery(event):
    delivered.append(event.pk)


@pytest.fixture(autouse=True)
def no_deliveries():
    delivered.clear()
    yield
    delivered.clear()


def writer(*contexts):
    user = UserFactory()
    role = Role(definition=TODO_WRITER, etag=uuid4().hex, modified_at=now())
    role.save()
    for context in contexts:
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=context,
            etag=uuid4().hex,
            modified_at=now(),
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


def event(**fields):
    """A stored `OutboxEvent` of a ToDo update, with `fields`."""
    values = dict(type_uri=TODO, resource_pk="1", action="UPDATE", checked=True)
    values.update(fields)
    return OutboxEvent.objects.create(**values)


@pytest.mark.django_db
@test_context
def test_an_api_put_records_one_event(front_end):
    user = writer("a")
    resource = todo(rbac_context="a")
    stored = dict(NOT_STARTED, rbac_context="a")
    Given.http.force_login(user)
    with RECORD_TODOS:
        put_todo(front_end, resource, dict(IN_PROGRESS, rbac_context="a"))
    Then.http.status_code_is(HTTPStatus.OK)
    (recorded,) = OutboxEvent.objects.all()
    assert recorded.type_uri == TODO
    assert recorded.resource_pk == str(resource.pk)
    assert recorded.action == "UPDATE"
    assert recorded.checked is True
    assert recorded.transitions == [
        {
            "context": "a",
            "transition": "start",
            "prior_state": "not started",
            "proposed_state": "in progress",
            "leaving": False,
            "error": None,
        }
    ]
    assert recorded.user_id == str(user.pk)
    assert recorded.prior_content == stored
    assert recorded.content == dict(IN_PROGRESS, rbac_context="a")
    assert recorded.created_at is not None
    assert recorded.sent_at is None


@pytest.mark.django_db
def test_a_rolled_back_write_records_none():
    resource = todo()
    with RECORD_TODOS, pytest.raises(RuntimeError):
        with transaction.atomic():
            ResourceRegistration.update(dict(IN_PROGRESS), "todos", str(resource.pk))
            assert OutboxEvent.objects.count() == 1  # written in the transaction
            raise RuntimeError("roll back")
    assert not OutboxEvent.objects.exists()
    assert Resource.objects.get(pk=resource.pk).content == NOT_STARTED


@pytest.mark.django_db
def test_an_unlisted_type_records_none():
    resource = todo()
    with override_settings(SCRUD_OUTBOX={"TYPES": ["tests://Other"]}):
        ResourceRegistration.update(dict(IN_PROGRESS), "todos", str(resource.pk))
    assert not OutboxEvent.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("outbox", [{}, {"TYPES": []}, {"TYPES": ()}, None])
def test_no_types_record_none(outbox):
    resource = todo()
    setting = {} if outbox is None else {"SCRUD_OUTBOX": outbox}
    with override_settings(**setting):
        ResourceRegistration.update(dict(IN_PROGRESS), "todos", str(resource.pk))
    assert not OutboxEvent.objects.exists()


@pytest.mark.django_db
def test_with_no_types_the_receiver_makes_no_query(django_assert_num_queries):
    resource = todo()
    with django_assert_num_queries(0):
        store_event(
            sender=Resource,
            instance=resource,
            using="default",
            resource_type_uri=TODO,
            action="UPDATE",
        )


@pytest.mark.django_db
def test_all_types_are_recorded_with_all():
    resource = todo()
    with override_settings(SCRUD_OUTBOX={"TYPES": "__all__"}):
        ResourceRegistration.update(dict(IN_PROGRESS), "todos", str(resource.pk))
    assert [e.type_uri for e in OutboxEvent.objects.all()] == [TODO]


def test_recorded_type():
    with override_settings(SCRUD_OUTBOX={"TYPES": [TODO]}):
        assert recorded_type(TODO)
        assert not recorded_type("tests://Other")
    with override_settings(SCRUD_OUTBOX={"TYPES": "__all__"}):
        assert recorded_type("tests://Other")
    # A malformed TYPES (E013) records nothing; a string is not a list of
    # one (no substring match).
    with override_settings(SCRUD_OUTBOX={"TYPES": TODO}):
        assert not recorded_type(TODO)
        assert not recorded_type("tests://To")
    with override_settings(SCRUD_OUTBOX=[TODO]):
        assert not recorded_type(TODO)


@pytest.mark.django_db
@test_context
def test_a_delete_records_its_action_and_no_check(front_end):
    user = writer("a")
    resource = todo(rbac_context="a")
    Given.http.force_login(user)
    with RECORD_TODOS:
        When.http.delete(
            api_url(front_end, "todos-detail", args=[resource.id]),
            HTTP_IF_MATCH=quote_etag(resource.etag),
        )
    Then.http.status_code_is(HTTPStatus.NO_CONTENT)
    (recorded,) = OutboxEvent.objects.all()
    assert recorded.action == "DELETE"
    assert recorded.checked is None
    assert recorded.transitions == []
    assert recorded.resource_pk == str(resource.pk)
    assert recorded.user_id == str(user.pk)
    assert recorded.content == dict(NOT_STARTED, rbac_context="a")
    assert recorded.prior_content is None


@pytest.mark.django_db
def test_a_create_records_no_prior_content():
    serve_ninja()
    user = UserFactory()
    with RECORD_TODOS, acting_as(user):
        resource = ResourceRegistration.register(dict(NOT_STARTED), "todos")
    (recorded,) = OutboxEvent.objects.all()
    assert recorded.action == "CREATE"
    assert recorded.resource_pk == str(resource.pk)
    assert recorded.checked is False
    assert recorded.prior_content is None
    assert recorded.content == NOT_STARTED
    assert recorded.user_id == str(user.pk)
    assert [entry["transition"] for entry in recorded.transitions] == ["__INITIALIZE__"]


@pytest.mark.django_db
def test_a_save_without_a_decision_records_the_instance():
    resource = todo()
    with RECORD_TODOS:
        scrud_post_save.send(
            sender=Resource,
            instance=resource,
            using="default",
            resource_type_uri=TODO,
            action="UPDATE",
        )
    (recorded,) = OutboxEvent.objects.all()
    assert recorded.checked is None
    assert recorded.transitions == []
    assert recorded.content == NOT_STARTED
    assert recorded.user_id is None
    assert recorded.prior_content is None


@pytest.mark.django_db
def test_contexts_are_recorded_in_the_decisions_order():
    resource = todo(rbac_context=["b", "a"])
    with RECORD_TODOS, acting_as(UserFactory()):
        ResourceRegistration.update(
            dict(IN_PROGRESS, rbac_context=["b", "a"]), "todos", str(resource.pk)
        )
    (recorded,) = OutboxEvent.objects.all()
    assert [entry["context"] for entry in recorded.transitions] == ["b", "a"]
    assert [entry["transition"] for entry in recorded.transitions] == [
        "start",
        "start",
    ]


@pytest.mark.django_db
def test_a_leaving_context_and_an_error_are_recorded():
    # Leaving "a": the transition there, flagged. An unchecked save into an
    # invalid state: its error's message, and no transition.
    resource = todo(rbac_context=["a", "b"])
    with RECORD_TODOS, acting_as(UserFactory()):
        ResourceRegistration.update(
            dict(IN_PROGRESS, rbac_context=["b"]), "todos", str(resource.pk)
        )
        ResourceRegistration.update(
            dict(NOT_STARTED, status="nonsense", rbac_context=["b"]),
            "todos",
            str(resource.pk),
        )
    leaving, invalid = OutboxEvent.objects.order_by("pk")
    assert leaving.transitions == [
        {
            "context": "a",
            "transition": "start",
            "prior_state": "not started",
            "proposed_state": "in progress",
            "leaving": True,
            "error": None,
        },
        {
            "context": "b",
            "transition": "start",
            "prior_state": "not started",
            "proposed_state": "in progress",
            "leaving": False,
            "error": None,
        },
    ]
    (entry,) = invalid.transitions
    assert entry["context"] == "b"
    assert entry["transition"] is None
    assert entry["prior_state"] is None
    assert entry["proposed_state"] is None
    assert entry["leaving"] is False
    assert isinstance(entry["error"], str) and entry["error"]


@pytest.mark.django_db(databases=["default", "other"])
def test_an_event_is_written_on_the_writes_database():
    resource = todo()
    with RECORD_TODOS:
        store_event(
            sender=Resource,
            instance=resource,
            using="other",
            resource_type_uri=TODO,
            action="UPDATE",
        )
    assert OutboxEvent.objects.using("other").count() == 1
    assert not OutboxEvent.objects.using("default").exists()


@pytest.mark.django_db
@pytest.mark.urls("tests.own_model_urls")
def test_an_own_model_write_records_its_iri_and_pk(client):
    from tests.own_models import ARTICLE_WORKFLOW  # noqa: F401 (governs Article)

    user = assign_role(ARTICLE_WRITER, "a")
    client.force_login(user)
    with override_settings(SCRUD_OUTBOX={"TYPES": [ARTICLE]}):
        response = client.post(
            "/api/articles/",
            {"title": "t", "state": "draft", "rbac_context": "a"},
            content_type="application/json",
        )
        assert response.status_code == 201, response.content
        pk = response.json()["id"]
        response = client.put(
            f"/api/articles/{pk}/",
            {"state": "published"},
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        response = client.delete(f"/api/articles/{pk}/")
        assert response.status_code == 204, response.content
    created, updated, deleted = OutboxEvent.objects.order_by("pk")
    for recorded in (created, updated, deleted):
        assert recorded.type_uri == ARTICLE
        assert recorded.resource_pk == str(pk)
        assert recorded.user_id == str(user.pk)
    assert created.action == "CREATE"
    assert created.checked is True
    assert created.prior_content is None
    assert created.content["state"] == "draft"
    assert updated.action == "UPDATE"
    assert updated.checked is True
    assert updated.prior_content == {
        "id": pk,
        "title": "t",
        "state": "draft",
        "rbac_context": "a",
    }
    assert updated.content == dict(updated.prior_content, state="published")
    assert updated.transitions == [
        {
            "context": "a",
            "transition": "publish",
            "prior_state": "draft",
            "proposed_state": "published",
            "leaving": False,
            "error": None,
        }
    ]
    assert deleted.action == "DELETE"
    assert deleted.checked is None
    assert deleted.content is None


@pytest.mark.django_db
def test_send_events_delivers_in_pk_order_and_marks_them_sent():
    first, second, third = event(), event(), event()
    sent = event(sent_at=now())
    seen = []
    assert send_events(lambda e: seen.append(e.pk), batch=2) == 2
    assert seen == [first.pk, second.pk]
    assert send_events(lambda e: seen.append(e.pk)) == 1
    assert seen == [first.pk, second.pk, third.pk]
    assert send_events(lambda e: seen.append(e.pk)) == 0
    for stored in OutboxEvent.objects.filter(pk__in=[first.pk, second.pk, third.pk]):
        assert stored.sent_at is not None
    assert sent.pk not in seen  # already sent


@pytest.mark.django_db
def test_a_raising_deliver_leaves_its_event_and_the_rest_unsent():
    first, second, third = event(), event(), event()
    seen = []

    def deliver(e):
        seen.append(e.pk)
        if e.pk == second.pk:
            raise RuntimeError("down")

    with pytest.raises(RuntimeError, match="down"):
        send_events(deliver)
    assert seen == [first.pk, second.pk]
    # The batch's transaction rolled back: none is marked sent, the one
    # delivered before included (at-least-once: it is delivered again).
    assert list(
        OutboxEvent.objects.filter(sent_at=None)
        .order_by("pk")
        .values_list("pk", flat=True)
    ) == [first.pk, second.pk, third.pk]


@pytest.mark.django_db
def test_the_command_fails_without_deliver():
    event()
    with override_settings(SCRUD_OUTBOX={"TYPES": [TODO]}):
        with pytest.raises(CommandError, match="DELIVER"):
            call_command("scrud_outbox_send")
    assert OutboxEvent.objects.filter(sent_at=None).count() == 1


@pytest.mark.django_db
def test_the_command_sends_with_deliver():
    first, second = event(), event()
    out = StringIO()
    with override_settings(
        SCRUD_OUTBOX={"TYPES": [TODO], "DELIVER": f"{__name__}.record_delivery"}
    ):
        call_command("scrud_outbox_send", "--batch", "1", stdout=out)
        assert delivered == [first.pk]
        assert "Sent 1" in out.getvalue()
        call_command("scrud_outbox_send", stdout=out)
    assert delivered == [first.pk, second.pk]
    assert not OutboxEvent.objects.filter(sent_at=None).exists()


@pytest.mark.parametrize(
    "outbox", [None, {}, {"TYPES": None}, {"TYPES": []}, {"TYPES": ()}]
)
def test_w010_warns_of_no_types(outbox):
    setting = {} if outbox is None else {"SCRUD_OUTBOX": outbox}
    with override_settings(**setting):
        (found,) = check_outbox_types(None)
    assert found.id == "scrud_django.W010"


@pytest.mark.parametrize(
    "outbox",
    [
        {"TYPES": TODO},
        {"TYPES": ""},
        {"TYPES": [TODO, 1]},
        {"TYPES": {TODO}},
        [TODO],
    ],
)
def test_e013_reports_a_malformed_types(outbox):
    with override_settings(SCRUD_OUTBOX=outbox):
        (found,) = check_outbox_types(None)
    assert found.id == "scrud_django.E013"


@pytest.mark.parametrize("types", ["__all__", [TODO], (TODO, ARTICLE)])
def test_well_formed_types_pass_the_check(types):
    with override_settings(SCRUD_OUTBOX={"TYPES": types}):
        assert check_outbox_types(None) == []


@pytest.mark.django_db
def test_an_event_without_a_pk_is_not_stored(caplog):
    with (
        RECORD_TODOS,
        caplog.at_level(logging.WARNING, logger="scrud_django.workflow_actions"),
    ):
        store_event(
            sender=Resource,
            instance=Resource(content=NOT_STARTED),  # unsaved: no pk
            using="default",
            resource_type_uri=TODO,
            action="UPDATE",
        )
    assert not OutboxEvent.objects.exists()
    (record,) = [r for r in caplog.records if r.name == "scrud_django.workflow_actions"]
    assert record.levelno == logging.WARNING
    assert "no primary key" in record.getMessage()
    assert TODO in record.getMessage()


@pytest.mark.django_db
def test_user_id_is_an_authenticated_users_pk_or_none():
    resource = todo()
    saved = UserFactory()
    users = [None, AnonymousUser(), UserFactory.build(), saved]
    with RECORD_TODOS:
        for user in users:
            store_event(
                sender=Resource,
                instance=resource,
                using="default",
                resource_type_uri=TODO,
                action="UPDATE",
                user=user,
            )
    recorded = list(
        OutboxEvent.objects.order_by("pk").values_list("user_id", flat=True)
    )
    assert recorded == [None, None, None, str(saved.pk)]
    assert "None" not in recorded


@pytest.mark.django_db
def test_the_outbox_check_is_registered():
    # run_checks() imports the URLconf, which registers resource types.
    with override_settings(SCRUD_OUTBOX={"TYPES": TODO}):
        ran = checks.run_checks()
    assert "scrud_django.E013" in {message.id for message in ran}, ran
