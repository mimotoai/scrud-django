"""A delete outside the API (``Resource.delete()``, a queryset's
``delete()``, the admin's delete and "delete selected") locks the resource
rows (``select_for_update``, in primary key order) before Django's
``Collector`` deletes anything, as the API's locked writes do: they lock the
resource row first and its ``ResourceContext`` rows after, so a delete that
took the rows in the other order deadlocked with them on PostgreSQL.

The ``WorkflowPolicy`` type lock is ``FOR NO KEY UPDATE`` where the database
has it: it serializes policy writers, but does not block the foreign key
check of a ``ResourceRemoval`` row a concurrent delete inserts. MySQL and
MariaDB lack it, and check a foreign key at once, waiting for that lock:
there a delete (or a ``scrud_sync_contexts`` repair) of a policy takes the
type lock before the policy's row, as the API's writes do.

The rows are locked by key, after their keys are read: on MySQL and MariaDB
a locking read of the queryset's own query would also lock what it joins (a
``ResourceType`` row, which every insert of that type then waits for).

SQLite ignores ``select_for_update``; the order of the statements is checked
on every database, and the two-connection races run on PostgreSQL, MySQL and
MariaDB.
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

# Quoted as the database quotes them ("x" or `x`).
RESOURCE_TABLE = connection.ops.quote_name("scrud_django_resource")
CONTEXT_TABLE = connection.ops.quote_name("scrud_django_resourcecontext")


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
    assert order.startswith(
        ("1 ASC", f"{RESOURCE_TABLE}.{connection.ops.quote_name('id')} ASC")
    ), locking_select
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


@pytest.mark.django_db
def test_a_batched_queryset_delete_deletes_every_locked_row():
    from scrud_django.models import resource as resource_module

    stored = profiles("a", "b", "c")
    pks = [r.pk for r in stored]
    with mock.patch.object(resource_module, "DELETE_BATCH_SIZE", 1):
        deleted, per_model = Resource.objects.filter(pk__in=pks).delete()
    assert per_model == {
        "scrud_django.Resource": 3,
        "scrud_django.ResourceContext": 3,
    }, per_model
    assert deleted == 6
    assert not Resource.objects.filter(pk__in=pks).exists()


@pytest.mark.django_db
def test_a_protected_row_in_a_later_batch_undoes_the_earlier_ones():
    from django.db.models import ProtectedError
    from django.utils import timezone

    from scrud_django.models import ResourceType
    from scrud_django.models import resource as resource_module

    (plain,) = profiles("a")
    (schema,) = profiles("b")
    ResourceType.objects.create(
        type_uri="tests://UsesTheSchema",
        slug="uses-the-schema",
        schema=schema,
        modified_at=timezone.now(),
        etag="e",
        revision="1",
    )
    with (
        transaction.atomic(),
        mock.patch.object(resource_module, "DELETE_BATCH_SIZE", 1),
    ):
        with pytest.raises(ProtectedError):
            Resource.objects.filter(pk__in=[plain.pk, schema.pk]).delete()
        # The first batch (plain) was undone, and the transaction is usable.
        assert Resource.objects.filter(pk__in=[plain.pk, schema.pk]).count() == 2


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


POLICY = {
    "resource_type": "tests://ToDo",
    "initial_states": {"any": "true"},
    "states": {},
    "transitions": {},
    "rbac_context": "pol",
}


def a_policy():
    reverse("todos-list")
    return ResourceRegistration.register(dict(POLICY), "workflow-policies")


def locks_in_order(events):
    return [event[1] for event in events if event[0] == "lock"]


@pytest.mark.django_db
@pytest.mark.parametrize("how", ["model", "queryset"])
def test_a_delete_of_a_policy_locks_its_type_before_its_row(how):
    stored = a_policy()
    (other,) = profiles("a")
    with recorded_statements() as events:
        if how == "model":
            Resource.objects.get(pk=stored.pk).delete()
        else:
            Resource.objects.filter(pk__in=[stored.pk, other.pk]).delete()
    assert locks_in_order(events)[:2] == ["ResourceType", "Resource"], events
    assert_locked_before_deleted(events)
    assert not Resource.objects.filter(pk=stored.pk).exists()


@pytest.mark.django_db
def test_a_delete_of_other_resources_takes_no_policy_lock():
    stored = profiles("a", "b")
    with recorded_statements() as events:
        Resource.objects.filter(pk__in=[r.pk for r in stored]).delete()
    assert "ResourceType" not in locks_in_order(events), events


@pytest.mark.django_db
@pytest.mark.parametrize("cached", [True, False])
def test_a_save_of_a_policy_locks_its_type_before_its_row(cached):
    stored = a_policy()
    policy = Resource.objects.get(pk=stored.pk)
    if cached:
        policy.resource_type  # noqa: B018 - read, and cached on the instance
    policy.content = dict(POLICY, rbac_context="moved")
    with recorded_statements() as events:
        policy.save()
    lock = events.index(
        ("lock", "ResourceType", connection.features.has_select_for_no_key_update)
    )
    update = next(
        i
        for i, event in enumerate(events)
        if event[0] == "sql" and event[1].startswith(f"UPDATE {RESOURCE_TABLE}")
    )
    assert lock < update, events


@pytest.mark.django_db
def test_a_save_of_other_resources_takes_no_policy_lock():
    (stored,) = profiles("a")
    resource = Resource.objects.get(pk=stored.pk)
    resource.content = {"name": "a", "rbac_context": "b"}
    with recorded_statements() as events:
        resource.save()
    assert "ResourceType" not in locks_in_order(events), events


@pytest.mark.django_db
def test_a_sync_repair_of_a_policy_locks_its_type_before_its_row():
    from scrud_django.models.resource import resync_contexts

    stored = a_policy()
    Resource.objects.filter(pk=stored.pk).update(
        content=dict(POLICY, rbac_context="moved")
    )
    with transaction.atomic(), recorded_statements() as events:
        assert resync_contexts(stored.pk, "default") == ({"pol"}, {"moved"})
    assert locks_in_order(events) == ["ResourceType", "Resource"], events


@pytest.mark.django_db
def test_a_sync_check_of_a_policy_takes_no_policy_lock():
    from scrud_django.models.resource import resync_contexts

    stored = a_policy()
    Resource.objects.filter(pk=stored.pk).update(
        content=dict(POLICY, rbac_context="moved")
    )
    with transaction.atomic(), recorded_statements() as events:
        assert resync_contexts(stored.pk, "default", repair=False) == (
            {"pol"},
            {"moved"},
        )
    assert locks_in_order(events) == ["Resource"], events


# PostgreSQL, MySQL and MariaDB: two connections.

TWO_WRITERS = pytest.mark.skipif(
    connection.vendor == "sqlite", reason="SQLite runs one writer at a time"
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


@TWO_WRITERS
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


@TWO_WRITERS
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


@TWO_WRITERS
@pytest.mark.django_db(transaction=True)
def test_a_queryset_delete_deletes_only_the_rows_it_locked(committed_types):
    # The bulk delete filters on content and locks what matches (nothing).
    # A server-side update then moves R into the filtered context and
    # commits, and an API PUT locks R and pauses. If the Collector ran the
    # filter again, it would collect R and delete its ResourceContext rows
    # without R's lock, while the PUT holds R and waits for them: a deadlock.
    # Deleting only the locked keys leaves R alone, as a delete that ran a
    # moment earlier would.
    from scrud_django.models import resource as resource_module

    (stored,) = profiles("x")
    url = reverse("partner-profiles-detail", args=[stored.pk])
    client = admin_api_client()
    connections.close_all()
    slow = threading.local()
    original_lock = resource_module.lock_for_delete

    def lock_then_wait(queryset, using):
        locked = original_lock(queryset, using)
        if getattr(slow, "on", False):
            time.sleep(1.0)  # between the lock and the Collector
        return locked

    results = {}

    def run(name, function, wait):
        time.sleep(wait)
        try:
            results[name] = function()
        except Exception as exc:  # noqa: BLE001 - reported to the test
            results[name] = exc
        finally:
            connections.close_all()

    def bulk():
        slow.on = True
        return Resource.objects.filter(content__rbac_context="y").delete()[0]

    def mover():
        ResourceRegistration.update(
            {"name": "x", "rbac_context": "y"}, "partner-profiles", str(stored.pk)
        )

    def api():
        with paused_while_locked(pre_save):
            return client.put(
                url,
                {"name": "x", "rbac_context": "z"},
                format="json",
                HTTP_IF_MATCH="*",
            ).status_code

    with mock.patch.object(resource_module, "lock_for_delete", lock_then_wait):
        threads = [
            threading.Thread(target=run, args=args)
            for args in (("bulk", bulk, 0), ("mover", mover, 0.2), ("api", api, 0.4))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
    assert not isinstance(results["bulk"], Exception), repr(results["bulk"])
    assert results["bulk"] == 0
    assert results["mover"] is None, repr(results["mover"])
    assert results["api"] == 200, results["api"]
    assert Resource.objects.get(pk=stored.pk).content["rbac_context"] == "z"


@TWO_WRITERS
@pytest.mark.django_db(transaction=True)
def test_a_queryset_delete_leaves_a_row_moved_out_of_its_filter(committed_types):
    # R is in "y". An API PUT locks R, moves it to "z" and pauses; a bulk
    # delete of "y" then waits on R's lock. Once the PUT commits, PostgreSQL
    # re-checks only the lock's `id IN (...)` on the new row version, so R is
    # locked although it left "y". The delete must re-apply its filter to the
    # locked keys and leave R alone, as a delete that ran a moment later would.
    (stored,) = profiles("y")
    url = reverse("partner-profiles-detail", args=[stored.pk])
    client = admin_api_client()
    connections.close_all()

    def api():
        with paused_while_locked(pre_save):
            return client.put(
                url,
                {"name": "x", "rbac_context": "z"},
                format="json",
                HTTP_IF_MATCH="*",
            ).status_code

    def bulk():
        return Resource.objects.filter(content__rbac_context="y").delete()[0]

    status, deleted = run_together(api, bulk, delay=0.3)
    assert status == 200, status
    assert not isinstance(deleted, Exception), repr(deleted)
    assert deleted == 0
    moved = Resource.objects.filter(pk=stored.pk).first()
    assert moved is not None, "deleted although it had left the filter"
    assert moved.content["rbac_context"] == "z"


@TWO_WRITERS
@pytest.mark.django_db(transaction=True)
def test_a_delete_filtered_by_type_does_not_block_inserts_of_that_type(
    committed_types,
):
    # A bulk delete filtered through the type (a join) holds its locks for
    # its transaction. A resource of that type created meanwhile must not
    # wait for it: on MySQL and MariaDB a locking read of the join locked
    # the ResourceType row, which the insert's foreign key check waits for.
    (stored,) = profiles("a")
    connections.close_all()
    times = {}

    def bulk():
        with transaction.atomic():
            deleted = Resource.objects.filter(
                resource_type__slug="partner-profiles", pk=stored.pk
            ).delete()[0]
            time.sleep(1.5)
            times["bulk"] = time.monotonic()
        return deleted

    def create():
        ResourceRegistration.register(
            {"name": "new", "rbac_context": "b"}, "partner-profiles"
        )
        times["create"] = time.monotonic()

    deleted, created = run_together(bulk, create, delay=0.3)
    assert not isinstance(deleted, Exception), repr(deleted)
    assert not isinstance(created, Exception), repr(created)
    assert deleted >= 1
    assert times["create"] < times["bulk"], times


@TWO_WRITERS
@pytest.mark.django_db(transaction=True)
def test_a_sync_repair_of_a_policy_and_its_api_put_do_not_deadlock(committed_types):
    # The repair of a drifted policy stamps the context it left, a
    # ResourceRemoval of the WorkflowPolicy type. An API PUT of the policy
    # holds the type lock and waits for the policy's row. A repair that
    # held the row first waited for the type (the foreign key check, on
    # MySQL and MariaDB) and deadlocked; taking the type lock first, it
    # runs before or after the PUT.
    from scrud_django.models import resource as resource_module

    stored = a_policy()
    Resource.objects.filter(pk=stored.pk).update(
        content=dict(POLICY, rbac_context="moved")
    )
    url = reverse("workflow-policies-detail", args=[stored.pk])
    client = admin_api_client()
    connections.close_all()
    original_sync = resource_module.sync_resource_contexts
    slow = threading.local()

    def sync_after_a_while(resource, using, **kwargs):
        if getattr(slow, "on", False):
            time.sleep(0.6)  # holding the policy's row
        return original_sync(resource, using, **kwargs)

    def repair():
        slow.on = True
        return resource_module.resync_contexts(stored.pk, "default")

    def put():
        return client.put(
            url,
            dict(POLICY, rbac_context="moved", initial_states={"x": "true"}),
            format="json",
            HTTP_IF_MATCH="*",
        ).status_code

    with mock.patch.object(
        resource_module, "sync_resource_contexts", sync_after_a_while
    ):
        repaired, status = run_together(repair, put, delay=0.2)
    assert not isinstance(repaired, Exception), repr(repaired)
    assert repaired == ({"pol"}, {"moved"}), repaired
    assert status == 200, status
    assert list(resource_module.find_context_drift("default")) == []


@TWO_WRITERS
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("how", ["model", "queryset"])
def test_a_delete_of_a_policy_holding_its_row_and_an_api_put_do_not_deadlock(
    how, committed_types
):
    # The delete locks the policy's row and pauses; an API PUT of the policy
    # then takes the type lock and waits for the row. The delete's removal
    # stamp (a ResourceRemoval of the WorkflowPolicy type) checks its foreign
    # key against the type's row: on MySQL and MariaDB that waits for the
    # PUT's type lock, a deadlock, unless the delete took the type lock first.
    from scrud_django.models import resource as resource_module

    stored = a_policy()
    url = reverse("workflow-policies-detail", args=[stored.pk])
    client = admin_api_client()
    connections.close_all()
    original_lock = resource_module.lock_for_delete
    slow = threading.local()

    def lock_then_wait(queryset, using):
        locked = original_lock(queryset, using)
        if getattr(slow, "on", False):
            time.sleep(0.6)  # holding the policy's row
        return locked

    def delete():
        slow.on = True
        if how == "model":
            return Resource.objects.get(pk=stored.pk).delete()[0]
        return Resource.objects.filter(pk=stored.pk).delete()[0]

    def put():
        return client.put(
            url,
            dict(POLICY, initial_states={"x": "true"}),
            format="json",
            HTTP_IF_MATCH="*",
        ).status_code

    with mock.patch.object(resource_module, "lock_for_delete", lock_then_wait):
        deleted, status = run_together(delete, put, delay=0.2)
    assert not isinstance(deleted, Exception), repr(deleted)
    assert deleted >= 1
    assert status == 404, status
    assert not Resource.objects.filter(pk=stored.pk).exists()


@TWO_WRITERS
@pytest.mark.django_db(transaction=True)
def test_a_save_moving_a_policy_and_its_api_put_do_not_deadlock(committed_types):
    # Resource.save() (the admin's change form, server code) of a policy that
    # leaves a context updates its row, then stamps the context it left: a
    # ResourceRemoval of the WorkflowPolicy type. An API PUT of the policy
    # meanwhile holds the type lock and waits for the row: on MySQL and
    # MariaDB the stamp's foreign key check waited for the PUT, a deadlock,
    # unless the save took the type lock first.
    from scrud_django.models import resource as resource_module

    stored = a_policy()
    url = reverse("workflow-policies-detail", args=[stored.pk])
    client = admin_api_client()
    connections.close_all()
    original_stamp = resource_module.stamp_removals
    slow = threading.local()

    def stamp_after_a_while(*args, **kwargs):
        if getattr(slow, "on", False):
            time.sleep(0.6)  # holding the policy's row
        return original_stamp(*args, **kwargs)

    def save():
        slow.on = True
        policy = Resource.objects.get(pk=stored.pk)
        policy.content = dict(POLICY, rbac_context="moved")
        policy.save()

    def put():
        return client.put(
            url,
            dict(POLICY, initial_states={"x": "true"}),
            format="json",
            HTTP_IF_MATCH="*",
        ).status_code

    with mock.patch.object(resource_module, "stamp_removals", stamp_after_a_while):
        saved, status = run_together(save, put, delay=0.2)
    assert not isinstance(saved, Exception), repr(saved)
    # The PUT (a superuser's, who may write it in any context) waited for
    # the save, and then wrote over it.
    assert status == 200, status
    content = Resource.objects.get(pk=stored.pk).content
    assert (content["rbac_context"], content["initial_states"]) == (
        "pol",
        {"x": "true"},
    )
