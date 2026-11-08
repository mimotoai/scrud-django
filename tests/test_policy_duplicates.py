"""Duplicate WorkflowPolicy resources (a uniqueness race, or data stored
before 0.12.0): the write that would make one is re-checked under a lock,
writes governed by existing duplicates fail closed, and a system check
reports them and policies stored with a list ``rbac_context``."""

from contextlib import contextmanager
from unittest import mock

import pytest
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse
from rest_framework.test import APIClient

from scrud_django import checks as scrud_checks
from scrud_django.mixins import view_mixins
from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)
from scrud_django.validators import WorkflowValidator

from .factories import UserFactory

TODO = "tests://ToDo"
WORKFLOW_POLICY = "http://api.openteams.com/json-ld/WorkflowPolicy"
PERMISSIVE_POLICY = {
    "resource_type": TODO,
    "initial_states": {"any": "true"},
    "states": {},
    "transitions": {},
}


def store_policy(content):
    reverse("todos-list")
    workflow_policy_resource_type()
    return ResourceRegistration.register(content, "workflow-policies")


def admin_client():
    client = APIClient()
    client.force_authenticate(UserFactory(is_staff=True, is_superuser=True))
    return client


def policies():
    return list(
        Resource.objects.filter(resource_type__type_uri=WORKFLOW_POLICY).order_by("pk")
    )


@pytest.mark.django_db
def test_a_racing_duplicate_is_refused_under_the_lock():
    first = store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    lock = mock.Mock(wraps=view_mixins.lock_workflow_policy_type)
    # As if a concurrent create committed after this request's validation.
    with (
        mock.patch.object(WorkflowValidator, "validate_policy"),
        mock.patch.object(view_mixins, "lock_workflow_policy_type", lock),
    ):
        response = admin_client().post(
            reverse("workflow-policies-list"),
            dict(PERMISSIVE_POLICY, rbac_context="p"),
            format="json",
        )
    assert response.status_code == 400, response.content
    body = response.json()
    assert (
        body["type"] == "https://api.openteams.com/http-problem/workflow-policy-invalid"
    ), body
    assert "already exists in context 'p'" in body["detail"], body
    assert policies() == [first]
    lock.assert_called_once_with("default")


@pytest.mark.django_db
def test_a_racing_move_into_an_occupied_context_is_refused_under_the_lock():
    from django.utils.http import quote_etag

    store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    other = store_policy(dict(PERMISSIVE_POLICY, rbac_context="q"))
    with mock.patch.object(WorkflowValidator, "validate_policy"):
        response = admin_client().put(
            reverse("workflow-policies-detail", args=[other.pk]),
            dict(PERMISSIVE_POLICY, rbac_context="p"),
            format="json",
            HTTP_IF_MATCH=quote_etag(other.etag),
        )
    assert response.status_code == 400, response.content
    assert Resource.objects.get(pk=other.pk).content == other.content


@pytest.mark.django_db
def test_policy_writes_take_the_lock():
    lock = mock.Mock(wraps=view_mixins.lock_workflow_policy_type)
    reverse("todos-list")
    with mock.patch.object(view_mixins, "lock_workflow_policy_type", lock):
        response = admin_client().post(
            reverse("workflow-policies-list"),
            dict(PERMISSIVE_POLICY, rbac_context="p"),
            format="json",
        )
        assert response.status_code == 201, response.content
        response = admin_client().post(
            reverse("todos-list"), {"title": "t", "rbac_context": "p"}, format="json"
        )
        assert response.status_code == 201, response.content
    lock.assert_called_once_with("default")


@pytest.mark.django_db
def test_a_write_under_duplicate_policies_is_refused():
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    before = Resource.objects.count()
    with pytest.raises(ImproperlyConfigured, match="2 WorkflowPolicy"):
        admin_client().post(
            reverse("todos-list"), {"title": "t", "rbac_context": "p"}, format="json"
        )
    assert Resource.objects.count() == before
    # Other contexts are unaffected.
    response = admin_client().post(
        reverse("todos-list"),
        {"title": "t", "status": "not started", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_system_check_reports_duplicates_and_list_contexts():
    a = store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    b = store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    shared = store_policy(dict(PERMISSIVE_POLICY, rbac_context=["x", "y"]))
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="z"))
    found = scrud_checks.check_stored_workflow_policies(None, databases=["default"])
    assert [(m.id, m.level) for m in found] == [
        ("scrud_django.E002", checks.ERROR),
        ("scrud_django.W001", checks.WARNING),
    ]
    assert found[0].msg == (
        f"2 WorkflowPolicy resources apply to {TODO!r} in context 'p' "
        f"(pks {[a.pk, b.pk]}); there must be at most one."
    )
    assert found[1].msg == (
        f"WorkflowPolicy pk {shared.pk} has a list rbac_context ['x', 'y']; "
        "a policy has one context."
    )
    # Registered as a database check: `check --database` and `migrate` run it.
    assert found[0] in checks.run_checks(databases=["default"])
    assert found[0] not in checks.run_checks()


@pytest.mark.django_db
def test_system_check_counts_a_repeated_context_of_one_policy_once():
    # One policy whose list rbac_context repeats a context is one policy
    # there (stored_policies_for counts it once, and writes work): W001 for
    # the list, no E002. With a second policy in that context, E002 names
    # each policy once.
    repeated = store_policy(dict(PERMISSIVE_POLICY, rbac_context=["a", "a"]))
    found = scrud_checks.check_stored_workflow_policies(None, databases=["default"])
    assert [(m.id, m.level) for m in found] == [("scrud_django.W001", checks.WARNING)]
    other = store_policy(dict(PERMISSIVE_POLICY, rbac_context="a"))
    found = scrud_checks.check_stored_workflow_policies(None, databases=["default"])
    assert [(m.id, m.level) for m in found] == [
        ("scrud_django.E002", checks.ERROR),
        ("scrud_django.W001", checks.WARNING),
    ]
    assert found[0].msg == (
        f"2 WorkflowPolicy resources apply to {TODO!r} in context 'a' "
        f"(pks {[repeated.pk, other.pk]}); there must be at most one."
    )


@pytest.mark.django_db
def test_system_check_passes_without_duplicates():
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="q"))
    assert (
        scrud_checks.check_stored_workflow_policies(None, databases=["default"]) == []
    )
    assert scrud_checks.check_stored_workflow_policies(None) == []


# Lock order: every WorkflowPolicy write takes the type lock before it writes
# the Resource row (PUT and DELETE in different orders deadlock on PostgreSQL).


@pytest.fixture
def write_order():
    from django.db.models.signals import pre_delete, pre_save

    from scrud_django import registration

    events = []

    def record(event):
        def receiver(sender, instance, **kwargs):
            if instance.resource_type.type_uri == WORKFLOW_POLICY:
                events.append(event)

        return receiver

    on_save, on_delete = record("save"), record("delete")
    pre_save.connect(on_save, sender=Resource)
    pre_delete.connect(on_delete, sender=Resource)

    def lock(using):
        events.append("lock")

    with (
        mock.patch.object(registration, "lock_workflow_policy_type", lock),
        mock.patch.object(view_mixins, "lock_workflow_policy_type", lock),
    ):
        yield events
    pre_save.disconnect(on_save, sender=Resource)
    pre_delete.disconnect(on_delete, sender=Resource)


@pytest.mark.django_db
def test_api_delete_of_a_policy_locks_before_deleting(write_order):
    from django.utils.http import quote_etag

    stored = store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    del write_order[:]
    response = admin_client().delete(
        reverse("workflow-policies-detail", args=[stored.pk]),
        HTTP_IF_MATCH=quote_etag(stored.etag),
    )
    assert response.status_code == 204, response.content
    assert write_order == ["lock", "delete"]


@pytest.mark.django_db
def test_api_update_of_a_policy_locks_before_saving(write_order):
    from django.utils.http import quote_etag

    stored = store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    del write_order[:]
    response = admin_client().put(
        reverse("workflow-policies-detail", args=[stored.pk]),
        dict(PERMISSIVE_POLICY, rbac_context="p", initial_states={"x": "true"}),
        format="json",
        HTTP_IF_MATCH=quote_etag(stored.etag),
    )
    assert response.status_code == 200, response.content
    assert write_order[0] == "lock" and "save" in write_order, write_order
    assert write_order.index("lock") < write_order.index("save")


@pytest.mark.django_db
def test_server_side_policy_writes_lock_before_writing(write_order):
    stored = store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    assert write_order == ["lock", "save"]
    del write_order[:]
    ResourceRegistration.update(
        content=dict(PERMISSIVE_POLICY, rbac_context="q"),
        register_type="workflow-policies",
        slug=str(stored.pk),
    )
    assert write_order == ["lock", "save"]
    del write_order[:]
    ResourceRegistration.delete(register_type="workflow-policies", slug=str(stored.pk))
    assert write_order == ["lock", "delete"]


@pytest.mark.django_db
def test_other_writes_take_no_policy_lock(write_order):
    reverse("todos-list")
    ResourceRegistration.register({"name": "n"}, "partner-profiles")
    assert write_order == []


@pytest.mark.django_db
@pytest.mark.parametrize("resource_type", [[TODO], {"@id": TODO}, None, 5], ids=repr)
def test_system_check_reports_a_non_string_resource_type(resource_type):
    from django.utils.timezone import now

    from scrud_django.models import ResourceType

    workflow_policy_resource_type()
    odd = Resource.objects.create(
        resource_type=ResourceType.objects.get(type_uri=WORKFLOW_POLICY),
        content=dict(PERMISSIVE_POLICY, resource_type=resource_type),
        etag="x",
        modified_at=now(),
    )
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    found = scrud_checks.check_stored_workflow_policies(None, databases=["default"])
    assert [(m.id, m.level) for m in found] == [("scrud_django.E003", checks.ERROR)]
    assert found[0].msg == (
        f"WorkflowPolicy pk {odd.pk} has resource_type {resource_type!r}; it must "
        "be a type URI (a string)."
    )


# Policies stored past the API's checks (server-side, or before 0.12.0), with
# the reason each fails WorkflowPolicy's checks.
STORED_INVALID_POLICIES = [
    pytest.param(
        dict(PERMISSIVE_POLICY, states={"done": "status == "}),
        "Invalid workflow state condition: ",
        id="invalid-condition",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, transitions={"go": {"from": "any", "to": "nowhere"}}),
        "Invalid workflow transition 'go': 'nowhere' is not a defined state.",
        id="transition-to-undefined-state",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, transitions={"__mine": {"from": "any", "to": "any"}}),
        "Invalid workflow transition '__mine': a name must be a string not "
        "starting with '__'",
        id="reserved-transition-name",
    ),
    pytest.param(
        dict(
            PERMISSIVE_POLICY,
            transitions={
                "go": {"from": "any", "to": "any"},
                "again": {"from": ["any"], "to": "any"},
            },
        ),
        "Invalid workflow transitions 'go' and 'again': both lead from 'any' to "
        "'any'; a pair of states may have one transition.",
        id="duplicate-pair",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, restrict_update="any"),
        "Invalid workflow restrict_update: it must be a list of defined state "
        "names, not 'any'.",
        id="restrict-update-string",
    ),
    # Shapes the WorkflowPolicy schema refuses, so only server-side code (or
    # an earlier version) stores them.
    pytest.param(
        dict(PERMISSIVE_POLICY, initial_states=["x"]),
        "Invalid workflow initial_states: it must be an object of state names "
        "to CEL conditions, not ['x'].",
        id="initial-states-list",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, initial_states=None),
        "Invalid workflow initial_states: it must be an object of state names "
        "to CEL conditions, not None.",
        id="initial-states-null",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, states="nope"),
        "Invalid workflow states: it must be an object of state names to CEL "
        "conditions, not 'nope'.",
        id="states-string",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, transitions=["t"]),
        "Invalid workflow transitions: it must be an object of transition names "
        "to transitions, not ['t'].",
        id="transitions-list",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, initial_states={"a": 3}),
        "Invalid workflow state condition for 'a': it must be a CEL expression "
        "(a string), not 3.",
        id="condition-number",
    ),
    pytest.param(
        dict(PERMISSIVE_POLICY, states={"a": None}),
        "Invalid workflow state condition for 'a': it must be a CEL expression "
        "(a string), not None.",
        id="condition-null",
    ),
]


@pytest.mark.django_db
@pytest.mark.parametrize("content,reason", STORED_INVALID_POLICIES)
def test_system_check_reports_an_invalid_stored_policy(content, reason):
    invalid = store_policy(dict(content, rbac_context="p"))
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="q"))
    found = scrud_checks.check_stored_workflow_policies(None, databases=["default"])
    assert [(m.id, m.level) for m in found] == [("scrud_django.E005", checks.ERROR)]
    assert found[0].msg.startswith(
        f"WorkflowPolicy pk {invalid.pk} for {TODO!r} in context 'p' does not "
        f"pass WorkflowPolicy's checks: {reason}"
    ), found[0].msg
    # The hint names the ways to fix it on the migrated database the check
    # reports on (it reports nothing before migrate: test_upgrade_checks.py).
    assert found[0].hint == (
        "Fix it (a PUT through the API checks it) or delete it (through the "
        "API or ResourceRegistration.delete) on this migrated database: until "
        "then every write of its type in its contexts fails with "
        "ImproperlyConfigured (a 500)."
    )
    # A database check: `check --database` and `migrate` run it.
    assert found[0] in checks.run_checks(databases=["default"])
    assert found[0] not in checks.run_checks()


@pytest.mark.django_db
def test_system_check_reports_a_policy_it_cannot_check(monkeypatch):
    # Whatever compiling a stored policy raises, the check reports it (E005)
    # rather than failing `manage.py check --database` and `migrate`.
    from scrud_django import workflow

    broken = store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))

    def boom(self, *args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(workflow.WorkflowPolicy, "__init__", boom)
    found = scrud_checks.check_stored_workflow_policies(None, databases=["default"])
    assert [(m.id, m.level) for m in found] == [("scrud_django.E005", checks.ERROR)]
    assert found[0].msg == (
        f"WorkflowPolicy pk {broken.pk} for {TODO!r} in context 'p' could not be "
        "checked: RuntimeError: unexpected"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("content,reason", STORED_INVALID_POLICIES)
def test_a_write_under_an_invalid_stored_policy_names_it(content, reason):
    # The writes it governs still fail (500), with an ImproperlyConfigured
    # that names the policy and its check, not an AttributeError.
    from django.core.exceptions import ImproperlyConfigured

    invalid = store_policy(dict(content, rbac_context="p"))
    reverse("todos-list")
    with pytest.raises(ImproperlyConfigured) as excinfo:
        admin_client().post(
            reverse("todos-list"), {"title": "t", "rbac_context": "p"}, format="json"
        )
    assert str(excinfo.value).startswith(
        f"Stored WorkflowPolicy pk {invalid.pk} is invalid (scrud_django.E005): "
        f"{reason}"
    ), excinfo.value


class ReadReplicaRouter:
    """Reads go to a replica (here an alias that does not exist), writes to
    "default"."""

    def db_for_read(self, model, **hints):
        return "replica"

    def db_for_write(self, model, **hints):
        return "default"


@pytest.mark.django_db
def test_policy_lookup_reads_the_write_database():
    from django.test import override_settings

    from scrud_django.workflow import stored_policies_for

    stored = store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    with override_settings(
        DATABASE_ROUTERS=["tests.test_policy_duplicates.ReadReplicaRouter"]
    ):
        assert stored_policies_for(TODO, "p") == [stored]


# The lookup selects a type's policies by their ResourceContext rows, and
# also by content, so that a policy whose rows are stale (written without
# Resource.save()) is not left out: it still governs its contexts' writes.

REFUSING_POLICY = {
    "resource_type": TODO,
    "initial_states": {"never": "false"},
    "states": {},
    "transitions": {},
}
ABSENT = object()


def post_todo(context):
    """An admin's create of a ToDo in `context` (``ABSENT``: none named),
    valid under the default ToDo policy."""
    body = {"title": "t", "status": "not started"}
    if context is not ABSENT:
        body["rbac_context"] = context
    return admin_client().post(reverse("todos-list"), body, format="json")


def row_contexts(resource):
    return set(resource.rbac_context_rows.values_list("rbac_context", flat=True))


@pytest.mark.django_db
def test_policy_lookup_reads_its_contexts_in_one_query(django_assert_num_queries):
    from scrud_django.workflow import stored_policies_in

    p = store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    q = store_policy(dict(PERMISSIVE_POLICY, rbac_context="q"))
    store_policy(dict(PERMISSIVE_POLICY, rbac_context="r"))
    with django_assert_num_queries(1):
        found = stored_policies_in(TODO, ["p", "q", "s"])
    assert found == {"p": [p], "q": [q], "s": []}
    assert stored_policies_in(TODO, []) == {}


@pytest.mark.django_db
def test_policy_lookup_compares_contexts_exactly():
    from scrud_django.workflow import stored_policies_for

    stored = store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    assert stored_policies_for(TODO, "p") == [stored]
    # MySQL's and MariaDB's collations ignore case and trailing spaces.
    assert stored_policies_for(TODO, "P") == []
    assert stored_policies_for(TODO, "p ") == []


@pytest.mark.django_db
def test_a_policy_without_context_rows_still_governs_its_context():
    from django.utils.timezone import now

    from scrud_django.workflow import stored_policies_for

    store_policy(dict(PERMISSIVE_POLICY, rbac_context="other"))
    policy_type = workflow_policy_resource_type()
    # bulk_create() bypasses Resource.save(): no ResourceContext row.
    Resource.objects.bulk_create(
        [
            Resource(
                resource_type=policy_type,
                content=dict(REFUSING_POLICY, rbac_context="p"),
                etag="0" * 32,
                modified_at=now(),
            )
        ]
    )
    bulk = Resource.objects.get(resource_type=policy_type, content__rbac_context="p")
    assert row_contexts(bulk) == set()
    assert stored_policies_for(TODO, "p") == [bulk]
    assert stored_policies_for(TODO, "q") == []
    response = post_todo("p")
    assert response.status_code == 400, response.content
    response = post_todo("q")
    assert response.status_code == 201, response.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    "moved_to, context",
    [
        ("q", "q"),
        (["r", "q"], "q"),
        (ABSENT, ""),
        (None, ""),
    ],
    ids=["string", "list", "absent", "null"],
)
def test_a_policy_moved_past_save_governs_its_new_context(moved_to, context):
    from scrud_django.workflow import stored_policies_for, workflow_policy_for

    moved = store_policy(dict(REFUSING_POLICY, rbac_context="p"))
    content = dict(REFUSING_POLICY)
    if moved_to is not ABSENT:
        content["rbac_context"] = moved_to
    # QuerySet.update() bypasses Resource.save(): the rows still say "p".
    Resource.objects.filter(pk=moved.pk).update(content=content)
    moved.refresh_from_db()
    assert row_contexts(moved) == {"p"}
    assert stored_policies_for(TODO, context) == [moved]
    assert stored_policies_for(TODO, "p") == []
    response = post_todo(context or ABSENT)
    assert response.status_code == 400, response.content
    response = post_todo("p")
    assert response.status_code == 201, response.content
    # With a policy saved there, it is one of two: neither applies.
    store_policy(dict(PERMISSIVE_POLICY, rbac_context=context))
    with pytest.raises(ImproperlyConfigured, match="2 WorkflowPolicy"):
        workflow_policy_for(TODO, context)


class OtherWriteRouter:
    """Resource writes go to the "other" alias; everything else to default."""

    def db_for_write(self, model, **hints):
        return "other" if model is Resource else None

    def allow_relation(self, obj1, obj2, **hints):
        return True


class LockTaken(Exception):
    pass


@contextmanager
def lock_on_other():
    """Route Resource writes to "other" and stop at the policy lock, recording
    the alias it got and whether the code under test had opened a transaction
    (an atomic block) on that alias. Enter it after the test's setup writes,
    which go to "default"."""
    from django.db import connections
    from django.test import override_settings

    from scrud_django import registration

    seen = []
    # The test itself runs in a transaction on each database: what counts is
    # an atomic block the code under test opened on the alias, one deeper.
    depth = {alias: len(connections[alias].atomic_blocks) for alias in connections}

    def lock(using):
        seen.append((using, len(connections[using].atomic_blocks) > depth[using]))
        raise LockTaken()

    with (
        override_settings(
            DATABASE_ROUTERS=["tests.test_policy_duplicates.OtherWriteRouter"]
        ),
        mock.patch.object(registration, "lock_workflow_policy_type", lock),
    ):
        yield seen


@pytest.mark.django_db(databases=["default", "other"])
def test_server_side_policy_writes_lock_inside_the_routed_transaction():
    stored = store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    policy_type = stored.resource_type
    with lock_on_other() as seen:
        with pytest.raises(LockTaken):
            ResourceRegistration.register_instance(
                Resource(
                    content=dict(PERMISSIVE_POLICY, rbac_context="q"),
                    resource_type=policy_type,
                )
            )
        with pytest.raises(LockTaken):
            ResourceRegistration.update(
                content=dict(PERMISSIVE_POLICY, rbac_context="q"),
                register_type="workflow-policies",
                slug=str(stored.pk),
            )
        with pytest.raises(LockTaken):
            ResourceRegistration.delete(
                register_type="workflow-policies", slug=str(stored.pk)
            )
    assert seen == [("other", True)] * 3


@pytest.mark.django_db(databases=["default", "other"])
def test_api_delete_of_a_policy_locks_inside_the_routed_transaction():
    from django.utils.http import quote_etag

    stored = store_policy(dict(PERMISSIVE_POLICY, rbac_context="p"))
    client = admin_client()
    with lock_on_other() as seen, pytest.raises(LockTaken):
        client.delete(
            reverse("workflow-policies-detail", args=[stored.pk]),
            HTTP_IF_MATCH=quote_etag(stored.etag),
        )
    assert seen == [("other", True)]
    assert Resource.objects.filter(pk=stored.pk).exists()
