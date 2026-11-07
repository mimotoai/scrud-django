"""PUT and DELETE look the resource up, authorize it, evaluate the request's
preconditions, check the workflow and write it in one transaction on the
write database, with the row locked (``select_for_update``) before anything
reads it.

SQLite ignores ``select_for_update`` and runs one request at a time, so a
concurrent writer is simulated: its change commits right before the request
opens its first transaction on the write database
(`concurrent_write_before_the_write_transaction`). Under the lock that is
the last moment another writer can commit before the request reads the row;
before the fix the request had already read it, authorized it, evaluated its
preconditions and checked its workflow transition by then. The PostgreSQL
proof with two connections is in the batch report.
"""

import copy
import time
from contextlib import contextmanager
from datetime import timedelta
from unittest import mock
from uuid import uuid4

import pytest
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.test import override_settings
from django.urls import reverse
from django.utils.http import http_date, quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django import registration
from scrud_django.models import Resource, ResourceType
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
    write_db,
)

from .factories import UserFactory

TODO = "tests://ToDo"
WORKFLOW_POLICY = "http://api.openteams.com/json-ld/WorkflowPolicy"
IN_PROGRESS = {
    "title": "Buy milk",
    "status": "in progress",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
}
CANCELED = dict(IN_PROGRESS, status="canceled")
# Allows a ToDo only in progress or done: canceling one is refused (400).
STRICT_POLICY = {
    "resource_type": TODO,
    "initial_states": {"in progress": "status == 'in progress'"},
    "states": {"done": "status == 'done'"},
    "transitions": {},
}
DONE = dict(IN_PROGRESS, status="done")
PERMISSIVE_POLICY = {
    "resource_type": TODO,
    "initial_states": {"any": "true"},
    "states": {},
    "transitions": {},
}


def admin_client():
    client = APIClient()
    client.force_authenticate(UserFactory(is_staff=True, is_superuser=True))
    return client


def client_with(definition, context):
    user = UserFactory()
    role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
    role.save()
    RoleAssignment(
        user=user, role=role, rbac_context=context, etag=uuid4().hex, modified_at=now()
    ).save()
    client = APIClient()
    client.force_authenticate(user)
    return client


def a_todo(content=IN_PROGRESS):
    reverse("todos-list")
    return ResourceRegistration.register(dict(content), "todos")


def todo_url(todo):
    return reverse("todos-detail", args=[todo.pk])


def server_update(todo, content):
    return lambda: ResourceRegistration.update(
        content=dict(content), register_type="todos", slug=str(todo.pk)
    )


def later():
    """An If-Unmodified-Since every stored resource passes: the precondition
    lets the request through, so the workflow check decides."""
    return http_date(time.time() + 3600)


@contextmanager
def concurrent_write_before_the_write_transaction(write):
    """Run `write`, another writer's change, once: right before the request
    opens its first transaction (atomic block) on the write database."""
    pending = [write]
    original = transaction.Atomic.__enter__

    def enter(self):
        if pending and (self.using or DEFAULT_DB_ALIAS) == write_db():
            pending.pop()()
        return original(self)

    with mock.patch.object(transaction.Atomic, "__enter__", enter):
        yield
    assert not pending, "the request opened no transaction on the write database"


@pytest.mark.django_db
def test_a_transition_the_stored_state_forbids_is_refused():
    todo = a_todo()
    # in progress -> done is allowed; canceled -> done is not. The other
    # writer cancels the ToDo first.
    with concurrent_write_before_the_write_transaction(server_update(todo, CANCELED)):
        response = admin_client().put(
            todo_url(todo), DONE, format="json", HTTP_IF_UNMODIFIED_SINCE=later()
        )
    assert response.status_code == 400, response.content
    body = response.json()
    assert body["prior_state"] == "canceled", body
    assert body["proposed_state"] == "done", body
    assert Resource.objects.get(pk=todo.pk).content == CANCELED


@pytest.mark.django_db
def test_a_put_whose_if_match_a_concurrent_write_made_stale_is_412():
    todo = a_todo()
    with concurrent_write_before_the_write_transaction(server_update(todo, CANCELED)):
        response = admin_client().put(
            todo_url(todo), DONE, format="json", HTTP_IF_MATCH=quote_etag(todo.etag)
        )
    assert response.status_code == 412, response.content
    stored = Resource.objects.get(pk=todo.pk)
    assert stored.content == CANCELED
    assert stored.etag != todo.etag


@pytest.mark.django_db
def test_a_delete_whose_if_match_a_concurrent_write_made_stale_is_412():
    todo = a_todo()
    with concurrent_write_before_the_write_transaction(server_update(todo, CANCELED)):
        response = admin_client().delete(
            todo_url(todo), HTTP_IF_MATCH=quote_etag(todo.etag)
        )
    assert response.status_code == 412, response.content
    assert Resource.objects.get(pk=todo.pk).content == CANCELED


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["put", "delete"])
def test_a_write_is_authorized_against_the_stored_context(method):
    todo = a_todo()
    client = client_with(
        {m: [TODO] for m in ("http.GET", "http.PUT", "http.DELETE")}, ""
    )
    # The other writer moves the ToDo to a context this user has no grant in.
    moved = dict(IN_PROGRESS, rbac_context="other")
    with concurrent_write_before_the_write_transaction(server_update(todo, moved)):
        response = getattr(client, method)(
            todo_url(todo),
            *([IN_PROGRESS] if method == "put" else []),
            format="json",
            HTTP_IF_UNMODIFIED_SINCE=later(),
        )
    assert response.status_code == 404, response.content
    assert Resource.objects.get(pk=todo.pk).content == moved


class RecordingRouter:
    """Routes nothing; records the reads of scrud_django models that the
    router decides: queries, and loads of related objects (a type's schema,
    with the type as the instance hint), which a router ignoring hints, as
    Django's documented primary/replica example does, would send to the
    replica too."""

    reads = []

    def db_for_read(self, model, **hints):
        if model._meta.app_label == "scrud_django":
            self.reads.append(model.__name__)
        return None


@contextmanager
def routed_reads():
    RecordingRouter.reads = []
    with override_settings(
        DATABASE_ROUTERS=["tests.test_atomic_writes.RecordingRouter"]
    ):
        yield RecordingRouter.reads


@pytest.mark.django_db
def test_api_writes_read_no_resource_or_type_through_the_read_router():
    todo = a_todo()
    other = a_todo()
    client = admin_client()
    list_etag = client.get(reverse("todos-list"))["ETag"]
    with routed_reads() as reads:
        # A conditional create locks the type row it reads: read on the
        # type's write database, not a replica's (maybe older) revision. Every
        # create reads there the schema it validates the body against.
        for headers in ({"HTTP_IF_MATCH": list_etag}, {"HTTP_IF_MATCH": "*"}, {}):
            response = client.post(
                reverse("todos-list"), IN_PROGRESS, format="json", **headers
            )
            assert response.status_code == 201, (headers, response.content)
        response = client.put(
            todo_url(todo), DONE, format="json", HTTP_IF_MATCH=quote_etag(todo.etag)
        )
        assert response.status_code == 200, response.content
        response = client.delete(todo_url(other), HTTP_IF_MATCH=quote_etag(other.etag))
        assert response.status_code == 204, response.content
    assert [r for r in reads if r in ("Resource", "ResourceType")] == []


@pytest.mark.django_db
def test_server_side_writes_read_no_resource_or_type_through_the_read_router():
    todo = a_todo()
    with routed_reads() as reads:
        ResourceRegistration.register(dict(IN_PROGRESS), "todos")
        ResourceRegistration.update(
            content=dict(DONE), register_type="todos", slug=str(todo.pk)
        )
        ResourceRegistration.delete(register_type="todos", slug=str(todo.pk))
    assert [r for r in reads if r in ("Resource", "ResourceType")] == []


@pytest.mark.django_db
def test_registering_types_reads_no_type_through_the_read_router():
    # A process start (tests.resource_types, as conftest registers them)
    # compares each stored type's schema and context with its files.
    from . import resource_types

    a_todo()
    with routed_reads() as reads:
        resource_types.json_resource_types()
    assert [r for r in reads if r in ("Resource", "ResourceType")] == []


@pytest.mark.django_db
def test_a_refused_transitions_policy_link_reads_no_type_through_the_router():
    # The 400 links the stored policy by its type's route: the type comes
    # with the policy, read on the write database.
    todo = a_todo()
    workflow_policy_resource_type()
    ResourceRegistration.register(dict(STRICT_POLICY), "workflow-policies")
    with routed_reads() as reads:
        response = admin_client().put(
            todo_url(todo),
            CANCELED,
            format="json",
            HTTP_IF_MATCH=quote_etag(todo.etag),
        )
    assert response.status_code == 400, response.content
    assert response.json()["policy"], response.json()
    assert [r for r in reads if r in ("Resource", "ResourceType")] == []


class LaggingReplicaRouter:
    """Reads resources and types on "other", a replica lagging behind
    (`lagging_replica`), ignoring hints, as Django's documented
    primary/replica example does; writes on the default database."""

    def db_for_read(self, model, **hints):
        if model in (Resource, ResourceType):
            return "other"
        return None

    def db_for_write(self, model, **hints):
        return DEFAULT_DB_ALIAS

    def allow_relation(self, *args, **kwargs):
        return True


def copied(row, **changes):
    row = copy.copy(row)
    for name, value in changes.items():
        setattr(row, name, value)
    return row


def lagging_replica(resource_type, schema=None):
    """Copy `resource_type`, its context and its schema (with their own
    types) to "other", the schema's content as `schema` (an older one, with
    another ETag and modification time, as an older version has: the
    compiled validators are cached by them), else as stored."""
    documents = [resource_type.schema, resource_type.context]
    types = {doc.resource_type_id: doc.resource_type for doc in documents}
    ResourceType.objects.using("other").bulk_create(
        [
            copied(t, schema_id=None, context_id=None, indexing_policy_id=None)
            for t in types.values()
        ]
    )
    stored_schema = resource_type.schema
    if schema is not None:
        stored_schema = copied(
            stored_schema,
            content=schema,
            etag=uuid4().hex,
            modified_at=stored_schema.modified_at - timedelta(days=1),
        )
    Resource.objects.using("other").bulk_create(
        [stored_schema, copied(resource_type.context)]
    )
    ResourceType.objects.using("other").bulk_create(
        [copied(resource_type, indexing_policy_id=None)]
    )


LAGGING = override_settings(
    DATABASE_ROUTERS=["tests.test_atomic_writes.LaggingReplicaRouter"]
)


@pytest.mark.django_db(databases=["default", "other"])
@pytest.mark.parametrize(
    "method, headers",
    [("post", {}), ("post", {"HTTP_IF_MATCH": "*"}), ("put", None)],
    ids=["post", "post-if-match-any", "put"],
)
def test_a_write_is_validated_against_the_schema_not_a_lagging_replicas(
    method, headers
):
    # The replica still has an older, permissive schema: the body the
    # current one refuses is a 400 all the same, and nothing is stored.
    todo = a_todo()
    lagging_replica(todo.resource_type, schema={"type": "object"})
    refused = dict(IN_PROGRESS, priority="bogus")
    client = admin_client()
    with LAGGING:
        if method == "put":
            response = client.put(
                todo_url(todo),
                refused,
                format="json",
                HTTP_IF_MATCH=quote_etag(todo.etag),
            )
        else:
            response = client.post(
                reverse("todos-list"), refused, format="json", **headers
            )
    assert response.status_code == 400, response.content
    assert not Resource.objects.filter(content__priority="bogus").exists()


@pytest.mark.django_db(databases=["default", "other"])
def test_registering_types_compares_the_files_with_the_write_database():
    # The replica has the todos type and its schema as the files have it,
    # and no partner-profiles type yet; the primary's todos schema was
    # edited since. A process start puts the files' schema back, and stores
    # no second partner-profiles type.
    from . import resource_types

    todo = a_todo()
    todos_type = todo.resource_type
    from_file = dict(todos_type.schema.content)
    lagging_replica(todos_type)
    ResourceRegistration.update(
        dict(from_file, title="edited"), "json-schema", todos_type.schema.pk
    )
    with LAGGING:
        resource_types.json_resource_types()
    stored = ResourceType.objects.get(slug="todos").schema
    assert stored.content == from_file
    slugs = list(ResourceType.objects.values_list("slug", flat=True))
    assert len(slugs) == len(set(slugs)), sorted(slugs)


@contextmanager
def recorded_write_order():
    """Record, in order, the policy type lock (``"lock"``), the queries of
    the ``Resource`` table (``"select"`` and ``"update"``/``"delete"``) and
    whether each ran inside a transaction the code under test opened on the
    write database."""
    connection = connections[write_db()]
    quote = connection.ops.quote_name
    depth = len(connection.atomic_blocks)
    events = []
    original_lock = registration.lock_workflow_policy_type

    def lock(using):
        events.append(("lock", len(connections[using].atomic_blocks) > depth))
        return original_lock(using)

    def record(execute, sql, params, many, context):
        if (
            quote("scrud_django_resource") in sql
            and quote("scrud_django_resourcetype") not in sql
        ):
            kind = sql.split(None, 1)[0].lower()
            events.append((kind, len(connection.atomic_blocks) > depth))
        return execute(sql, params, many, context)

    with (
        mock.patch.object(registration, "lock_workflow_policy_type", lock),
        connection.execute_wrapper(record),
    ):
        yield events


def a_policy():
    reverse("todos-list")
    workflow_policy_resource_type()
    return ResourceRegistration.register(
        dict(PERMISSIVE_POLICY, rbac_context="p"), "workflow-policies"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["put", "delete"])
def test_a_policy_write_locks_the_type_before_it_reads_the_row(method):
    stored = a_policy()
    client = admin_client()
    url = reverse("workflow-policies-detail", args=[stored.pk])
    with recorded_write_order() as events:
        response = getattr(client, method)(
            url,
            *(
                [dict(PERMISSIVE_POLICY, rbac_context="p", states={"x": "true"})]
                if method == "put"
                else []
            ),
            format="json",
            HTTP_IF_MATCH=quote_etag(stored.etag),
        )
    assert response.status_code in (200, 204), response.content
    assert events[0] == ("lock", True), events
    assert events[1] == ("select", True), events
    # Every read and write of the row ran inside the write's transaction.
    assert all(inside for _, inside in events), events


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["put", "delete"])
def test_a_write_reads_the_row_inside_its_transaction(method):
    todo = a_todo()
    client = admin_client()
    with recorded_write_order() as events:
        response = getattr(client, method)(
            todo_url(todo),
            *([DONE] if method == "put" else []),
            format="json",
            HTTP_IF_MATCH=quote_etag(todo.etag),
        )
    assert response.status_code in (200, 204), response.content
    assert events and events[0] == ("select", True), events
    assert all(inside for _, inside in events), events
    assert ("lock", True) not in events


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["put", "delete"])
def test_the_locked_lookup_selects_for_update_on_the_write_database(method):
    from scrud_django.mixins.view_mixins import ResourceViewMixin

    todo = a_todo()
    seen = []
    original = ResourceViewMixin.get_queryset

    def get_queryset(self):
        queryset = original(self)
        seen.append((queryset.db, queryset.query.select_for_update))
        return queryset

    with mock.patch.object(ResourceViewMixin, "get_queryset", get_queryset):
        response = getattr(admin_client(), method)(
            todo_url(todo),
            *([DONE] if method == "put" else []),
            format="json",
            HTTP_IF_MATCH=quote_etag(todo.etag),
        )
    assert response.status_code in (200, 204), response.content
    assert seen and all(entry == (write_db(), True) for entry in seen), seen


@pytest.mark.django_db(databases=["default", "other"])
@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "stale"])
def test_a_locked_lookup_reads_the_write_database_whatever_get_queryset_uses(
    fresh,
):
    # A get_queryset() of a subclass selecting another database (using(), a
    # replica: "other" has no resource) does not move a locked write's
    # lookup there: the row is read and locked on the write database, and
    # the If-Match is evaluated against it.
    from scrud_django.mixins.view_mixins import ResourceViewMixin

    todo = a_todo()
    original = ResourceViewMixin.get_queryset

    def get_queryset(self):
        return original(self).using("other")

    etag = quote_etag(todo.etag) if fresh else '"stale"'
    with mock.patch.object(ResourceViewMixin, "get_queryset", get_queryset):
        response = admin_client().put(
            todo_url(todo), DONE, format="json", HTTP_IF_MATCH=etag
        )
    assert response.status_code == (200 if fresh else 412), response.content
    stored = Resource.objects.get(pk=todo.pk)
    assert stored.content == (DONE if fresh else IN_PROGRESS)
    assert not Resource.objects.using("other").exists()


@pytest.mark.django_db
def test_a_refused_write_rolls_back_the_requests_transaction():
    todo = a_todo()
    before = ResourceType.objects.get(pk=todo.resource_type_id).etag
    # A body in no workflow state is refused with 400 by the workflow check.
    response = admin_client().put(
        todo_url(todo),
        {"title": "t", "status": "nonsense"},
        format="json",
        HTTP_IF_MATCH=quote_etag(todo.etag),
    )
    assert response.status_code == 400, response.content
    assert Resource.objects.get(pk=todo.pk).content == IN_PROGRESS
    assert ResourceType.objects.get(pk=todo.resource_type_id).etag == before
