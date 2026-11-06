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
from unittest import mock
from uuid import uuid4

import pytest
from django.db import DEFAULT_DB_ALIAS, connection, connections, transaction
from django.db.models import Count, F, QuerySet, TextField, Value
from django.db.models.functions import Concat
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from scoped_rbac.models import Role, RoleAssignment

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
    response = reader_in("").post(
        list_url(), {"text": "new"}, format="json", HTTP_IF_MATCH='"nope"'
    )
    assert response.status_code == 403, response.content
    assert collection_holds() == before
    assert created() == 0


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
def test_a_conditional_post_outside_a_conditional_create_is_refused():
    # A POST mapped to a method that is neither create nor an @action, which
    # calls the scrudful create: its preconditions would be evaluated with
    # nothing locked, so they are refused (ImproperlyConfigured).
    from django.core.exceptions import ImproperlyConfigured

    from .urls import NoteViewSet

    class Delegating(NoteViewSet):
        def import_note(self, request):
            return self.create(request)

    a_note()
    etag = superuser_client().get(list_url())["ETag"]
    request = APIRequestFactory().post(
        list_url(), {"text": "new"}, format="json", HTTP_IF_MATCH=etag
    )
    force_authenticate(request, UserFactory(is_superuser=True, is_staff=True))
    with pytest.raises(ImproperlyConfigured, match="outside a conditional create"):
        Delegating.as_view({"post": "import_note"})(request)
    assert created() == 0


@TWO_WRITERS
@pytest.mark.django_db(transaction=True)
def test_two_generic_conditional_creates_with_one_etag_give_one_201_and_one_412(
    committed_types,
):
    # The first create pauses after its preconditions passed, holding the
    # collection lock; the second, sent meanwhile, waits for the lock, then
    # is evaluated against the list the first left.
    from .urls import NoteSerializer

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

    with mock.patch.object(NoteSerializer, "is_valid", is_valid):
        results = run_threads(
            first=lambda: create(first_client, "first"), second=second
        )
    assert timeline["paused"] < timeline["second sent"] < timeline["resumed"], timeline
    assert results == {"first": 201, "second": 412}, results
    texts = sorted(Note.objects.values_list("text", flat=True))
    assert texts == ["existing", "first"], texts
