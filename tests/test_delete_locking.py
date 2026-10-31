"""A delete outside the API (``Resource.delete()``, a queryset's
``delete()``, the admin's delete and "delete selected") locks the resource
rows (``select_for_update``, in primary key order) before Django's
``Collector`` deletes anything, as the API's locked writes do: they lock the
resource row first and its ``ResourceContext`` rows after, so a delete that
took the rows in the other order deadlocked with them on PostgreSQL.

The ``WorkflowPolicy`` type lock is ``FOR NO KEY UPDATE`` where the database
has it: it serializes policy writers, but does not block the foreign key
check of a ``ResourceRemoval`` row a concurrent delete inserts.

SQLite ignores ``select_for_update``; the order of the statements is checked
on every database, and the two-connection races run on PostgreSQL only.
"""

import threading
import time
from contextlib import contextmanager
from unittest import mock

import pytest
from django.contrib.auth.models import Permission
from django.db import connection, connections, transaction
from django.db.models import QuerySet
from django.db.models.signals import pre_delete, pre_save
from django.test import Client
from django.urls import reverse
from rest_framework.test import APIClient

from scrud_django import registration
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.workflow import lock_workflow_policy_type

from .factories import UserFactory

RESOURCE_TABLE = '"scrud_django_resource"'
CONTEXT_TABLE = '"scrud_django_resourcecontext"'


@contextmanager
def recorded_statements():
    """The locks asked for (``("lock", model name, no_key)``, when
    ``select_for_update`` is called) and the SQL run (``("sql", sql)``) on
    the default database, in order."""
    events = []
    original = QuerySet.select_for_update

    def select_for_update(self, *args, **kwargs):
        events.append(("lock", self.model.__name__, kwargs.get("no_key", False)))
        return original(self, *args, **kwargs)

    def record(execute, sql, params, many, context):
        events.append(("sql", sql))
        return execute(sql, params, many, context)

    with (
        mock.patch.object(QuerySet, "select_for_update", select_for_update),
        connection.execute_wrapper(record),
    ):
        yield events


def assert_locked_before_deleted(events):
    """The resource rows were locked, in pk order, before any row was
    deleted."""
    first_delete = next(
        i
        for i, event in enumerate(events)
        if event[0] == "sql" and event[1].startswith("DELETE")
    )
    lock = next(
        (i for i, event in enumerate(events) if event[:2] == ("lock", "Resource")),
        None,
    )
    assert lock is not None and lock < first_delete, events
    locking_select = next(
        event[1]
        for event in events[lock + 1 :]
        if event[0] == "sql" and event[1].startswith("SELECT")
    )
    head = locking_select.partition(" WHERE ")[0]
    assert f"FROM {RESOURCE_TABLE}" in head, locking_select
    # Ordered by the key, the only column it selects (ORDER BY 1).
    order = locking_select.rpartition(" ORDER BY ")[2]
    assert order.startswith(("1 ASC", f'{RESOURCE_TABLE}."id" ASC')), locking_select
    if connection.features.has_select_for_update:
        assert "FOR UPDATE" in locking_select, locking_select


def profiles(*contexts):
    return [
        ResourceRegistration.register(
            {"name": c, "rbac_context": c}, "partner-profiles"
        )
        for c in contexts
    ]


@pytest.mark.django_db
def test_model_delete_locks_the_row_before_it_deletes_anything():
    (resource,) = profiles("a")
    with recorded_statements() as events:
        Resource.objects.get(pk=resource.pk).delete()
    assert_locked_before_deleted(events)
    assert not Resource.objects.filter(pk=resource.pk).exists()


@pytest.mark.django_db
def test_queryset_delete_locks_the_rows_in_pk_order_before_it_deletes_anything():
    stored = profiles("b", "a", "b")
    pks = [r.pk for r in stored]
    with recorded_statements() as events:
        deleted, _ = Resource.objects.filter(pk__in=pks).order_by("-pk").delete()
    assert deleted >= 3
    assert_locked_before_deleted(events)
    assert not Resource.objects.filter(pk__in=pks).exists()


@pytest.mark.django_db
def test_model_delete_inside_a_transaction_keeps_it_usable_after_protected_error():
    # Locking takes no savepoint-less atomic block inside the caller's
    # transaction: a ProtectedError the caller catches leaves it usable, as
    # Model.delete() always has.
    from django.db.models import ProtectedError

    reverse("partner-profiles-list")
    schema = registration.get_resource_type_for("partner-profiles").schema
    with transaction.atomic():
        with pytest.raises(ProtectedError):
            Resource.objects.get(pk=schema.pk).delete()
        assert Resource.objects.filter(pk=schema.pk).exists()


@pytest.mark.django_db
def test_queryset_delete_inside_a_transaction_keeps_it_usable_after_protected_error():
    # As for Model.delete(): a ProtectedError the caller catches leaves its
    # transaction usable (a savepoint-less atomic block marked it for
    # rollback, so its next query failed).
    from django.db.models import ProtectedError

    reverse("partner-profiles-list")
    schema = registration.get_resource_type_for("partner-profiles").schema
    with transaction.atomic():
        with pytest.raises(ProtectedError):
            Resource.objects.filter(pk=schema.pk).delete()
        assert Resource.objects.filter(pk=schema.pk).exists()


def superuser_admin():
    user = UserFactory(is_staff=True, is_superuser=True)
    user.user_permissions.set(
        Permission.objects.filter(content_type__app_label="scrud_django")
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_the_admins_delete_locks_the_row_before_it_deletes_anything():
    (resource,) = profiles("a")
    client = superuser_admin()
    with recorded_statements() as events:
        response = client.post(
            reverse("admin:scrud_django_resource_delete", args=[resource.pk]),
            {"post": "yes"},
        )
    assert response.status_code == 302, response.content
    assert_locked_before_deleted(events)
    assert not Resource.objects.filter(pk=resource.pk).exists()


@pytest.mark.django_db
def test_the_admins_delete_selected_locks_the_rows_before_it_deletes_anything():
    stored = profiles("a", "b")
    client = superuser_admin()
    with recorded_statements() as events:
        response = client.post(
            reverse("admin:scrud_django_resource_changelist"),
            {
                "action": "delete_selected",
                "_selected_action": [r.pk for r in stored],
                "post": "yes",
            },
        )
    assert response.status_code == 302, response.content
    assert_locked_before_deleted(events)
    assert not Resource.objects.filter(pk__in=[r.pk for r in stored]).exists()


@pytest.mark.django_db
def test_the_workflow_policy_type_lock_does_not_block_foreign_key_checks():
    with transaction.atomic(), recorded_statements() as events:
        lock_workflow_policy_type("default")
    locks = [event for event in events if event[0] == "lock"]
    no_key = connection.features.has_select_for_no_key_update
    assert locks == [("lock", "ResourceType", no_key)], events
    if no_key:
        assert any(
            event[0] == "sql" and event[1].endswith("FOR NO KEY UPDATE")
            for event in events
        ), events


# PostgreSQL: two connections.

POSTGRESQL = pytest.mark.skipif(
    connection.vendor != "postgresql", reason="SQLite runs one writer at a time"
)


def admin_api_client():
    client = APIClient(raise_request_exception=False)
    client.force_authenticate(UserFactory(is_staff=True, is_superuser=True))
    return client


def run_together(first, second, *, delay):
    """Run `first` and, `delay` seconds later, `second`, in two threads (two
    connections); their results (or the exceptions they raised)."""
    results = {}

    def run(name, function, wait):
        time.sleep(wait)
        try:
            results[name] = function()
        except Exception as exc:  # noqa: BLE001 - reported to the test
            results[name] = exc
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=run, args=("first", first, 0)),
        threading.Thread(target=run, args=("second", second, delay)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return results["first"], results["second"]


@contextmanager
def paused_while_locked(signal):
    """Make the first save or delete of a `Resource` in the thread that
    entered this block sleep (holding its row lock) when `signal` is sent."""
    local = threading.local()
    local.pause = True

    def hold(sender, instance, **kwargs):
        if getattr(local, "pause", False):
            local.pause = False
            time.sleep(0.6)

    signal.connect(hold, sender=Resource, weak=False, dispatch_uid="k3-hold")
    try:
        yield local
    finally:
        signal.disconnect(sender=Resource, dispatch_uid="k3-hold")


@POSTGRESQL
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("method", ["put", "delete"])
@pytest.mark.parametrize("unlocked", ["model", "queryset"])
def test_an_unlocked_delete_waits_for_a_locked_api_write(
    method, unlocked, committed_types
):
    (resource,) = profiles("before")
    url = reverse("partner-profiles-detail", args=[resource.pk])
    client = admin_api_client()
    connections.close_all()
    signal = pre_save if method == "put" else pre_delete

    def api():
        # The API write locks the row, then pauses (pre_save / pre_delete)
        # before it touches the ResourceContext rows.
        with paused_while_locked(signal):
            if method == "put":
                return client.put(
                    url,
                    {"name": "x", "rbac_context": "after"},
                    format="json",
                    HTTP_IF_MATCH="*",
                ).status_code
            return client.delete(url, HTTP_IF_MATCH="*").status_code

    def other():
        if unlocked == "model":
            stored = Resource.objects.filter(pk=resource.pk).first()
            return stored.delete() if stored is not None else (0, {})
        return Resource.objects.filter(pk=resource.pk).delete()

    api_status, deleted = run_together(api, other, delay=0.25)
    assert api_status == (200 if method == "put" else 204), api_status
    assert not isinstance(deleted, Exception), repr(deleted)
    assert not Resource.objects.filter(pk=resource.pk).exists()


@POSTGRESQL
@pytest.mark.django_db(transaction=True)
def test_a_bulk_delete_of_a_policy_and_its_api_put_do_not_deadlock(committed_types):
    reverse("todos-list")
    policy = {
        "resource_type": "tests://ToDo",
        "initial_states": {"any": "true"},
        "states": {},
        "transitions": {},
        "rbac_context": "pol",
    }
    stored = ResourceRegistration.register(policy, "workflow-policies")
    url = reverse("workflow-policies-detail", args=[stored.pk])
    client = admin_api_client()
    connections.close_all()

    def bulk():
        with transaction.atomic():
            deleted = Resource.objects.filter(pk=stored.pk).delete()[0]
            time.sleep(0.3)
        return deleted

    def put():
        return client.put(
            url,
            dict(policy, initial_states={"x": "true"}),
            format="json",
            HTTP_IF_MATCH="*",
        ).status_code

    deleted, status = run_together(bulk, put, delay=0.1)
    assert not isinstance(deleted, Exception), repr(deleted)
    assert deleted >= 1
    assert status == 404, status
