"""``GenericViewMixin`` (views of a model of your own, here
``tests.urls.NoteViewSet`` over ``tests.models.Note``) gets what
``ResourceViewMixin`` has: list validators computed from what the requester
may list, a PUT or DELETE run in one transaction with the row locked, and
scrudful's precondition errors as problem details."""

import json
from unittest import mock
from uuid import uuid4

import pytest
from django.db import DEFAULT_DB_ALIAS, connection, transaction
from django.db.models import Count, QuerySet
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework import generics
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from scoped_rbac.models import Role, RoleAssignment

from .factories import UserFactory
from .models import Note

NOTE = "tests://Note"
NOTE_LIST = f"{NOTE}/list"


def a_note(text="x", rbac_context=""):
    reverse("generic-notes-list")  # tests.urls registers the routes
    return Note.objects.create(
        text=text, rbac_context=rbac_context, etag=uuid4().hex, modified_at=now()
    )


def server_write(note, text):
    """Another writer's change of `note`, as code of the adopter's own makes
    it (not through the view)."""

    def write():
        Note.objects.filter(pk=note.pk).update(
            text=text, etag=uuid4().hex, modified_at=now()
        )

    return write


def superuser_client():
    client = APIClient()
    client.force_authenticate(UserFactory(is_staff=True, is_superuser=True))
    return client


def reader_in(context):
    user = UserFactory()
    role = Role(
        definition={"http.GET": [NOTE, NOTE_LIST]},
        etag=uuid4().hex,
        modified_at=now(),
    )
    role.save()
    RoleAssignment(
        user=user, role=role, rbac_context=context, etag=uuid4().hex, modified_at=now()
    ).save()
    client = APIClient()
    client.force_authenticate(user)
    return client


def detail(note):
    return reverse("generic-notes-detail", args=[note.pk])


def list_url():
    return reverse("generic-notes-list")


def concurrent_write_before_the_write_transaction(write):
    """As in tests/test_atomic_writes.py: run `write` right before the request
    opens its first transaction on the default database (Note's)."""
    pending = [write]
    original = transaction.Atomic.__enter__

    def enter(self):
        if pending and (self.using or DEFAULT_DB_ALIAS) == DEFAULT_DB_ALIAS:
            pending.pop()()
        return original(self)

    return mock.patch.object(transaction.Atomic, "__enter__", enter), pending


@pytest.mark.django_db
def test_a_write_to_a_listed_note_changes_the_lists_validators():
    note = a_note()
    client = superuser_client()
    response = client.get(list_url())
    assert response.status_code == 200, response.content
    etag = response["ETag"]
    assert client.get(list_url(), HTTP_IF_NONE_MATCH=etag).status_code == 304
    # A write of the model of your own does not touch the resource type.
    server_write(note, "changed")()
    response = client.get(list_url(), HTTP_IF_NONE_MATCH=etag)
    assert response.status_code == 200, response.content
    assert response["ETag"] != etag
    assert [item["text"] for item in response.json()] == ["changed"]
    etag = response["ETag"]
    a_note("new")
    response = client.get(list_url(), HTTP_IF_NONE_MATCH=etag)
    assert response.status_code == 200, response.content
    assert len(response.json()) == 2


@pytest.mark.django_db
def test_the_lists_validators_are_what_the_requester_may_list():
    mine = a_note("mine", "a")
    theirs = a_note("theirs", "b")
    client = reader_in("a")
    response = client.get(list_url())
    assert response.status_code == 200, response.content
    assert [item["text"] for item in response.json()] == ["mine"]
    etag, last_modified = response["ETag"], response["Last-Modified"]
    # A write the requester cannot see changes neither validator.
    server_write(theirs, "changed")()
    response = client.get(list_url(), HTTP_IF_NONE_MATCH=etag)
    assert response.status_code == 304
    assert response["ETag"] == etag
    response = client.get(list_url())
    assert response["Last-Modified"] == last_modified
    # One they can see changes the ETag.
    server_write(mine, "changed")()
    assert client.get(list_url(), HTTP_IF_NONE_MATCH=etag).status_code == 200


@pytest.mark.django_db
def test_a_put_whose_if_match_a_concurrent_write_made_stale_is_412():
    note = a_note()
    patch, pending = concurrent_write_before_the_write_transaction(
        server_write(note, "theirs")
    )
    with patch:
        response = superuser_client().put(
            detail(note),
            {"text": "mine"},
            format="json",
            HTTP_IF_MATCH=quote_etag(note.etag),
        )
    assert not pending, "the request opened no transaction"
    assert response.status_code == 412, response.content
    assert response["Content-Type"] == "application/problem+json"
    assert response.json()["code"] == "precondition_failed"
    assert Note.objects.get(pk=note.pk).text == "theirs"


@pytest.mark.django_db
def test_a_delete_whose_if_match_a_concurrent_write_made_stale_is_412():
    note = a_note()
    patch, pending = concurrent_write_before_the_write_transaction(
        server_write(note, "theirs")
    )
    with patch:
        response = superuser_client().delete(
            detail(note), HTTP_IF_MATCH=quote_etag(note.etag)
        )
    assert not pending, "the request opened no transaction"
    assert response.status_code == 412, response.content
    assert Note.objects.filter(pk=note.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["put", "delete"])
def test_the_row_is_locked_on_the_write_database_before_the_lookup(method):
    # The row is locked with a query of its own (select_for_update on the
    # model's base manager, filtered by the URL's lookup), then looked up
    # through the view's queryset, unlocked, on the same database.
    note = a_note()
    events = []
    original_lock = QuerySet.select_for_update
    original_lookup = generics.get_object_or_404

    def select_for_update(self, *args, **kwargs):
        if self.model is Note:
            events.append(("lock", self.db))
        return original_lock(self, *args, **kwargs)

    def get_object_or_404(queryset, *args, **kwargs):
        if queryset.model is Note:
            events.append(
                ("lookup", queryset.db, bool(queryset.query.select_for_update))
            )
        return original_lookup(queryset, *args, **kwargs)

    with (
        mock.patch.object(QuerySet, "select_for_update", select_for_update),
        mock.patch.object(generics, "get_object_or_404", get_object_or_404),
    ):
        response = getattr(superuser_client(), method)(
            detail(note),
            *([{"text": "y"}] if method == "put" else []),
            format="json",
            HTTP_IF_MATCH=quote_etag(note.etag),
        )
    assert response.status_code in (200, 204), response.content
    assert events[:2] == [("lock", "default"), ("lookup", "default", False)], events


@pytest.mark.django_db
def test_a_refused_write_rolls_back():
    note = a_note()
    response = superuser_client().put(detail(note), {"text": "y"}, format="json")
    # No precondition: 428, and nothing written.
    assert response.status_code == 428, response.content
    assert response["Content-Type"] == "application/problem+json"
    assert Note.objects.get(pk=note.pk).text == "x"


def distinct_notes():
    return Note.objects.distinct()


def annotated_notes():
    return Note.objects.annotate(n=Count("id"))


def notes_with_authors():
    return Note.objects.select_related("author")


@pytest.mark.django_db
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL refuses these locks"
)
@pytest.mark.parametrize(
    "queryset", [distinct_notes, annotated_notes, notes_with_authors]
)
@pytest.mark.parametrize("method", ["put", "delete"])
def test_a_write_locks_the_row_whatever_the_views_queryset(queryset, method):
    # PostgreSQL refuses SELECT ... FOR UPDATE with DISTINCT, with GROUP BY
    # and on the nullable side of an outer join: the lock must not be taken
    # through the view's queryset.
    from .urls import NoteViewSet

    class Notes(NoteViewSet):
        def get_queryset(self):
            return queryset()

    note = a_note()
    factory = APIRequestFactory()
    url = f"/notes/{note.pk}/"
    if method == "put":
        request = factory.put(
            url,
            json.dumps({"text": "y"}),
            content_type="application/json",
            HTTP_IF_MATCH=quote_etag(note.etag),
        )
    else:
        request = factory.delete(url, HTTP_IF_MATCH=quote_etag(note.etag))
    force_authenticate(request, user=UserFactory(is_superuser=True, is_staff=True))
    response = Notes.as_view({method: "update" if method == "put" else "destroy"})(
        request, slug=str(note.pk)
    )
    assert response.status_code in (200, 204), response.data
