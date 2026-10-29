"""Duplicate WorkflowPolicy resources (a uniqueness race, or data stored
before 0.12.0): the write that would make one is re-checked under a lock,
writes governed by existing duplicates fail closed, and a system check
reports them and policies stored with a list ``rbac_context``."""

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
    assert "workflow_policy" in response.json()["errors"], response.json()
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
