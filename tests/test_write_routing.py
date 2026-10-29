"""Write routing: `ResourceRegistration` writes a resource, its context rows
and its type's row on one database, the alias of its transaction; a system
check (``scrud_django.E004``) refuses a router that sends ``Resource``,
``ResourceType`` and ``ResourceContext`` writes to different aliases."""

from contextlib import contextmanager
from unittest import mock

import pytest
from django.core import checks
from django.db.models.signals import pre_save
from django.test import override_settings
from django.urls import reverse
from django.utils.http import quote_etag
from rest_framework.test import APIClient

from scrud_django import checks as scrud_checks
from scrud_django import registration
from scrud_django.models import Resource, ResourceContext, ResourceType
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)

from .factories import UserFactory

TODO = "tests://ToDo"
NOT_STARTED = {"title": "t", "status": "not started"}
PERMISSIVE_POLICY = {
    "resource_type": TODO,
    "initial_states": {"any": "true"},
    "states": {},
    "transitions": {},
}


class TypeElsewhereRouter:
    """``ResourceType`` writes go to "other"; everything else to default."""

    def db_for_write(self, model, **hints):
        return "other" if model is ResourceType else None

    def allow_relation(self, obj1, obj2, **hints):
        return True


class TypeSavesElsewhereRouter:
    """A ``ResourceType`` instance (an ``instance`` hint, as ``save()`` gives)
    is written to "other"; everything else, and a write asked without a hint,
    to default."""

    def db_for_write(self, model, **hints):
        if model is ResourceType and hints.get("instance") is not None:
            return "other"
        return None

    def allow_relation(self, obj1, obj2, **hints):
        return True


class ContextsElsewhereRouter:
    """``ResourceContext`` writes go to "other"; everything else to default."""

    def db_for_write(self, model, **hints):
        return "other" if model is ResourceContext else None


class OneAliasRouter:
    """Every scrud_django write goes to "other"."""

    def db_for_write(self, model, **hints):
        return "other" if model._meta.app_label == "scrud_django" else None


class InstanceHintRouter:
    """Routes a write by its instance hint: an instance with a
    ``scrud_test_alias`` attribute goes there."""

    def db_for_write(self, model, **hints):
        return getattr(hints.get("instance"), "scrud_test_alias", None)

    def allow_relation(self, obj1, obj2, **hints):
        return True


def routers(*names):
    return override_settings(
        DATABASE_ROUTERS=[f"tests.test_write_routing.{name}" for name in names]
    )


def admin_client():
    client = APIClient()
    client.force_authenticate(UserFactory(is_staff=True, is_superuser=True))
    return client


@contextmanager
def type_saves():
    """The database of each ``ResourceType`` save."""
    seen = []

    def record(sender, instance, using, **kwargs):
        seen.append(using)

    pre_save.connect(record, sender=ResourceType)
    try:
        yield seen
    finally:
        pre_save.disconnect(record, sender=ResourceType)


@pytest.mark.django_db(databases=["default", "other"])
def test_server_side_writes_save_the_type_on_the_transactions_database():
    reverse("todos-list")
    todo = ResourceRegistration.register(dict(NOT_STARTED), "todos")
    with routers("TypeSavesElsewhereRouter"), type_saves() as seen:
        ResourceRegistration.register(dict(NOT_STARTED), "todos")
        ResourceRegistration.update(dict(NOT_STARTED), "todos", str(todo.pk))
        ResourceRegistration.delete("todos", str(todo.pk))
    assert seen == ["default"] * 3


@pytest.mark.django_db(databases=["default", "other"])
def test_api_writes_save_the_type_on_the_transactions_database():
    reverse("todos-list")
    todo = ResourceRegistration.register(dict(NOT_STARTED), "todos")
    client = admin_client()
    url = reverse("todos-detail", args=[todo.pk])
    with routers("TypeSavesElsewhereRouter"), type_saves() as seen:
        response = client.post(reverse("todos-list"), NOT_STARTED, format="json")
        assert response.status_code == 201, response.content
        response = client.put(
            url, NOT_STARTED, format="json", HTTP_IF_MATCH=quote_etag(todo.etag)
        )
        assert response.status_code == 200, response.content
        response = client.delete(url, HTTP_IF_MATCH=response["ETag"])
        assert response.status_code == 204, response.content
    assert seen == ["default"] * 3


class LockTaken(Exception):
    pass


@pytest.mark.django_db(databases=["default", "other"])
def test_register_instance_opens_its_transaction_where_the_instance_is_routed():
    from django.db import connections

    reverse("todos-list")
    workflow_policy_resource_type()
    policy_type = ResourceType.objects.get(slug="workflow-policies")
    depth = len(connections["other"].atomic_blocks)
    seen = []

    def lock(using):
        seen.append((using, len(connections[using].atomic_blocks) > depth))
        raise LockTaken()

    resource = Resource(
        content=dict(PERMISSIVE_POLICY, rbac_context="p"), resource_type=policy_type
    )
    resource.scrud_test_alias = "other"
    with (
        routers("InstanceHintRouter"),
        mock.patch.object(registration, "lock_workflow_policy_type", lock),
        pytest.raises(LockTaken),
    ):
        ResourceRegistration.register_instance(resource)
    assert seen == [("other", True)]


def write_routing_errors():
    return [m for m in checks.run_checks() if m.id == "scrud_django.E004"]


def test_one_write_database_passes_the_check():
    assert scrud_checks.check_write_routing(None) == []
    with routers("OneAliasRouter"):
        assert scrud_checks.check_write_routing(None) == []


@pytest.mark.parametrize(
    "router, models",
    [
        ("TypeElsewhereRouter", "ResourceType -> 'other'"),
        ("ContextsElsewhereRouter", "ResourceContext -> 'other'"),
        (
            "tests.test_policy_duplicates.OtherWriteRouter",
            "Resource -> 'other'",
        ),
    ],
)
def test_split_write_routing_is_an_error(router, models):
    names = [router] if "." in router else [f"tests.test_write_routing.{router}"]
    with override_settings(DATABASE_ROUTERS=names):
        found = scrud_checks.check_write_routing(None)
        assert [(m.id, m.level) for m in found] == [("scrud_django.E004", checks.ERROR)]
        assert models in found[0].msg
        # Registered without a tag: `manage.py check` runs it, no database
        # needed.
        assert write_routing_errors() == found


# The type row's etag bump saves only etag and modified_at: a copy of the
# type read earlier (a lagging replica, or before a re-registration
# committed) must not write its other columns back.

NEW_SCHEMA_URI = "http://schema.example/new"


@contextmanager
def a_stale_todo_type():
    """The ToDo type as stored (its ``schema_uri`` just changed) and a copy
    read before that change."""
    reverse("todos-list")
    stored = ResourceType.objects.get(slug="todos")
    stale = ResourceType.objects.get(pk=stored.pk)
    ResourceType.objects.filter(pk=stored.pk).update(schema_uri=NEW_SCHEMA_URI)
    yield stale


def assert_bumped_and_kept(stale):
    stored = ResourceType.objects.get(pk=stale.pk)
    assert stored.schema_uri == NEW_SCHEMA_URI
    assert stored.etag == stale.etag


@pytest.mark.django_db
def test_server_side_writes_bump_only_the_types_etag():
    with a_stale_todo_type() as stale:
        todo = ResourceRegistration.register_instance(
            Resource(content=dict(NOT_STARTED), resource_type=stale)
        )
        assert_bumped_and_kept(stale)
        with mock.patch.object(
            registration, "get_resource_type_for", return_value=stale
        ):
            ResourceRegistration.update(dict(NOT_STARTED), "todos", str(todo.pk))
            assert_bumped_and_kept(stale)
            ResourceRegistration.delete("todos", str(todo.pk))
            assert_bumped_and_kept(stale)


@pytest.mark.django_db
def test_api_writes_bump_only_the_types_etag():
    from scrud_django.mixins.view_mixins import ResourceViewMixin

    client = admin_client()
    with (
        a_stale_todo_type() as stale,
        mock.patch.object(ResourceViewMixin, "get_resource_type", return_value=stale),
    ):
        response = client.post(reverse("todos-list"), NOT_STARTED, format="json")
        assert response.status_code == 201, response.content
        assert_bumped_and_kept(stale)
        url = response["Location"]
        response = client.put(
            url, NOT_STARTED, format="json", HTTP_IF_MATCH=response["ETag"]
        )
        assert response.status_code == 200, response.content
        assert_bumped_and_kept(stale)
        response = client.delete(url, HTTP_IF_MATCH=response["ETag"])
        assert response.status_code == 204, response.content
        assert_bumped_and_kept(stale)
