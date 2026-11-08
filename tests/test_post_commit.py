"""Side effects outside the database run when the write commits: the
configured ``SCRUD_WORKFLOW_ACTIONS`` (`ScrudSignalProcessor`) and the search
index (`scrud_django.search.OnCommitSignalProcessor`, the test settings'
``HAYSTACK_SIGNAL_PROCESSOR``). A write rolled back (a post-save 403, any
error) runs none of them. ``scrud_post_save`` itself still fires inside the
write, before the post-save check."""

import logging
import threading
from contextlib import contextmanager, nullcontext
from uuid import uuid4

import pytest
from django.apps import apps
from django.db import connection, connections, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.scrud_signals import scrud_post_delete, scrud_post_save, write_alias
from scrud_django.search_indexes import ResourceIndex

from .factories import UserFactory
from .workflow_actions import actions

TODO = "tests://ToDo"
NOT_STARTED = {"title": "t", "status": "not started"}
IN_PROGRESS = {
    "title": "t",
    "status": "in progress",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
}


@pytest.fixture
def ran(monkeypatch):
    """The ToDo actions that ran: ``("save", pk)`` and ``("delete", etag,
    pk)``. A deleted instance has no pk: the ``pk`` the action is given
    names the deleted row."""
    calls = []
    entry = actions[TODO]

    def on_save(sender, instance, **kwargs):
        calls.append(("save", instance.pk))

    def on_delete(sender, instance, pk=None, **kwargs):
        calls.append(("delete", instance.etag, pk))

    monkeypatch.setitem(entry, "on_save", on_save)
    monkeypatch.setitem(entry, "on_delete", on_delete)
    return calls


class ActionFailed(Exception):
    pass


@pytest.fixture
def failing_on_save(monkeypatch):
    """A ToDo ``on_save`` action that raises; returns the pks it was called
    with."""
    calls = []

    def on_save(sender, instance, **kwargs):
        calls.append(instance.pk)
        raise ActionFailed(f"on_save failed for {instance.pk}")

    monkeypatch.setitem(actions[TODO], "on_save", on_save)
    return calls


@pytest.fixture
def failing_on_delete(monkeypatch):
    """A ToDo ``on_delete`` action that raises; returns the pks it was given."""
    calls = []

    def on_delete(sender, instance, pk=None, **kwargs):
        calls.append(pk)
        raise ActionFailed(f"on_delete failed for {pk}")

    monkeypatch.setitem(actions[TODO], "on_delete", on_delete)
    return calls


def logged_action_failures(caplog):
    return [
        record
        for record in caplog.records
        if record.levelno == logging.ERROR
        and record.exc_info
        and isinstance(record.exc_info[1], ActionFailed)
    ]


@pytest.fixture
def indexed(monkeypatch):
    """The search index updates: ``("update", pk)`` and ``("remove", id)``."""
    calls = []

    def update_object(self, instance, using=None, **kwargs):
        if isinstance(instance, Resource):
            calls.append(("update", instance.pk))

    def remove_object(self, instance, using=None, **kwargs):
        calls.append(("remove", instance))

    monkeypatch.setattr(ResourceIndex, "update_object", update_object)
    monkeypatch.setattr(ResourceIndex, "remove_object", remove_object)
    return calls


def admin_client():
    client = APIClient()
    client.force_authenticate(UserFactory(is_staff=True, is_superuser=True))
    return client


def default_context_todo_writer():
    user = UserFactory()
    definition = {
        m: [TODO] for m in ("http.GET", "http.POST", "http.PUT", "http.DELETE")
    }
    role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
    role.save()
    RoleAssignment(
        user=user,
        role=role,
        rbac_context=DEFAULT_CONTEXT,
        etag=uuid4().hex,
        modified_at=now(),
    ).save()
    client = APIClient()
    client.force_authenticate(user)
    return client


@contextmanager
def todos_moved_after_save(context="other"):
    """A ``scrud_post_save`` receiver that moves every saved ToDo to
    `context`: by default "other", where the writer may not write, so that
    the post-save check refuses the write with 403 and rolls it back (a
    reserved context is a 400, whoever writes)."""

    def move(sender, instance, resource_type_uri=None, **kwargs):
        if resource_type_uri == TODO:
            content = {**instance.content, "rbac_context": context}
            type(instance).objects.filter(pk=instance.pk).update(content=content)

    scrud_post_save.connect(move, weak=False, dispatch_uid="test-post-commit-move")
    try:
        yield
    finally:
        scrud_post_save.disconnect(dispatch_uid="test-post-commit-move")


def a_todo(content=NOT_STARTED):
    reverse("todos-list")
    return ResourceRegistration.register(dict(content), "todos")


class ReplicaRouter:
    """Reads from "other" (a read replica), writes to default."""

    def db_for_read(self, model, **hints):
        return "other"

    def db_for_write(self, model, **hints):
        return "default"

    def allow_relation(self, obj1, obj2, **hints):
        return True


def read_from_the_replica(todo):
    """`todo` as a read through `ReplicaRouter` returns it: its
    ``_state.db`` is "other". (The test's "other" database is not a copy of
    default, so it is read from default, with its type, and given the
    replica's alias.)"""
    resources = Resource.objects.using("default").select_related("resource_type")
    instance = resources.get(pk=todo.pk)
    instance._state.db = "other"
    return instance


@pytest.mark.django_db
def test_a_create_refused_after_its_save_runs_no_side_effect(
    ran, indexed, django_capture_on_commit_callbacks
):
    reverse("todos-list")
    before = Resource.objects.count()
    with (
        todos_moved_after_save(),
        django_capture_on_commit_callbacks(execute=True),
    ):
        response = default_context_todo_writer().post(
            reverse("todos-list"), NOT_STARTED, format="json"
        )
    assert response.status_code == 403, response.content
    assert Resource.objects.count() == before
    assert ran == []
    assert indexed == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "context, client, status",
    [
        ("other", default_context_todo_writer, 403),
        ("__x", admin_client, 400),
        (None, admin_client, 201),
    ],
    ids=["refused-403", "reserved-context-400", "created"],
)
def test_a_committed_create_runs_its_side_effects_only_if_it_stands(
    context, client, status, ran, indexed, committed_types
):
    # Committed for real: a POST with no preconditions runs in no
    # transaction of the request's, so save_checked's is the outermost and
    # its commit, or its rollback, decides what runs. (In a test's own
    # transaction, as above, nothing commits until after the response.)
    before = Resource.objects.count()
    # Only the POST's: not what committed_types registered.
    ran.clear()
    indexed.clear()
    moved = todos_moved_after_save(context) if context else nullcontext()
    with moved:
        response = client().post(reverse("todos-list"), NOT_STARTED, format="json")
    assert response.status_code == status, response.content
    if status == 201:
        pk = Resource.objects.latest("pk").pk
        assert ran == [("save", pk)]
        assert indexed == [("update", pk)]
    else:
        assert Resource.objects.count() == before
        assert ran == []
        assert indexed == []


@pytest.mark.django_db
def test_an_update_refused_after_its_save_runs_no_side_effect(
    ran, indexed, django_capture_on_commit_callbacks
):
    todo = a_todo()
    with (
        todos_moved_after_save(),
        django_capture_on_commit_callbacks(execute=True),
    ):
        response = default_context_todo_writer().put(
            reverse("todos-detail", args=[todo.pk]),
            IN_PROGRESS,
            format="json",
            HTTP_IF_MATCH=quote_etag(todo.etag),
        )
    assert response.status_code == 403, response.content
    assert Resource.objects.get(pk=todo.pk).content == NOT_STARTED
    assert ran == []
    assert indexed == []


@pytest.mark.django_db
def test_a_create_runs_its_side_effects_once_after_commit(
    ran, indexed, django_capture_on_commit_callbacks
):
    reverse("todos-list")
    with django_capture_on_commit_callbacks() as callbacks:
        response = admin_client().post(
            reverse("todos-list"), NOT_STARTED, format="json"
        )
        assert response.status_code == 201, response.content
    # Nothing ran inside the write's transaction.
    assert ran == []
    assert indexed == []
    for callback in callbacks:
        callback()
    pk = Resource.objects.latest("pk").pk
    assert ran == [("save", pk)]
    assert indexed == [("update", pk)]


@pytest.mark.django_db
def test_an_update_runs_its_side_effects_once_after_commit(
    ran, indexed, django_capture_on_commit_callbacks
):
    todo = a_todo()
    with django_capture_on_commit_callbacks() as callbacks:
        response = admin_client().put(
            reverse("todos-detail", args=[todo.pk]),
            IN_PROGRESS,
            format="json",
            HTTP_IF_MATCH=quote_etag(todo.etag),
        )
        assert response.status_code == 200, response.content
    assert ran == []
    assert indexed == []
    for callback in callbacks:
        callback()
    assert ran == [("save", todo.pk)]
    assert indexed == [("update", todo.pk)]


@pytest.mark.django_db
def test_a_delete_runs_its_side_effects_once_after_commit(
    ran, indexed, django_capture_on_commit_callbacks
):
    todo = a_todo()
    with django_capture_on_commit_callbacks() as callbacks:
        response = admin_client().delete(
            reverse("todos-detail", args=[todo.pk]),
            HTTP_IF_MATCH=quote_etag(todo.etag),
        )
        assert response.status_code == 204, response.content
    assert ran == []
    assert indexed == []
    for callback in callbacks:
        callback()
    assert ran == [("delete", todo.etag, todo.pk)]
    # The index entry is named before the instance loses its pk.
    assert indexed == [("remove", f"scrud_django.resource.{todo.pk}")]


@pytest.mark.django_db
def test_scrud_post_save_still_fires_inside_the_write(
    ran, django_capture_on_commit_callbacks
):
    todo = a_todo()
    received = []

    def record(sender, instance, **kwargs):
        received.append(instance.pk)

    scrud_post_save.connect(record)
    try:
        with django_capture_on_commit_callbacks() as callbacks:
            ResourceRegistration.update(dict(IN_PROGRESS), "todos", str(todo.pk))
            # The signal fired inside the save; the action waits for commit.
            assert received == [todo.pk]
            assert ran == []
    finally:
        scrud_post_save.disconnect(record)
    for callback in callbacks:
        callback()
    assert ran == [("save", todo.pk)]


@pytest.mark.django_db
def test_an_on_save_action_gets_transitions_and_transition_error(
    monkeypatch, django_capture_on_commit_callbacks
):
    from scrud_django.workflow import TransitionData

    todo = a_todo(dict(NOT_STARTED, rbac_context="a"))
    received = []

    def on_save(sender, instance, **kwargs):
        received.append(kwargs)

    monkeypatch.setitem(actions[TODO], "on_save", on_save)
    with django_capture_on_commit_callbacks(execute=True):
        ResourceRegistration.update(
            dict(IN_PROGRESS, rbac_context="a"), "todos", str(todo.pk)
        )
    (kwargs,) = received
    started = TransitionData("in progress", "not started", "start")
    assert kwargs["transition_data"] == started
    assert kwargs["transitions"] == {"a": started}
    assert kwargs["transition_error"] is None


@pytest.mark.django_db(databases=["default", "other"])
def test_a_save_to_a_database_without_the_row_is_reported_as_a_create():
    """A fetched instance copied to another database (``save(using=...)``)
    whose row is not there: no prior content, so it is reported as a create
    (``__INITIALIZE__``), not an ``AttributeError``."""
    from scrud_django.models import ResourceType
    from scrud_django.workflow import TransitionData

    todo = a_todo(dict(IN_PROGRESS, rbac_context="a"))
    # The type (without its documents) on "other", for the row's foreign key.
    copy = ResourceType.objects.get(pk=todo.resource_type_id)
    copy.schema_id = copy.context_id = copy.indexing_policy_id = None
    ResourceType.objects.using("other").bulk_create([copy])
    received = []

    def record(sender, **kwargs):
        received.append(kwargs)

    scrud_post_save.connect(record, weak=False, dispatch_uid="test-missing-row")
    try:
        todo.save(using="other")
    finally:
        scrud_post_save.disconnect(dispatch_uid="test-missing-row")
    assert Resource.objects.using("other").get(pk=todo.pk).content == todo.content
    (kwargs,) = received
    assert kwargs["using"] == "other"
    initialized = TransitionData("in progress", None, "__INITIALIZE__")
    assert kwargs["transition_data"] == initialized
    assert kwargs["transitions"] == {"a": initialized}
    assert kwargs["transition_error"] is None


# An action that raises never fails the write: it is logged, once, on the
# scrud_django.workflow_actions logger, naming the action, the type, the kind
# of action and the resource's pk.

ACTIONS_LOGGER = "scrud_django.workflow_actions"


def assert_names_the_failed_action(record, kind, pk):
    assert record.name == ACTIONS_LOGGER
    message = record.getMessage()
    assert f"tests.test_post_commit.failing_{kind}.<locals>.{kind}" in message, message
    assert TODO in message
    assert f"{kind} " in message
    assert f"pk {pk}" in message


@pytest.mark.django_db
def test_an_action_that_raises_on_commit_does_not_fail_the_write(
    failing_on_save, caplog, django_capture_on_commit_callbacks
):
    reverse("todos-list")
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client().post(
            reverse("todos-list"), NOT_STARTED, format="json"
        )
    assert response.status_code == 201, response.content
    pk = Resource.objects.latest("pk").pk
    assert Resource.objects.get(pk=pk).content["status"] == "not started"
    assert failing_on_save == [pk]
    (record,) = logged_action_failures(caplog)
    assert f"on_save failed for {pk}" in record.getMessage()
    assert_names_the_failed_action(record, "on_save", pk)


@pytest.mark.django_db
def test_an_action_that_raises_outside_a_transaction_does_not_raise(
    failing_on_save, caplog
):
    """Outside a transaction (autocommit) ``on_commit`` runs the action at
    once, inside the signal, and it still must not raise. The test's own
    connection is in a transaction, so the signal is handled on another
    thread, which has its own connection in autocommit. Nothing is written."""
    processor = apps.get_app_config("scrud_django").processor
    raised = []

    def trigger():
        try:
            assert connection.get_autocommit()
            processor.post_save_trigger(
                Resource, instance=Resource(pk=-1), resource_type_uri=TODO
            )
        except BaseException as error:
            raised.append(error)
        finally:
            connections.close_all()

    thread = threading.Thread(target=trigger)
    thread.start()
    thread.join()
    assert raised == []
    assert failing_on_save == [-1]
    (record,) = logged_action_failures(caplog)
    assert "on_save failed for -1" in record.getMessage()
    assert_names_the_failed_action(record, "on_save", -1)


@pytest.mark.django_db
def test_an_on_delete_action_that_raises_is_logged_with_the_deleted_pk(
    failing_on_delete, caplog, django_capture_on_commit_callbacks
):
    todo = a_todo()
    todo_pk = todo.pk
    with django_capture_on_commit_callbacks(execute=True):
        response = admin_client().delete(
            reverse("todos-detail", args=[todo_pk]),
            HTTP_IF_MATCH=quote_etag(todo.etag),
        )
    assert response.status_code == 204, response.content
    assert not Resource.objects.filter(pk=todo_pk).exists()
    assert failing_on_delete == [todo_pk]
    (record,) = logged_action_failures(caplog)
    assert f"on_delete failed for {todo_pk}" in record.getMessage()
    assert_names_the_failed_action(record, "on_delete", todo_pk)


class RolledBack(Exception):
    pass


@pytest.mark.django_db(transaction=True, databases=["default", "other"])
@pytest.mark.parametrize("commit", [False, True], ids=["rolled-back", "committed"])
def test_a_delete_of_a_resource_read_from_a_replica_acts_when_it_commits(
    commit, ran, committed_types
):
    # Committed for real, on both databases: "other" is in autocommit, so an
    # action registered there would run at once, inside the delete.
    todo = read_from_the_replica(a_todo())
    todo_pk = todo.pk
    ran.clear()
    with override_settings(DATABASE_ROUTERS=["tests.test_post_commit.ReplicaRouter"]):
        try:
            with transaction.atomic(using="default"):
                todo.delete()
                # The delete's action waits for the write database's commit.
                assert ran == []
                if not commit:
                    raise RolledBack
        except RolledBack:
            pass
    assert Resource.objects.using("default").filter(pk=todo_pk).exists() != commit
    assert ran == ([("delete", todo.etag, todo_pk)] if commit else [])


@pytest.mark.django_db(databases=["default", "other"])
def test_scrud_signals_name_the_database_written_to_not_the_one_read_from():
    todo = read_from_the_replica(a_todo())
    received = []

    def record(sender, using=None, **kwargs):
        received.append(using)

    scrud_post_save.connect(record)
    scrud_post_delete.connect(record)
    try:
        with override_settings(
            DATABASE_ROUTERS=["tests.test_post_commit.ReplicaRouter"]
        ):
            # A sender that names no database: the router's write database.
            assert write_alias(Resource, {"instance": todo}) == "default"
            todo.save()
            todo._state.db = "other"
            todo.delete()
    finally:
        scrud_post_save.disconnect(record)
        scrud_post_delete.disconnect(record)
    assert received == ["default", "default"]


@pytest.mark.django_db
def test_scrud_post_delete_names_the_deleted_row():
    todo = a_todo()
    todo_pk = todo.pk
    received = []

    def record(sender, instance, pk=None, **kwargs):
        received.append((instance.pk, pk))

    scrud_post_delete.connect(record)
    try:
        ResourceRegistration.delete("todos", str(todo_pk))
        other = a_todo()
        other_pk = other.pk
        other.delete()
    finally:
        scrud_post_delete.disconnect(record)
    # The instance is Django's deleted instance (no pk); `pk` names its row.
    assert received == [(None, todo_pk), (None, other_pk)]


# A queryset's delete() (bulk deletes, the admin's "delete selected") sends
# scrud_post_delete for each resource it deletes, as Resource.delete() does.


@pytest.mark.django_db
def test_a_queryset_delete_runs_on_delete_once_per_row_after_commit(
    ran, django_capture_on_commit_callbacks
):
    todos = [a_todo() for _ in range(3)]
    kept = a_todo()
    profile = ResourceRegistration.register({"name": "x"}, "partner-profiles")
    deleted = [*todos[:2], profile]
    received = []

    def record(sender, instance, **kwargs):
        received.append((sender, instance.pk, kwargs))

    ran.clear()
    scrud_post_delete.connect(record)
    try:
        with django_capture_on_commit_callbacks() as callbacks:
            Resource.objects.filter(pk__in=[r.pk for r in deleted]).delete()
            # The signal fired inside the delete; the actions wait for commit.
            assert len(received) == 3
            assert ran == []
    finally:
        scrud_post_delete.disconnect(record)
    assert sorted(kwargs["pk"] for _, _, kwargs in received) == sorted(
        r.pk for r in deleted
    )
    for sender, instance_pk, kwargs in received:
        assert sender is Resource
        # Django's deleted instance, as for Resource.delete().
        assert instance_pk is None
        assert kwargs["using"] == "default"
        assert kwargs["action"] == "DELETE"
        expected_type = (
            "tests://PartnerProfiles" if kwargs["pk"] == profile.pk else TODO
        )
        assert kwargs["resource_type_uri"] == expected_type
    for callback in callbacks:
        callback()
    # Once per deleted ToDo (the profile's type has no action).
    assert sorted(ran) == sorted(("delete", t.etag, t.pk) for t in todos[:2])
    assert Resource.objects.filter(pk__in=[todos[2].pk, kept.pk]).count() == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("commit", [False, True], ids=["rolled-back", "committed"])
def test_a_queryset_delete_acts_only_when_it_commits(commit, ran, committed_types):
    todos = [a_todo() for _ in range(2)]
    pks = [t.pk for t in todos]
    ran.clear()
    try:
        with transaction.atomic():
            Resource.objects.filter(pk__in=pks).delete()
            assert ran == []
            if not commit:
                raise RolledBack
    except RolledBack:
        pass
    assert Resource.objects.filter(pk__in=pks).count() == (0 if commit else 2)
    assert sorted(ran) == (
        sorted(("delete", t.etag, t.pk) for t in todos) if commit else []
    )


@pytest.mark.django_db
def test_a_raw_save_sends_no_scrud_signal(ran, django_capture_on_commit_callbacks):
    """A raw save (``loaddata``, a django-reversion revert) is Django's
    ``save_base(raw=True)``: it sends ``post_save`` with ``raw=True``, but no
    scrud signal, so no workflow action runs (fixtures have no side
    effects)."""
    todo = a_todo()
    received = []

    def record(sender, **kwargs):
        received.append(kwargs)

    ran.clear()
    scrud_post_save.connect(record)
    try:
        with django_capture_on_commit_callbacks(execute=True):
            todo.content = dict(IN_PROGRESS)
            todo.save_base(raw=True)
    finally:
        scrud_post_save.disconnect(record)
    assert Resource.objects.get(pk=todo.pk).content == IN_PROGRESS
    assert received == []
    assert ran == []
