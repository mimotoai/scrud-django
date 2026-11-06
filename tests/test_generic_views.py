"""``GenericViewMixin`` (views of a model of your own, here
``tests.urls.NoteViewSet`` over ``tests.models.Note``) gets what
``ResourceViewMixin`` has: list validators computed from what the requester
may list, a PUT or DELETE run in one transaction with the row locked, and
scrudful's precondition errors as problem details; a conditional create
(``create``, or a POST ``@action`` of the list) is evaluated under the
collection's lock."""

import json
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from unittest import mock
from uuid import uuid4

import pytest
from django.db import DEFAULT_DB_ALIAS, connection, connections, transaction
from django.db.models import Count, F, QuerySet, TextField, Value
from django.db.models.functions import Concat
from django.http import Http404
from django.urls import reverse
from django.utils.http import http_date, quote_etag
from django.utils.timezone import now
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from scoped_rbac.models import Role, RoleAssignment
from scrudful.rest_framework import scrudful

from .factories import UserFactory
from .models import Note
from .test_conditional_create import TWO_WRITERS, run_threads

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


@contextmanager
def evaluated_note_queries():
    """Record each ``Note`` queryset the database evaluates (not merely
    builds): ``(database, select_for_update, SQL)``, in order."""
    events = []
    original = QuerySet._fetch_all

    def fetch_all(self):
        if self.model is Note and self._result_cache is None:
            events.append(
                (self.db, bool(self.query.select_for_update), str(self.query))
            )
        return original(self)

    with mock.patch.object(QuerySet, "_fetch_all", fetch_all):
        yield events


# Quoted as the database quotes them ("x" or `x`).
NOTE_TABLE = connection.ops.quote_name("tests_note")
NOTE_ID = f"{NOTE_TABLE}.{connection.ops.quote_name('id')}"


def where_pk_is(pk):
    return f"WHERE {NOTE_ID} = {pk}"


def write(client, method, url, etag, body=None):
    if method == "put":
        return client.put(
            url, body or {"text": "y"}, format="json", HTTP_IF_MATCH=quote_etag(etag)
        )
    return client.delete(url, HTTP_IF_MATCH=quote_etag(etag))


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["put", "delete"])
def test_the_row_is_locked_on_the_write_database_before_the_lookup(method):
    # The row the URL names is locked with a query of its own, which runs
    # (select_for_update on the model's base manager, filtered to that row),
    # then looked up through the view's queryset, unlocked, on the same
    # database.
    note = a_note()
    a_note("another")
    with evaluated_note_queries() as events:
        response = write(superuser_client(), method, detail(note), note.etag)
    assert response.status_code in (200, 204), response.content
    (lock_db, lock_for_update, lock_sql), (lookup_db, lookup_for_update, _) = events[:2]
    assert (lock_db, lock_for_update) == ("default", True), events
    assert where_pk_is(note.pk) in lock_sql, lock_sql
    assert lock_sql.startswith(
        f"SELECT {NOTE_ID} AS {connection.ops.quote_name('pk')} FROM {NOTE_TABLE} "
    ), lock_sql
    assert (lookup_db, lookup_for_update) == ("default", False), events


class NotesByCode:
    """A view whose ``lookup_field`` is an annotation of its queryset
    (``code``, "n-" and the text), which the model's base manager cannot
    filter by."""

    def __new__(cls):
        from .urls import NoteViewSet

        class ByCode(NoteViewSet):
            lookup_field = "code"

            def get_queryset(self):
                return Note.objects.annotate(
                    code=Concat(Value("n-"), F("text"), output_field=TextField())
                )

        return ByCode


def by_code(view, method, code, etag, body=None):
    factory = APIRequestFactory()
    if method == "put":
        request = factory.put(
            f"/notes/{code}/",
            json.dumps(body or {"text": "abc"}),
            content_type="application/json",
            HTTP_IF_MATCH=quote_etag(etag),
        )
    else:
        request = factory.delete(f"/notes/{code}/", HTTP_IF_MATCH=quote_etag(etag))
    force_authenticate(request, user=UserFactory(is_superuser=True, is_staff=True))
    action = "update" if method == "put" else "destroy"
    return view.as_view({method: action})(request, slug=code)


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["put", "delete"])
def test_a_lookup_field_the_queryset_annotates_is_locked_by_key(method):
    # The base manager cannot filter by an annotation: the key is read
    # through the view's queryset (unlocked), the row locked by its key,
    # then looked up as usual.
    note = a_note("abc")
    a_note("other")
    with evaluated_note_queries() as events:
        response = by_code(NotesByCode(), method, "n-abc", note.etag)
    assert response.status_code in (200, 204), response.data
    # The key is resolved through the view's queryset (unlocked), the row
    # locked by that key, then looked up (unlocked). A successful PUT has
    # scrudful look the row up again for the response's validators.
    resolve, lock, lookup = events[:3]
    assert not resolve[1] and "n-abc" in resolve[2], events
    assert lock[1] and f"WHERE {NOTE_ID} IN ({note.pk})" in lock[2], lock
    assert not lookup[1] and "n-abc" in lookup[2], events


@pytest.mark.django_db
def test_a_row_that_stops_matching_before_its_lock_is_404():
    # The key "n-abc" resolves to `first`; before the lock is taken another
    # writer renames it and gives "abc" to `second`. The lookup then finds
    # `second`, a row this request did not lock: 404, nothing written.
    first = a_note("abc")
    second = a_note("xyz")
    original = QuerySet.select_for_update

    def concurrent_rename(self, *args, **kwargs):
        if self.model is Note and Note.objects.get(pk=first.pk).text == "abc":
            Note.objects.filter(pk=first.pk).update(text="renamed")
            Note.objects.filter(pk=second.pk).update(text="abc")
        return original(self, *args, **kwargs)

    with mock.patch.object(QuerySet, "select_for_update", concurrent_rename):
        response = by_code(NotesByCode(), "put", "n-abc", second.etag, {"text": "mine"})
    assert response.status_code == 404, response.data
    # Nothing written: the 404 rolls the request back, the rename with it.
    assert Note.objects.get(pk=first.pk).text == "abc"
    assert Note.objects.get(pk=second.pk).text == "xyz"


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


def note_writer(*contexts, condition=None):
    """A client whose user may GET and PUT notes in each of `contexts` (the
    PUT conditioned on `condition`, if given)."""
    user = UserFactory()
    put = {NOTE: condition} if condition is not None else [NOTE]
    role = Role(
        definition={"http.GET": [NOTE, NOTE_LIST], "http.PUT": put},
        etag=uuid4().hex,
        modified_at=now(),
    )
    role.save()
    for context in contexts:
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=context,
            etag=uuid4().hex,
            modified_at=now(),
        ).save()
    client = APIClient()
    client.force_authenticate(user)
    return client


# The type of what each call of the `rewriting` operator got.
REWRITTEN = []


def rewriting(resource, configuration):
    """An adopter's buggy operator that rewrites the body's text (and a
    nested value), and allows. (The object check gives it the `Note`
    itself, as scoped-rbac does: left as it is.)"""
    REWRITTEN.append(type(resource))
    if isinstance(resource, dict):
        resource["text"] = "MUTATED"
        resource["nested"]["evil"] = True
    return True


@pytest.mark.django_db
def test_a_generic_views_request_check_reads_the_body_itself():
    # scoped-rbac's request check gives operators the parsed body itself, a
    # plain dict (scoped-rbac issue #55: not guarded): an operator that
    # changes it changes what the view then stores, a bug in that operator.
    from django.conf import settings
    from django.test import override_settings

    note = a_note("before", rbac_context="a")
    operators = dict(settings.SCOPED_RBAC, OPERATORS={"rewriting": rewriting})
    with override_settings(SCOPED_RBAC=operators):
        client = note_writer("a", condition={"condition": {"operator": "rewriting"}})
        response = client.put(
            detail(note),
            {"text": "after", "rbac_context": "a", "nested": {"a": 1}},
            format="json",
            HTTP_IF_MATCH=quote_etag(Note.objects.get(pk=note.pk).etag),
        )
    assert response.status_code == 200, response.content
    # The request check's body (a dict), the object check's Note.
    assert set(REWRITTEN) == {dict, Note}, REWRITTEN
    assert Note.objects.get(pk=note.pk).text == "MUTATED"


@pytest.mark.django_db
def test_a_generic_views_body_naming_several_contexts_is_checked_in_each():
    # scoped-rbac's default contexts_from (access.body_contexts) reads the
    # body's array of contexts (a list): PUT is needed in each.
    note = a_note("before", rbac_context="a")
    for contexts, status_code in ((("a",), 403), (("a", "b"), 200)):
        client = note_writer(*contexts)
        response = client.put(
            detail(note),
            {"text": "after", "rbac_context": ["a", "b"]},
            format="json",
            HTTP_IF_MATCH=quote_etag(Note.objects.get(pk=note.pk).etag),
        )
        assert response.status_code == status_code, response.content


# A conditional create (scrudful 0.9 evaluates a POST's preconditions against
# the list before the view's ``create`` runs): one whose preconditions need the
# list's validators runs in a transaction holding scoped-rbac's collection lock
# of the view's model (GenericViewMixin.conditional_create_lock), taken after
# the request is authorized.


def collection_holds():
    from scoped_rbac.models import CollectionLock

    lock = CollectionLock.objects.filter(label=Note._meta.label_lower).first()
    return 0 if lock is None else lock.holds


def created(text="new"):
    return Note.objects.filter(text=text).count()


@contextmanager
def recorded_collection_locks(events=None):
    """Each conditional create lock GenericViewMixin takes, as it is held:
    ``(thread name, time.monotonic())``; also ``"locked"`` appended to
    `events`, if given. A rollback undoes the lock's ``holds``, not this
    record."""
    from scrud_django.mixins.view_mixins import GenericViewMixin

    original = GenericViewMixin.conditional_create_lock
    taken = []

    @contextmanager
    def conditional_create_lock(self, using):
        with original(self, using):
            taken.append((threading.current_thread().name, time.monotonic()))
            if events is not None:
                events.append("locked")
            yield

    with mock.patch.object(
        GenericViewMixin, "conditional_create_lock", conditional_create_lock
    ):
        yield taken


@pytest.mark.django_db
def test_a_generic_create_with_the_lists_etag_creates_under_the_collection_lock():
    a_note()
    client = superuser_client()
    etag = client.get(list_url())["ETag"]
    before = collection_holds()
    response = client.post(
        list_url(), {"text": "new"}, format="json", HTTP_IF_MATCH=etag
    )
    assert response.status_code == 201, response.content
    assert created() == 1
    assert collection_holds() == before + 1


@pytest.mark.django_db
def test_a_generic_create_with_a_stale_etag_is_412_and_creates_nothing():
    a_note()
    client = superuser_client()
    etag = client.get(list_url())["ETag"]
    a_note("another")
    response = client.post(
        list_url(), {"text": "new"}, format="json", HTTP_IF_MATCH=etag
    )
    assert response.status_code == 412, response.content
    assert response["Content-Type"] == "application/problem+json"
    assert created() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "headers", [{}, {"HTTP_IF_MATCH": "*"}], ids=["unconditional", "if-match-any"]
)
def test_a_generic_create_that_needs_no_validators_takes_no_lock(headers):
    a_note()
    before = collection_holds()
    response = superuser_client().post(
        list_url(), {"text": "new"}, format="json", **headers
    )
    assert response.status_code == 201, response.content
    assert collection_holds() == before


@pytest.mark.django_db
def test_a_refused_generic_create_takes_no_lock():
    a_note()
    before = collection_holds()
    with recorded_collection_locks() as taken:
        response = reader_in("").post(
            list_url(), {"text": "new"}, format="json", HTTP_IF_MATCH='"nope"'
        )
    assert response.status_code == 403, response.content
    assert taken == []  # refused before the lock, not taken and rolled back
    assert collection_holds() == before
    assert created() == 0


@pytest.mark.django_db
def test_a_conditional_create_locks_the_collection_before_it_reads_the_list():
    a_note()
    client = superuser_client()
    etag = client.get(list_url())["ETag"]
    note_table = connection.ops.quote_name(Note._meta.db_table)
    events = []

    def record(execute, sql, params, many, context):
        if sql.startswith("SELECT") and note_table in sql:
            events.append("list read")
        return execute(sql, params, many, context)

    with recorded_collection_locks(events), connection.execute_wrapper(record):
        response = client.post(
            list_url(), {"text": "new"}, format="json", HTTP_IF_MATCH=etag
        )
    assert response.status_code == 201, response.content
    assert events and events[0] == "locked", events
    assert "list read" in events, events


def import_url():
    return reverse("generic-notes-import")


@pytest.mark.django_db
def test_a_stale_create_inside_the_collection_lock_rolls_back_its_lock():
    a_note()
    client = superuser_client()
    etag = client.get(list_url())["ETag"]
    a_note("another")
    before = collection_holds()
    response = client.post(
        list_url(), {"text": "new"}, format="json", HTTP_IF_MATCH=etag
    )
    assert response.status_code == 412, response.content
    assert collection_holds() == before
    assert created() == 0


@pytest.mark.django_db
def test_a_conditional_create_the_serializer_refuses_rolls_back():
    # The lock is taken, the preconditions pass, the create answers 400: the
    # transaction rolls back, the lock's hold with it.
    a_note()
    client = superuser_client()
    etag = client.get(list_url())["ETag"]
    before = collection_holds()
    response = client.post(
        list_url(),
        {"text": "new", "rbac_context": "c" * 300},  # past max_length
        format="json",
        HTTP_IF_MATCH=etag,
    )
    assert response.status_code == 400, response.content
    assert "rbac_context" in response.json()["errors"], response.content
    assert collection_holds() == before
    assert created() == 0


# A POST @action of the list (tests.urls.NoteImportViewSet.import_note), whose
# preconditions scrudful does not evaluate, evaluates them itself with
# list_precondition_response, under the collection lock: it is a conditional
# create too (LockedWriteMixin.is_conditional_create).


@pytest.mark.django_db
def test_an_actions_stale_create_is_412_by_list_precondition_response():
    a_note()
    client = superuser_client()
    etag = client.get(list_url())["ETag"]
    a_note("another")
    before = collection_holds()
    response = client.post(
        import_url(), {"text": "new"}, format="json", HTTP_IF_MATCH=etag
    )
    assert response.status_code == 412, response.content
    assert response["Content-Type"] == "application/problem+json"
    assert response.json()["status"] == 412
    assert created() == 0
    assert collection_holds() == before


@pytest.mark.django_db
def test_an_actions_create_with_the_lists_etag_creates_under_the_lock():
    a_note()
    client = superuser_client()
    etag = client.get(list_url())["ETag"]
    before = collection_holds()
    response = client.post(
        import_url(), {"text": "new"}, format="json", HTTP_IF_MATCH=etag
    )
    assert response.status_code == 201, response.content
    assert created() == 1
    assert collection_holds() == before + 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "headers, status",
    [({}, 201), ({"HTTP_IF_MATCH": "*"}, 201), ({"HTTP_IF_NONE_MATCH": "*"}, 412)],
    ids=["unconditional", "if-match-any", "if-none-match-any"],
)
def test_an_actions_create_that_needs_no_validators_takes_no_lock(headers, status):
    a_note()
    before = collection_holds()
    response = superuser_client().post(
        import_url(), {"text": "new"}, format="json", **headers
    )
    assert response.status_code == status, response.content
    assert created() == (1 if status == 201 else 0)
    assert collection_holds() == before


@pytest.mark.django_db
def test_a_list_precondition_response_outside_a_view_reads_the_list_unlocked():
    # Called on database `using` outside a conditional create (as a handler
    # of a view that is no viewset may call it), it evaluates without a lock.
    from .urls import NoteViewSet

    a_note()
    etag = superuser_client().get(list_url())["ETag"]
    view = NoteViewSet(action_map={"post": "create"})
    user = UserFactory(is_superuser=True, is_staff=True)
    for if_match, expected in ((etag, None), ('"stale"', 412)):
        request = APIRequestFactory().post(list_url(), {}, HTTP_IF_MATCH=if_match)
        force_authenticate(request, user)
        request = view.initialize_request(request)
        view.request, view.args, view.kwargs = request, (), {}
        view.format_kwarg = None
        response = view.list_precondition_response(request, using=DEFAULT_DB_ALIAS)
        assert (response and response.status_code) == expected, response
    assert collection_holds() == 0


@pytest.mark.django_db
def test_an_undecorated_handler_of_the_lists_post_is_a_conditional_create():
    # A POST mapped to a method of the view's own (no @action, no
    # @scrudful) that calls the scrudful create: it is the list's POST (the
    # URL names no item), so it takes the collection lock, and the create
    # (the outer scrudful handler) evaluates the preconditions under it.
    from .urls import NoteViewSet

    class Delegating(NoteViewSet):
        def import_note(self, request):
            return self.create(request)

    a_note()
    etag = superuser_client().get(list_url())["ETag"]
    user = UserFactory(is_superuser=True, is_staff=True)

    def post(if_match):
        request = APIRequestFactory().post(
            list_url(), {"text": "new"}, format="json", HTTP_IF_MATCH=if_match
        )
        force_authenticate(request, user)
        return Delegating.as_view({"post": "import_note"})(request)

    with recorded_collection_locks() as taken:
        assert post('"stale"').status_code == 412
        assert created() == 0
        assert post(etag).status_code == 201
    assert created() == 1
    assert len(taken) == 2, taken


# A POST @action of the list whose handler calls a create that does not
# evaluate the preconditions (tests.urls.NoteUncheckedImportViewSet): scrudful
# evaluates nothing of the nested create, so its success is refused
# (ImproperlyConfigured) and what it wrote rolled back, whatever the
# precondition. One without a precondition is no conditional POST.


@pytest.mark.django_db
@pytest.mark.parametrize(
    "headers",
    [
        {"HTTP_IF_MATCH": "list"},
        {"HTTP_IF_NONE_MATCH": '"other"'},
        {"HTTP_IF_NONE_MATCH": "*"},
        {"HTTP_IF_MATCH": "*", "HTTP_IF_NONE_MATCH": '"other"'},
        {"HTTP_IF_UNMODIFIED_SINCE": "Tue, 01 Jan 2030 00:00:00 GMT"},
    ],
    ids=[
        "if-match-list",
        "if-none-match-tag",
        "if-none-match-any",
        "if-match-any-and-if-none-match",
        "if-unmodified-since",
    ],
)
def test_an_actions_create_nothing_evaluated_is_refused_and_rolled_back(headers):
    from django.core.exceptions import ImproperlyConfigured

    a_note()
    client = superuser_client()
    if headers.get("HTTP_IF_MATCH") == "list":
        headers = {"HTTP_IF_MATCH": client.get(list_url())["ETag"]}
    with pytest.raises(ImproperlyConfigured, match="without evaluating"):
        client.post(
            reverse("generic-notes-unchecked-import"),
            {"text": "new"},
            format="json",
            **headers,
        )
    assert created() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"HTTP_IF_MATCH": "*"},
        {"HTTP_IF_MATCH": "*", "HTTP_IF_UNMODIFIED_SINCE": "x"},
    ],
    ids=["unconditional", "if-match-any", "if-match-any-and-since"],
)
def test_an_actions_create_whose_preconditions_always_pass_is_not_refused(headers):
    # No precondition, or an If-Match: * (which passes on a list, which
    # always exists; If-Unmodified-Since is not evaluated beside it): there
    # is nothing to evaluate.
    a_note()
    response = superuser_client().post(
        reverse("generic-notes-unchecked-import"),
        {"text": "new"},
        format="json",
        **headers,
    )
    assert response.status_code == 201, response.content
    assert created() == 1


# A POST that ``@<action>.mapping.post`` maps to a handler of its own
# (tests.urls.NoteMappedImportViewSet), which DRF gives no ``detail``: a
# conditional create all the same, routed by a router (which gives the view
# ``detail``) or not (the viewset's extra actions tell).

MAPPED_ROUTES = pytest.mark.parametrize(
    "route", ["routed-notes-imports", "generic-notes-mapped-import"]
)


@pytest.mark.django_db
@MAPPED_ROUTES
def test_a_mapped_posts_stale_create_is_412(route):
    a_note()
    client = superuser_client()
    etag = client.get(list_url())["ETag"]
    a_note("another")
    with recorded_collection_locks() as taken:
        response = client.post(
            reverse(route), {"text": "new"}, format="json", HTTP_IF_MATCH=etag
        )
    assert response.status_code == 412, response.content
    assert created() == 0
    assert len(taken) == 1, taken


@pytest.mark.django_db
@MAPPED_ROUTES
def test_a_mapped_posts_create_with_the_lists_etag_creates_under_the_lock(route):
    a_note()
    client = superuser_client()
    etag = client.get(list_url())["ETag"]
    with recorded_collection_locks() as taken:
        response = client.post(
            reverse(route), {"text": "new"}, format="json", HTTP_IF_MATCH=etag
        )
    assert response.status_code == 201, response.content
    assert created() == 1
    assert len(taken) == 1, taken


@pytest.mark.django_db
@pytest.mark.parametrize(
    "initkwargs, url_kwargs, expected",
    [
        ({"action_map": {"post": "create"}}, {}, True),
        ({"action_map": {"post": "import_note"}}, {}, True),  # mapped, no router
        ({"action_map": {"post": "touch"}}, {"slug": "1"}, False),  # detail action
        ({"action_map": {"post": "touch"}}, {}, False),  # as its @action says
        ({"action_map": {"post": "own"}}, {}, True),  # no action: by the URL
        ({"action_map": {"post": "own"}}, {"slug": "1"}, False),
        ({"action_map": {"post": "import_note"}, "detail": False}, {}, True),
        ({"action_map": {"post": "import_note"}, "detail": True}, {}, False),
        ({"action_map": {"post": "create"}, "detail": True}, {}, False),
        ({}, {}, True),  # a plain view's post, to no item
        ({}, {"slug": "1"}, False),
    ],
)
def test_which_posts_are_to_the_list(initkwargs, url_kwargs, expected):
    from .urls import NoteMappedImportViewSet

    class View(NoteMappedImportViewSet):
        @action(detail=True, methods=["post"])
        def touch(self, request, slug):
            return Response({})

    view = View(**initkwargs)
    view.kwargs = url_kwargs
    assert view.is_list_post(APIRequestFactory().post("/")) is expected


def test_only_a_post_is_to_the_list():
    from scrud_django.mixins.view_mixins import LockedWriteMixin

    view = LockedWriteMixin()
    assert view.is_list_post(APIRequestFactory().post("/")) is True
    for method in ("get", "put", "patch", "delete", "options"):
        request = getattr(APIRequestFactory(), method)("/")
        assert view.is_list_post(request) is False, method


@pytest.mark.django_db
def test_an_initial_that_skips_super_is_refused_its_conditional_create():
    # DRF's initial alone: neither scoped-rbac's nor LockedWriteMixin's, so
    # the collection lock is never taken. The preconditions are refused
    # rather than evaluated unlocked.
    from django.core.exceptions import ImproperlyConfigured
    from rest_framework.views import APIView

    from .urls import NoteViewSet

    class Bypassing(NoteViewSet):
        def initial(self, request, *args, **kwargs):
            APIView.initial(self, request, *args, **kwargs)

    a_note()
    etag = superuser_client().get(list_url())["ETag"]
    request = APIRequestFactory().post(
        list_url(), {"text": "new"}, format="json", HTTP_IF_MATCH=etag
    )
    force_authenticate(request, UserFactory(is_superuser=True, is_staff=True))
    with pytest.raises(ImproperlyConfigured, match="outside a conditional create"):
        Bypassing.as_view({"post": "create"})(request)
    assert created() == 0


@pytest.mark.django_db
def test_a_view_without_a_queryset_attribute_writes_where_its_model_is_routed():
    from django.db import router

    from .urls import NoteViewSet

    class Unqueried(NoteViewSet):
        queryset = None

        def get_queryset(self):
            return Note.objects.all()

    def db_for_write(model, **hints):
        return "other" if model is Note else DEFAULT_DB_ALIAS

    with mock.patch.object(router, "db_for_write", db_for_write):
        assert Unqueried().locked_write_database() == "other"
        assert NoteViewSet().locked_write_database() == "other"


@TWO_WRITERS
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("lock_row", ["first-use", "existing"])
def test_two_generic_conditional_creates_with_one_etag_give_one_201_and_one_412(
    committed_types, lock_row
):
    # The first create pauses after its preconditions passed, holding the
    # collection lock; the second, sent meanwhile, waits for the lock, then
    # is evaluated against the list the first left. Whether or not the
    # collection's lock row exists yet (scoped-rbac creates it on first use).
    from scoped_rbac.conditional import lock_collection
    from scoped_rbac.models import CollectionLock

    from .urls import NoteSerializer

    if lock_row == "existing":
        with transaction.atomic():
            lock_collection(Note, DEFAULT_DB_ALIAS)
    label = Note._meta.label_lower
    assert CollectionLock.objects.filter(label=label).exists() is (
        lock_row == "existing"
    )
    a_note("existing")
    first_client, second_client = superuser_client(), superuser_client()
    etag = first_client.get(list_url())["ETag"]
    connections.close_all()
    original = NoteSerializer.is_valid
    evaluated = threading.Event()
    timeline = {}

    def is_valid(self, *args, **kwargs):
        if threading.current_thread().name == "first" and "paused" not in timeline:
            timeline["paused"] = time.monotonic()
            evaluated.set()
            time.sleep(0.8)
            timeline["resumed"] = time.monotonic()
        return original(self, *args, **kwargs)

    def create(client, text):
        return client.post(
            list_url(), {"text": text}, format="json", HTTP_IF_MATCH=etag
        ).status_code

    def second():
        assert evaluated.wait(10), "the first create never evaluated"
        timeline["second sent"] = time.monotonic()
        return create(second_client, "second")

    with (
        mock.patch.object(NoteSerializer, "is_valid", is_valid),
        recorded_collection_locks() as taken,
    ):
        results = run_threads(
            first=lambda: create(first_client, "first"), second=second
        )
    assert timeline["paused"] < timeline["second sent"] < timeline["resumed"], timeline
    assert results == {"first": 201, "second": 412}, results
    # The second waited for the lock: it held it only after the first resumed.
    locked = dict(taken)
    assert sorted(locked) == ["first", "second"], taken
    assert locked["second"] > timeline["resumed"], (taken, timeline)
    texts = sorted(Note.objects.values_list("text", flat=True))
    assert texts == ["existing", "first"], texts


# Whether a POST to the list was evaluated is recorded as it happens: by the
# list callbacks scrudful calls to evaluate it (gate_uniform_errors), a
# viewset's own Meta list validators as well as scrud's, or by
# list_precondition_response completing.


def custom_url():
    return reverse("generic-custom-etag-notes-list")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "if_match, status_code",
    [('"custom"', 201), ('"other"', 412), ("*", 201)],
    ids=["its-etag", "another-etag", "any"],
)
def test_a_create_evaluated_by_its_own_meta_list_validators(if_match, status_code):
    a_note()
    client = superuser_client()
    assert client.get(custom_url())["ETag"] == '"custom"'
    response = client.post(
        custom_url(), {"text": "new"}, format="json", HTTP_IF_MATCH=if_match
    )
    assert response.status_code == status_code, response.content
    assert created() == (1 if status_code == 201 else 0)


def bulk_url():
    return reverse("generic-bulk-notes")


@pytest.mark.django_db
@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "stale"])
def test_a_scrudful_handler_of_its_own_that_skips_evaluation_is_refused(fresh):
    # tests.urls.NoteBulkViewSet.bulk: an @scrudful handler with no list
    # validators (no @action) calling a create that does not evaluate: the
    # list's POST all the same (the URL names no item), refused once it
    # succeeds, and rolled back.
    from django.core.exceptions import ImproperlyConfigured

    a_note()
    client = superuser_client()
    etag = client.get(list_url())["ETag"] if fresh else '"stale"'
    with (
        recorded_collection_locks() as taken,
        pytest.raises(ImproperlyConfigured, match="without evaluating"),
    ):
        client.post(bulk_url(), {"text": "new"}, format="json", HTTP_IF_MATCH=etag)
    assert created() == 0
    assert len(taken) == 1, taken  # under the lock, as a conditional create


def serve(view_class, actions, request, **kwargs):
    """`request` served by an instance of `view_class` as DRF's ``as_view``
    serves it, returning the instance (its ``response`` set) so that a test
    can serve it again."""
    view = view_class()
    view.action_map = actions
    for method, name in actions.items():
        setattr(view, method, getattr(view, name))
    view.request, view.args, view.kwargs = request, (), kwargs
    view.response = view.dispatch(request, **kwargs)
    return view


def authenticated_post(url, **headers):
    request = APIRequestFactory().post(url, {"text": "new"}, format="json", **headers)
    force_authenticate(request, UserFactory(is_superuser=True, is_staff=True))
    return request


@pytest.mark.django_db
def test_a_refusal_the_handler_swallows_still_refuses_its_success():
    # The handler catches list_precondition_response's refusal (no lock: its
    # initial skips super()) and creates: nothing was evaluated, so the
    # success is refused, and rolled back.
    from django.core.exceptions import ImproperlyConfigured
    from rest_framework.views import APIView

    from .urls import NoteViewSet

    class Swallowing(NoteViewSet):
        def initial(self, request, *args, **kwargs):
            APIView.initial(self, request, *args, **kwargs)

        def import_note(self, request):
            try:
                failed = self.list_precondition_response(request)
            except ImproperlyConfigured:
                failed = None
            return failed or NoteViewSet.create.view_method(self, request)

    a_note()
    etag = superuser_client().get(list_url())["ETag"]
    with pytest.raises(ImproperlyConfigured, match="without evaluating"):
        Swallowing.as_view({"post": "import_note"})(
            authenticated_post(list_url(), HTTP_IF_MATCH=etag)
        )
    assert created() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "headers, refused",
    [({"HTTP_IF_MATCH": "*"}, False), ({"HTTP_IF_NONE_MATCH": "*"}, True)],
    ids=["if-match-any", "if-none-match-any"],
)
def test_a_create_override_not_decorated_again_is_refused_unless_it_cannot_fail(
    headers, refused
):
    # An override replaces scrudful's wrapper of create (scrudful_viewset
    # decorates the class it is given), so nothing evaluates it: refused,
    # but for an If-Match: * alone, which passes on a list.
    from django.core.exceptions import ImproperlyConfigured

    from .urls import NoteViewSet

    class Overriding(NoteViewSet):
        def create(self, request):
            return NoteViewSet.create.view_method(self, request)

    a_note()
    view = Overriding.as_view({"post": "create"})
    if refused:
        with pytest.raises(ImproperlyConfigured, match="decorated again"):
            view(authenticated_post(list_url(), **headers))
        assert created() == 0
    else:
        assert view(authenticated_post(list_url(), **headers)).status_code == 201
        assert created() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "headers, refused",
    [({"HTTP_IF_MATCH": "*"}, False), ({"HTTP_IF_NONE_MATCH": '"x"'}, True)],
    ids=["if-match-any", "if-none-match-tag"],
)
def test_a_list_action_that_creates_nothing_must_evaluate_too(headers, refused):
    # A POST @action of the list answering without writing (a search) is the
    # list's POST: its preconditions are evaluated, or it is refused.
    from django.core.exceptions import ImproperlyConfigured

    from .urls import ACTION, NoteViewSet

    class Searching(NoteViewSet):
        @action(detail=False, methods=["post"])
        @scrudful(**ACTION)
        def search(self, request):
            return Response([])

    a_note()
    view = Searching.as_view({"post": "search"})
    if refused:
        with pytest.raises(ImproperlyConfigured, match="without evaluating"):
            view(authenticated_post(list_url(), **headers))
    else:
        assert view(authenticated_post(list_url(), **headers)).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize("route", ["routed-notes-touch", "generic-notes-touch"])
def test_a_post_to_an_item_is_not_the_lists(route):
    # tests.urls.NoteMappedImportViewSet.touch, @action(detail=True): its
    # If-Match is the item's, not the list's: no collection lock, no
    # refusal (docs/follow-ups.md, detail-post-action-preconditions).
    note = a_note()
    with recorded_collection_locks() as taken:
        response = superuser_client().post(
            reverse(route, args=[note.pk]), {}, format="json", HTTP_IF_MATCH='"x"'
        )
    assert response.status_code == 200, response.content
    assert response.json() == {"touched": str(note.pk)}
    assert taken == []


@pytest.mark.django_db
def test_a_reused_view_forgets_what_it_recorded_of_its_last_post():
    # Records of an earlier POST (evaluated, by scrudful or not, with the lock
    # held) must not let the next one through unevaluated.
    from django.core.exceptions import ImproperlyConfigured

    from .urls import NoteUncheckedImportViewSet

    a_note()
    actions = {"post": "import_note"}
    view = serve(NoteUncheckedImportViewSet, actions, authenticated_post(list_url()))
    assert view.response.status_code == 201
    view._scrud_list_preconditions_evaluated = True
    view._scrud_create_locked = True
    view._scrud_create_validators = (None, None)
    request = authenticated_post(list_url(), HTTP_IF_NONE_MATCH="*")
    view.request = request
    with pytest.raises(ImproperlyConfigured, match="without evaluating"):
        view.dispatch(request)
    assert created() == 1


@pytest.mark.django_db
def test_a_view_whose_get_queryset_reads_drfs_request_serves_its_writes():
    # locked_write_database does not call get_queryset() (DRF has not wrapped
    # the request when dispatch opens the transaction).
    from .urls import NoteViewSet

    class Unqueried(NoteViewSet):
        queryset = None

        def get_queryset(self):
            assert self.request.query_params is not None  # DRF's request
            return Note.objects.all()

    note = a_note()
    etag = superuser_client().get(detail(note))["ETag"]
    request = APIRequestFactory().put(
        detail(note), {"text": "changed"}, format="json", HTTP_IF_MATCH=etag
    )
    force_authenticate(request, UserFactory(is_superuser=True, is_staff=True))
    response = Unqueried.as_view({"put": "update"})(request, slug=str(note.pk))
    assert response.status_code == 200, response.data
    assert Note.objects.get(pk=note.pk).text == "changed"


def routing_notes_to(alias):
    """``router.db_for_write`` sending Note's writes to `alias`."""
    from django.db import router

    def db_for_write(model, **hints):
        return alias if model is Note else DEFAULT_DB_ALIAS

    return mock.patch.object(router, "db_for_write", db_for_write)


@pytest.mark.django_db
def test_the_queryset_attribute_names_the_model_before_the_serializer():
    # The queryset's model (Note, routed to "other") decides, not the
    # serializer's (ResourceType, on the default database).
    from rest_framework import serializers

    from scrud_django.models import ResourceType

    from .urls import NoteViewSet

    class TypeSerializer(serializers.ModelSerializer):
        class Meta:
            model = ResourceType
            fields = ["id"]

    class OtherSerializer(NoteViewSet):
        serializer_class = TypeSerializer

    class SerializerOnly(NoteViewSet):
        queryset = None

    class Modelless(NoteViewSet):
        queryset = None
        serializer_class = None

    with routing_notes_to("other"):
        assert OtherSerializer().locked_write_database() == "other"
        assert SerializerOnly().locked_write_database() == "other"
        assert Modelless().locked_write_database() == DEFAULT_DB_ALIAS


def modelless_view():
    """A NoteViewSet naming its model only through get_queryset() and
    get_serializer_class() (common DRF idioms): its transaction opens on the
    default database."""
    from .urls import NoteSerializer, NoteViewSet

    class Modelless(NoteViewSet):
        queryset = None
        serializer_class = None

        def get_queryset(self):
            return Note.objects.all()

        def get_serializer_class(self):
            return NoteSerializer

    return Modelless


@pytest.mark.django_db
@pytest.mark.parametrize(
    "headers",
    [{"HTTP_IF_MATCH": "list"}, {"HTTP_IF_NONE_MATCH": "*"}],
    ids=["conditional-create", "unlocked-if-none-match-any"],
)
def test_a_post_on_another_database_than_its_models_is_refused_first(headers):
    # Refused in initial, before the lock and the handler, whether or not a
    # lock is taken: the transaction would roll back nothing of a write on
    # the model's database.
    from django.core.exceptions import ImproperlyConfigured

    a_note()
    if headers.get("HTTP_IF_MATCH") == "list":
        headers = {"HTTP_IF_MATCH": superuser_client().get(list_url())["ETag"]}
    view = modelless_view().as_view({"post": "create"})
    request = authenticated_post(list_url(), **headers)
    with (
        routing_notes_to("other"),
        recorded_collection_locks() as taken,
        pytest.raises(ImproperlyConfigured, match="return 'other'"),
    ):
        view(request)
    assert taken == []
    assert created() == 0


@pytest.mark.django_db
def test_a_locked_write_on_another_database_than_its_models_is_refused():
    from django.core.exceptions import ImproperlyConfigured

    note = a_note()
    etag = superuser_client().get(detail(note))["ETag"]
    request = APIRequestFactory().put(
        detail(note), {"text": "changed"}, format="json", HTTP_IF_MATCH=etag
    )
    force_authenticate(request, UserFactory(is_superuser=True, is_staff=True))
    view = modelless_view().as_view({"put": "update"})
    with (
        routing_notes_to("other"),
        pytest.raises(ImproperlyConfigured, match="return 'other'"),
    ):
        view(request, slug=str(note.pk))
    assert Note.objects.get(pk=note.pk).text == "x"


@pytest.mark.django_db(databases=["default", "other"])
def test_a_view_selecting_its_database_by_hand_serves_its_locked_writes():
    # Django's "manually selecting a database": no router, get_queryset()
    # uses() the database, and locked_write_database() returns it.
    from .urls import NoteViewSet

    class OnOther(NoteViewSet):
        queryset = None

        def get_queryset(self):
            return Note.objects.using("other")

        def locked_write_database(self):
            return "other"

    reverse("generic-notes-list")
    note = Note.objects.using("other").create(
        text="x", rbac_context="", etag=uuid4().hex, modified_at=now()
    )
    request = APIRequestFactory().put(
        detail(note),
        {"text": "changed"},
        format="json",
        HTTP_IF_MATCH=quote_etag(note.etag),
    )
    force_authenticate(request, UserFactory(is_superuser=True, is_staff=True))
    response = OnOther.as_view({"put": "update"})(request, slug=str(note.pk))
    assert response.status_code == 200, response.data
    assert Note.objects.using("other").get(pk=note.pk).text == "changed"


# The same record, reached by a plain handler, a function view, a dropped
# answer, a redirect, and a view on another database.


@pytest.mark.django_db
@pytest.mark.parametrize(
    "if_match, status_code", [('"custom"', 201), ('"other"', 412)], ids=["its", "other"]
)
@pytest.mark.parametrize("handler", ["own", "create-override"])
def test_a_plain_handler_reaching_a_create_with_its_own_meta_validators(
    handler, if_match, status_code
):
    # An undecorated handler, or create override, reaching scrudful's create
    # (the outer scrudful handler): evaluated with the viewset's own Meta
    # list validators, once.
    from .urls import CustomListEtagNoteViewSet

    class Plain(CustomListEtagNoteViewSet):
        def import_note(self, request):
            return self.create(request)

    class Override(CustomListEtagNoteViewSet):
        def create(self, request):
            return super().create(request)

    if handler == "own":
        view = Plain.as_view({"post": "import_note"})
    else:
        view = Override.as_view({"post": "create"})
    a_note()
    response = view(authenticated_post(list_url(), HTTP_IF_MATCH=if_match))
    assert response.status_code == status_code, response.data
    assert created() == (1 if status_code == 201 else 0)


@pytest.mark.django_db
@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "stale"])
def test_a_create_a_scrudful_function_view_delegates_to_is_refused(fresh):
    # scrudful skips the viewset's create (it already handles the request):
    # nothing evaluates it, whatever the handler's type says.
    from django.core.exceptions import ImproperlyConfigured

    from .urls import NoteViewSet

    @scrudful()
    def outer(request):
        return NoteViewSet.as_view({"post": "create"})(request)

    a_note()
    etag = superuser_client().get(list_url())["ETag"] if fresh else '"stale"'
    with pytest.raises(ImproperlyConfigured, match="without evaluating"):
        outer(authenticated_post(list_url(), HTTP_IF_MATCH=etag))
    assert created() == 0


@pytest.mark.django_db
def test_a_dropped_412_of_list_precondition_response_is_refused():
    # The handler evaluates, and creates whatever it found: refused, rolled
    # back.
    from django.core.exceptions import ImproperlyConfigured

    from .urls import ACTION, NoteViewSet

    class Dropping(NoteViewSet):
        @action(detail=False, methods=["post"])
        @scrudful(**ACTION)
        def import_note(self, request):
            self.list_precondition_response(request)
            return self.create(request)

    a_note()
    client = superuser_client()
    etag = client.get(list_url())["ETag"]
    view = Dropping.as_view({"post": "import_note"})
    assert view(authenticated_post(list_url(), HTTP_IF_MATCH=etag)).status_code == 201
    with pytest.raises(ImproperlyConfigured, match="though its preconditions failed"):
        view(authenticated_post(list_url(), HTTP_IF_MATCH=etag))  # stale now
    assert created() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("evaluates", [True, False], ids=["evaluated", "not"])
def test_a_redirect_from_a_conditional_post_commits_or_is_refused(evaluates):
    # A 3xx is no error: it commits (a client follows its Location to what
    # was written), unless nothing evaluated the preconditions.
    from django.core.exceptions import ImproperlyConfigured

    from .urls import ACTION, NoteViewSet

    class Redirecting(NoteViewSet):
        @action(detail=False, methods=["post"])
        @scrudful(**ACTION)
        def import_note(self, request):
            if evaluates:
                failed = self.list_precondition_response(request)
                if failed is not None:
                    return failed
            note = Note.objects.create(text="new", etag=uuid4().hex, modified_at=now())
            return Response(status=303, headers={"Location": f"/notes/{note.pk}/"})

    a_note()
    etag = superuser_client().get(list_url())["ETag"]
    view = Redirecting.as_view({"post": "import_note"})
    request = authenticated_post(list_url(), HTTP_IF_MATCH=etag)
    if evaluates:
        assert view(request).status_code == 303
        assert created() == 1
    else:
        with pytest.raises(ImproperlyConfigured, match="with 303 without"):
            view(request)
        assert created() == 0


@pytest.mark.django_db(databases=["default", "other"])
def test_a_view_reading_from_a_replica_serves_its_locked_writes():
    # get_queryset() reads elsewhere (using()), the router writes on the
    # default database, as locked_write_database (inherited) says: no
    # mismatch. Inside the transaction the lookup and the lock read it.
    from .urls import NoteViewSet

    class ReplicaNotes(NoteViewSet):
        def get_queryset(self):
            return Note.objects.using("other")

    note = a_note()
    request = APIRequestFactory().put(
        detail(note),
        {"text": "changed"},
        format="json",
        HTTP_IF_MATCH=quote_etag(note.etag),
    )
    force_authenticate(request, UserFactory(is_superuser=True, is_staff=True))
    response = ReplicaNotes.as_view({"put": "update"})(request, slug=str(note.pk))
    assert response.status_code == 200, response.data
    assert Note.objects.get(pk=note.pk).text == "changed"
    assert not Note.objects.using("other").exists()


@pytest.mark.django_db(databases=["default", "other"])
def test_a_model_the_router_writes_elsewhere_is_locked_and_written_there():
    from .urls import NoteViewSet

    reverse("generic-notes-list")
    note = Note.objects.using("other").create(
        text="x", rbac_context="", etag=uuid4().hex, modified_at=now()
    )
    request = APIRequestFactory().put(
        detail(note),
        {"text": "changed"},
        format="json",
        HTTP_IF_MATCH=quote_etag(note.etag),
    )
    force_authenticate(request, UserFactory(is_superuser=True, is_staff=True))
    with routing_notes_to("other"):
        response = NoteViewSet.as_view({"put": "update"})(request, slug=str(note.pk))
    assert response.status_code == 200, response.data
    assert Note.objects.using("other").get(pk=note.pk).text == "changed"


@pytest.mark.django_db
def test_the_write_database_is_checked_after_the_request_is_authorized():
    # The check reads get_queryset(): an anonymous request is refused (401)
    # first, not answered with the misconfiguration.
    note = a_note()
    request = APIRequestFactory().put(
        detail(note), {"text": "changed"}, format="json", HTTP_IF_MATCH='"x"'
    )
    with routing_notes_to("other"):
        response = modelless_view().as_view({"put": "update"})(
            request, slug=str(note.pk)
        )
    assert response.status_code in (401, 403), response.data
    assert Note.objects.get(pk=note.pk).text == "x"


def old_modification(view_instance, request, **kwargs):
    return now() - timedelta(days=1)


@pytest.mark.django_db
@pytest.mark.parametrize("validator", ["etag", "last-modified"])
@pytest.mark.parametrize("passes", [True, False], ids=["passes", "fails"])
def test_an_action_with_one_list_validator_of_its_own_is_evaluated(validator, passes):
    # Either callback alone records the evaluation (gate_uniform_errors
    # wraps each), and the action's method its pass.
    from .urls import ACTION, NoteViewSet, custom_list_etag

    options = (
        {"list_etag_func": custom_list_etag}
        if validator == "etag"
        else {"list_last_modified_func": old_modification}
    )

    class OneValidator(NoteViewSet):
        @action(detail=False, methods=["post"])
        @scrudful(**ACTION, **options)
        def import_note(self, request):
            return self.create(request)

    if validator == "etag":
        headers = {"HTTP_IF_MATCH": '"custom"' if passes else '"other"'}
    else:
        when = now() + timedelta(days=1 if passes else -2)
        headers = {"HTTP_IF_UNMODIFIED_SINCE": http_date(when.timestamp())}
    a_note()
    view = OneValidator.as_view({"post": "import_note"})
    response = view(authenticated_post(list_url(), **headers))
    assert response.status_code == (201 if passes else 412), response.data
    assert created() == (1 if passes else 0)


@pytest.mark.django_db
def test_a_handler_dropping_scrudfuls_412_is_refused_and_rolled_back():
    # A handler of its own (undecorated) writes, then calls the scrudful
    # create, which answers 412, and answers 200: the evaluation scrudful
    # started never passed.
    from django.core.exceptions import ImproperlyConfigured

    from .urls import NoteViewSet

    class Bulk(NoteViewSet):
        def import_note(self, request):
            Note.objects.create(text="audit", etag=uuid4().hex, modified_at=now())
            inner = self.create(request)
            return Response({"inner": inner.status_code})

    a_note()
    with pytest.raises(ImproperlyConfigured, match="though its preconditions failed"):
        Bulk.as_view({"post": "import_note"})(
            authenticated_post(list_url(), HTTP_IF_MATCH='"stale"')
        )
    assert created() == 0
    assert created("audit") == 0


@pytest.mark.django_db
def test_a_list_callback_that_raises_is_no_evaluation():
    # The first callback returns, the second raises (a missing collection,
    # say); the handler catches it and writes: nothing passed, refused.
    from django.core.exceptions import ImproperlyConfigured
    from scrudful.rest_framework import scrudful_viewset

    from scrud_django.mixins.view_mixins import ScrudfulMeta

    from .urls import NoteViewSet

    def missing(view_instance, request, **kwargs):
        raise Http404

    @scrudful_viewset
    class Raising(NoteViewSet):
        class Meta(ScrudfulMeta):
            list_last_modified_func = missing

    class Catching(Raising):
        def import_note(self, request):
            try:
                return self.create(request)
            except Http404:
                Note.objects.create(text="new", etag=uuid4().hex, modified_at=now())
                return Response({}, status=200)

    a_note()
    etag = superuser_client().get(list_url())["ETag"]
    with pytest.raises(ImproperlyConfigured, match="could not be evaluated"):
        Catching.as_view({"post": "import_note"})(
            authenticated_post(list_url(), HTTP_IF_MATCH=etag)
        )
    assert created() == 0


@pytest.mark.django_db
def test_own_meta_validators_without_the_lock_are_refused():
    # An initial that skips super() takes no lock: the viewset's own Meta
    # list callbacks are refused as scrud's are, before they read anything.
    from django.core.exceptions import ImproperlyConfigured
    from rest_framework.views import APIView

    from .urls import CustomListEtagNoteViewSet

    class Bypassing(CustomListEtagNoteViewSet):
        def initial(self, request, *args, **kwargs):
            APIView.initial(self, request, *args, **kwargs)

    a_note()
    with (
        recorded_collection_locks() as taken,
        pytest.raises(ImproperlyConfigured, match="outside a conditional create"),
    ):
        Bypassing.as_view({"post": "create"})(
            authenticated_post(list_url(), HTTP_IF_MATCH='"custom"')
        )
    assert taken == []
    assert created() == 0


@pytest.mark.django_db
def test_a_reused_view_forgets_a_failure_of_its_last_post():
    # Served by an action that evaluates with list_precondition_response, not
    # by scrudful's create (which would record its own evaluation over
    # whatever leaked).
    from .urls import NoteImportViewSet

    a_note()
    actions = {"post": "import_note"}
    view = serve(NoteImportViewSet, actions, authenticated_post(list_url()))
    assert view.response.status_code == 201
    view._scrud_list_preconditions_failed = True
    etag = superuser_client().get(list_url())["ETag"]
    request = authenticated_post(list_url(), HTTP_IF_MATCH=etag)
    view.request = request
    assert view.dispatch(request).status_code == 201
    assert created() == 2


@pytest.mark.django_db
def test_an_evaluation_passing_after_a_dropped_failure_is_still_refused():
    # scrudful's create fails the request (the custom Meta ETag is not the
    # one sent), the handler drops that 412, evaluates again with
    # list_precondition_response (scrud's validators, which pass) and
    # creates: the failure stays recorded.
    from django.core.exceptions import ImproperlyConfigured

    from .urls import CustomListEtagNoteViewSet, NoteViewSet

    class Retrying(CustomListEtagNoteViewSet):
        def import_note(self, request):
            assert self.create(request).status_code == 412
            assert self.list_precondition_response(request) is None
            return NoteViewSet.create.view_method(self, request)

    a_note()
    etag = superuser_client().get(list_url())["ETag"]  # scrud's, not "custom"
    with pytest.raises(ImproperlyConfigured, match="though its preconditions failed"):
        Retrying.as_view({"post": "import_note"})(
            authenticated_post(list_url(), HTTP_IF_MATCH=etag)
        )
    assert created() == 0
