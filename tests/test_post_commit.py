"""Side effects outside the database run when the write commits: the
configured ``SCRUD_WORKFLOW_ACTIONS`` (`ScrudSignalProcessor`) and the search
index (`scrud_django.search.OnCommitSignalProcessor`, the test settings'
``HAYSTACK_SIGNAL_PROCESSOR``). A write rolled back (a post-save 403, any
error) runs none of them. ``scrud_post_save`` itself still fires inside the
write, before the post-save check."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.scrud_signals import scrud_post_save
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
    """The ToDo actions that ran: ``("save", pk)`` and ``("delete", etag)``
    (a deleted instance has no pk)."""
    calls = []
    entry = actions[TODO]

    def on_save(sender, instance, **kwargs):
        calls.append(("save", instance.pk))

    def on_delete(sender, instance, **kwargs):
        calls.append(("delete", instance.etag))

    monkeypatch.setitem(entry, "on_save", on_save)
    monkeypatch.setitem(entry, "on_delete", on_delete)
    return calls


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
def todos_moved_after_save():
    """A ``scrud_post_save`` receiver that moves every saved ToDo to the
    context "other", where the writer may not write: the post-save check
    then refuses the write with 403 and rolls it back."""

    def move(sender, instance, resource_type_uri=None, **kwargs):
        if resource_type_uri == TODO:
            content = {**instance.content, "rbac_context": "other"}
            type(instance).objects.filter(pk=instance.pk).update(content=content)

    scrud_post_save.connect(move, weak=False, dispatch_uid="test-post-commit-move")
    try:
        yield
    finally:
        scrud_post_save.disconnect(dispatch_uid="test-post-commit-move")


def a_todo(content=NOT_STARTED):
    reverse("todos-list")
    return ResourceRegistration.register(dict(content), "todos")


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
    assert ran == [("delete", todo.etag)]
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
