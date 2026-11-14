"""A POST to a list evaluates its preconditions (RFC 9110 §13.2.2:
``If-Match``, else ``If-Unmodified-Since``, then ``If-None-Match``) against
the list's validators as the requester would GET them (their RBAC-filtered
list's ``ETag`` and ``Last-Modified``): a failed one is a 412 and nothing is
created, a malformed one a 400, as on the detail routes. A POST without
preconditions is unchanged. (scrudful 0.9 evaluates them before the
``create`` handler runs, against ``Meta.list_etag_func`` and
``Meta.list_last_modified_func``, which scrud gives the requester's list:
``ListValidatorsMixin.create_list_validators``.)"""

import threading
import time
from contextlib import contextmanager
from unittest import mock
from uuid import uuid4

import pytest
from django.conf import settings
from django.db import connection, connections, transaction
from django.db.models import QuerySet
from django.db.models.signals import post_delete, post_save
from django.test import override_settings
from django.urls import reverse
from django.utils.http import http_date
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.models import Resource, ResourceType
from scrud_django.registration import ResourceRegistration, json_resource_type_spec

from .factories import UserFactory
from .front_ends import api_url
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES
from .resource_types import PARTNER_PROFILES_TYPE

PP = TYPE_URI_PARTNER_PROFILES
PP_LIST = TYPE_URI_LIST_PARTNER_PROFILES
WRITER = {"http.GET": [PP, PP_LIST], "http.POST": [PP, PP_LIST]}
BODY = {"name": "new", "rbac_context": "a"}


def client_with(*grants):
    user = UserFactory()
    for definition, context in grants:
        role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
        role.save()
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=context,
            etag=uuid4().hex,
            modified_at=now(),
        ).save()
    client = APIClient()
    client.force_login(user)
    return client


def url(front_end="drf"):
    """The partner profiles list on `front_end` (the ``front_end`` fixture's
    URLconf)."""
    return api_url(front_end, "partner-profiles-list")


def profile(name, context="a"):
    # Registered as either front end registers it (a stored type: no write).
    json_resource_type_spec(**PARTNER_PROFILES_TYPE).register()
    return ResourceRegistration.register(
        {"name": name, "rbac_context": context}, "partner-profiles"
    )


def created():
    return Resource.objects.filter(content__name="new").count()


def post(client, front_end, **headers):
    return client.post(url(front_end), BODY, format="json", **headers)


@pytest.fixture
def writer():
    profile("existing")
    return client_with((WRITER, "a"))


@pytest.mark.django_db
def test_if_match_with_the_lists_etag_creates(front_end, writer):
    etag = writer.get(url(front_end))["ETag"]
    response = post(writer, front_end, HTTP_IF_MATCH=etag)
    assert response.status_code == 201, response.content
    assert created() == 1


@pytest.mark.django_db
def test_if_match_with_another_etag_is_412_and_creates_nothing(front_end, writer):
    response = post(writer, front_end, HTTP_IF_MATCH='"nope"')
    assert response.status_code == 412, response.content
    assert response["Content-Type"].startswith("application/problem+json")
    problem = response.json()
    assert (problem["status"], problem["code"]) == (412, "precondition_failed")
    assert "problem-details.schema.json" in response["Link"], response["Link"]
    assert created() == 0


@pytest.mark.django_db
def test_if_match_with_a_stale_etag_is_412(front_end, writer):
    etag = writer.get(url(front_end))["ETag"]
    profile("another")
    response = post(writer, front_end, HTTP_IF_MATCH=etag)
    assert response.status_code == 412, response.content
    assert created() == 0


@pytest.mark.django_db
def test_if_match_any_creates(front_end, writer):
    response = post(writer, front_end, HTTP_IF_MATCH="*")
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_if_unmodified_since_is_evaluated(front_end, writer):
    last_modified = writer.get(url(front_end))["Last-Modified"]
    earlier = http_date(
        Resource.objects.get(content__name="existing").modified_at.timestamp() - 60
    )
    response = post(writer, front_end, HTTP_IF_UNMODIFIED_SINCE=earlier)
    assert response.status_code == 412, response.content
    assert created() == 0
    response = post(writer, front_end, HTTP_IF_UNMODIFIED_SINCE=last_modified)
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_if_none_match_on_the_existing_list_is_412(front_end, writer):
    etag = writer.get(url(front_end))["ETag"]
    for value in ("*", etag):
        response = post(writer, front_end, HTTP_IF_NONE_MATCH=value)
        assert response.status_code == 412, (value, response.content)
    assert created() == 0
    response = post(writer, front_end, HTTP_IF_NONE_MATCH='"other"')
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_a_malformed_if_match_is_400(front_end, writer):
    response = post(writer, front_end, HTTP_IF_MATCH="nope")
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "malformed_precondition"
    assert created() == 0


@pytest.mark.django_db
def test_the_validators_are_the_requesters_list(front_end, writer):
    # A write the requester cannot see does not change their list's ETag:
    # their If-Match still passes.
    other = profile("elsewhere", context="b")
    etag = writer.get(url(front_end))["ETag"]
    ResourceRegistration.update(
        {"name": "elsewhere, changed", "rbac_context": "b"},
        "partner-profiles",
        other.pk,
    )
    response = post(writer, front_end, HTTP_IF_MATCH=etag)
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_without_uniform_errors_a_412_is_scrudfuls_native_body(front_end, writer):
    with override_settings(
        SCOPED_RBAC=dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=False)
    ):
        response = post(writer, front_end, HTTP_IF_MATCH='"nope"')
    assert response.status_code == 412, response.content
    assert response["Content-Type"].startswith("application/json")
    assert response.json() == {"detail": "Precondition failed."}


@pytest.mark.django_db
def test_a_create_without_preconditions_is_unchanged(front_end, writer):
    response = post(writer, front_end)
    assert response.status_code == 201, response.content
    assert response.has_header("ETag") and response.has_header("Location")


@pytest.mark.django_db
def test_authorization_comes_before_preconditions(front_end):
    profile("existing")
    reader = client_with(({"http.GET": [PP, PP_LIST]}, "a"))
    with recorded_type_locks() as events:
        response = post(reader, front_end, HTTP_IF_MATCH='"nope"')
    assert response.status_code == 403, response.content
    # Refused before the type's row is locked (a row lock leaves nothing to
    # check afterwards; the recorder sees the call).
    assert type_locks(events) == [], events


# Past SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"] the list's validators cannot be
# computed (a GET of the list is a 400 conditional_list_too_large). A
# precondition that needs them gets that 400 too; one that does not (an
# ``If-Match: *`` or ``If-None-Match: *``, decided by the list existing) is
# evaluated without them.

CONDITIONAL_WRITER = {
    "http.GET": {PP: {"condition": 'name != "zzz"'}, PP_LIST: True},
    "http.POST": [PP, PP_LIST],
}


@pytest.fixture
def capped_writer(front_end):
    for name in ("one", "two", "three"):
        profile(name)
    capped = dict(settings.SCOPED_RBAC, CONDITIONAL_LIST_MAX_ROWS=2)
    with override_settings(SCOPED_RBAC=capped):
        client = client_with((CONDITIONAL_WRITER, "a"))
        response = client.get(url(front_end))
        assert response.status_code == 400, response.content
        assert response.json()["code"] == "conditional_list_too_large"
        yield client


@pytest.mark.django_db
def test_if_match_any_past_the_cap_creates_as_an_unconditional_post(
    front_end, capped_writer
):
    assert post(capped_writer, front_end).status_code == 201
    response = post(capped_writer, front_end, HTTP_IF_MATCH="*")
    assert response.status_code == 201, response.content
    assert created() == 2


@pytest.mark.django_db
def test_if_none_match_any_past_the_cap_is_412(front_end, capped_writer):
    response = post(capped_writer, front_end, HTTP_IF_NONE_MATCH="*")
    assert response.status_code == 412, response.content
    assert created() == 0


@pytest.mark.django_db
def test_a_precondition_that_needs_the_validators_past_the_cap_is_400(
    front_end, capped_writer
):
    for headers in (
        {"HTTP_IF_MATCH": '"x"'},
        {"HTTP_IF_NONE_MATCH": '"x"'},
        {"HTTP_IF_UNMODIFIED_SINCE": "Thu, 01 Jan 2099 00:00:00 GMT"},
        {"HTTP_IF_MATCH": "*", "HTTP_IF_NONE_MATCH": '"x"'},
    ):
        response = post(capped_writer, front_end, **headers)
        assert response.status_code == 400, (headers, response.content)
        assert response.json()["code"] == "conditional_list_too_large", headers
    assert created() == 0


# A conditional POST that needs the list's validators evaluates them and
# creates in one transaction holding the type's ResourceType row (FOR NO KEY
# UPDATE where the database has it), so that two of them run one at a time;
# the other writes of the type do not take that lock first and cannot
# deadlock with it.


@contextmanager
def recorded_type_locks():
    """The ``select_for_update`` calls on ``ResourceType`` (their ``no_key``)
    and the INSERTs run, in order: ``("lock", no_key)`` / ``("insert", sql)``."""
    events = []
    original = QuerySet.select_for_update

    def select_for_update(self, *args, **kwargs):
        if self.model is ResourceType:
            events.append(("lock", kwargs.get("no_key", False)))
        return original(self, *args, **kwargs)

    def record(execute, sql, params, many, context):
        if sql.startswith("INSERT"):
            events.append(("insert", sql))
        return execute(sql, params, many, context)

    with (
        mock.patch.object(QuerySet, "select_for_update", select_for_update),
        connection.execute_wrapper(record),
    ):
        yield events


def type_locks(events):
    return [event for event in events if event[0] == "lock"]


@pytest.mark.django_db
def test_a_conditional_create_locks_the_type_row_before_it_writes(front_end, writer):
    etag = writer.get(url(front_end))["ETag"]
    with recorded_type_locks() as events:
        response = post(writer, front_end, HTTP_IF_MATCH=etag)
    assert response.status_code == 201, response.content
    no_key = connection.features.has_select_for_no_key_update
    assert type_locks(events) == [("lock", no_key)], events
    assert events[0][0] == "lock", events  # before any INSERT


@pytest.mark.django_db
def test_a_conditional_create_locks_the_row_of_the_type_it_saves_under(
    front_end, writer
):
    # The lock serializes the creates of the type only if it is the row the
    # resource is saved under (each type revision is a row of its own).
    # Recorded from the query's select_for_update flag, which SQLite (no row
    # locks) keeps too.
    etag = writer.get(url(front_end))["ETag"]
    locked = []
    original = QuerySet._fetch_all

    def fetch_all(self):
        fresh = self._result_cache is None
        original(self)
        if fresh and self.model is ResourceType and self.query.select_for_update:
            locked.extend(row.pk for row in self._result_cache)

    with mock.patch.object(QuerySet, "_fetch_all", fetch_all):
        response = post(writer, front_end, HTTP_IF_MATCH=etag)
    assert response.status_code == 201, response.content
    saved = Resource.objects.get(content__name="new")
    assert locked == [saved.resource_type_id], locked


@pytest.mark.django_db
@pytest.mark.parametrize(
    "headers", [{}, {"HTTP_IF_MATCH": "*"}], ids=["unconditional", "if-match-any"]
)
def test_a_create_that_needs_no_validators_takes_no_type_lock(
    front_end, writer, headers
):
    with recorded_type_locks() as events:
        response = post(writer, front_end, **headers)
    assert response.status_code == 201, response.content
    assert type_locks(events) == [], events


@pytest.mark.django_db
def test_a_conditional_create_of_a_workflow_policy_locks_its_type_once(front_end):
    from scrud_django.registration import workflow_policy_resource_type

    workflow_policy_resource_type()
    api_url(front_end, "todos-list")
    client = APIClient()
    client.force_login(UserFactory(is_superuser=True, is_staff=True))
    policy = {
        "resource_type": "tests://ToDo",
        "rbac_context": "pol",
        "initial_states": {"any": "true"},
        "states": {},
        "transitions": {},
    }
    with recorded_type_locks() as events:
        response = client.post(
            api_url(front_end, "workflow-policies-list"),
            policy,
            format="json",
            HTTP_IF_NONE_MATCH='"none"',
        )
    assert response.status_code == 201, response.content
    no_key = connection.features.has_select_for_no_key_update
    assert type_locks(events) == [("lock", no_key)], events
    assert events[0][0] == "lock", events


# PostgreSQL, MySQL and MariaDB: two connections.

TWO_WRITERS = pytest.mark.skipif(
    connection.vendor == "sqlite", reason="SQLite runs one writer at a time"
)


def superuser_client():
    client = APIClient(raise_request_exception=False)
    client.force_login(UserFactory(is_superuser=True, is_staff=True))
    return client


@contextmanager
def paused_after_evaluation(thread_name, front_end="drf", seconds=0.8):
    """The create in thread `thread_name` pauses for `seconds` right after
    its list preconditions passed (before it creates), on `front_end`: on
    DRF scrudful evaluates them before the ``create`` handler runs, which
    then calls ``create_resource``; on ninja the create operation evaluates
    them, then calls ``core.writes.create``. `timeline` records when
    (``paused``, ``resumed``) and `evaluated` is set as it pauses."""
    from scrud_django.core import writes
    from scrud_django.mixins.view_mixins import ResourceViewMixin

    target, name = {
        "drf": (ResourceViewMixin, "create_resource"),
        "ninja": (writes, "create"),
    }[front_end]
    original = getattr(target, name)
    evaluated = threading.Event()
    timeline = {}

    def pause():
        if threading.current_thread().name == thread_name and "paused" not in timeline:
            timeline["paused"] = time.monotonic()
            evaluated.set()
            time.sleep(seconds)
            timeline["resumed"] = time.monotonic()

    def create_resource(self, request, *args, **kwargs):
        pause()
        return original(self, request, *args, **kwargs)

    def create(*args, **kwargs):
        pause()
        return original(*args, **kwargs)

    paused = create_resource if front_end == "drf" else create
    with mock.patch.object(target, name, paused):
        yield evaluated, timeline


def run_threads(**functions):
    """Run each function in a thread of its name (its own connection); their
    results, or the exceptions they raised."""
    results = {}

    def run(name, function):
        try:
            results[name] = function()
        except Exception as exc:  # noqa: BLE001 - reported to the test
            results[name] = exc
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=run, args=(name, function), name=name)
        for name, function in functions.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    return results


@TWO_WRITERS
@pytest.mark.django_db(transaction=True)
def test_two_conditional_creates_with_one_etag_give_one_201_and_one_412(
    committed_types, front_end
):
    profile("existing")
    first_client, second_client = superuser_client(), superuser_client()
    etag = first_client.get(url(front_end))["ETag"]
    connections.close_all()

    def create(client, name):
        return client.post(
            url(front_end), dict(BODY, name=name), format="json", HTTP_IF_MATCH=etag
        ).status_code

    with paused_after_evaluation("first", front_end) as (evaluated, timeline):

        def second():
            assert evaluated.wait(10), "the first create never evaluated"
            timeline["second sent"] = time.monotonic()
            return create(second_client, "second")

        results = run_threads(
            first=lambda: create(first_client, "first"), second=second
        )
    # The forced schedule applied: the second POST was sent while the first,
    # its preconditions passed, had not created yet.
    assert timeline["paused"] < timeline["second sent"] < timeline["resumed"], timeline
    assert results == {"first": 201, "second": 412}, results
    names = list(
        Resource.objects.filter(resource_type__slug="partner-profiles").values_list(
            "content__name", flat=True
        )
    )
    assert sorted(names) == ["existing", "first"], names


def other_write(kind, stored, front_end="drf"):
    """A write of `kind` to the partner profile `stored` (or a new one), as
    `front_end` or server code makes it; returns what a success looks like
    and the function."""
    client = superuser_client()
    detail = api_url(front_end, "partner-profiles-detail", args=[stored.pk])
    if kind == "create":
        return (
            201,
            lambda: (
                client.post(
                    url(front_end),
                    {"name": "other", "rbac_context": "a"},
                    format="json",
                ).status_code
            ),
        )
    if kind == "put":
        # Moving it out of "a" stamps a removal (a row referring to the type).
        return (
            200,
            lambda: (
                client.put(
                    detail,
                    {"name": "moved", "rbac_context": "b"},
                    format="json",
                    HTTP_IF_MATCH="*",
                ).status_code
            ),
        )
    if kind == "delete":
        return 204, lambda: client.delete(detail, HTTP_IF_MATCH="*").status_code

    def queryset_delete():
        with transaction.atomic():
            return Resource.objects.filter(pk=stored.pk).delete()[0] and 1

    return 1, queryset_delete


@contextmanager
def other_paused_holding_its_locks(kind, seconds=0.8):
    """The `kind` write pauses for `seconds` once it has written (Django's
    post_save or post_delete of the Resource, inside its transaction),
    holding its locks; `held` is set as it pauses."""
    signal = post_save if kind in ("create", "put") else post_delete
    held = threading.Event()
    timeline = {}

    def hold(sender, instance, **kwargs):
        if threading.current_thread().name == "other" and "paused" not in timeline:
            timeline["paused"] = time.monotonic()
            held.set()
            time.sleep(seconds)
            timeline["resumed"] = time.monotonic()

    signal.connect(hold, sender=Resource, weak=False, dispatch_uid="k5-hold")
    try:
        yield held, timeline
    finally:
        signal.disconnect(sender=Resource, dispatch_uid="k5-hold")


@TWO_WRITERS
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["create", "put", "delete", "queryset-delete"])
@pytest.mark.parametrize("first", ["conditional", "other"])
def test_a_conditional_create_and_another_write_do_not_deadlock(
    kind, first, committed_types, front_end
):
    stored = profile("existing")
    expected, other = other_write(kind, stored, front_end)
    conditional_client = superuser_client()
    connections.close_all()

    def conditional():
        # Needs the validators (so takes the type lock); passes whatever the
        # list holds.
        return conditional_client.post(
            url(front_end), BODY, format="json", HTTP_IF_NONE_MATCH='"none"'
        ).status_code

    if first == "conditional":
        pause = paused_after_evaluation("conditional", front_end)
        starts, waits = conditional, other
    else:
        pause = other_paused_holding_its_locks(kind)
        starts, waits = other, conditional
    with pause as (event, timeline):

        def after():
            assert event.wait(10), "the first write never paused"
            timeline["second sent"] = time.monotonic()
            return waits()

        names = (
            ("conditional", "other")
            if first == "conditional"
            else ("other", "conditional")
        )
        results = run_threads(**{names[0]: starts, names[1]: after})
    assert timeline["paused"] < timeline["second sent"] < timeline["resumed"], timeline
    assert results == {"conditional": 201, "other": expected}, results


# DRF-only: scoped-rbac's DRF role list (its ninja one is scoped-rbac's own).
@pytest.mark.django_db
def test_scoped_rbacs_own_lists_evaluate_a_creates_preconditions():
    # scoped-rbac 0.12 (#62) evaluates them on its role and role-assignment
    # lists, which scrud_django.urls mounts (formerly scrud's follow-up
    # scrudful-create-preconditions).
    client = APIClient()
    client.force_authenticate(UserFactory(is_superuser=True, is_staff=True))
    role = {"rbac_context": "", "definition": {"http.GET": [PP]}}
    response = client.post(
        reverse("role-list"), role, format="json", HTTP_IF_MATCH='"nope"'
    )
    assert response.status_code == 412, response.content
    assert not Role.objects.filter(definition={"http.GET": [PP]}).exists()
    etag = client.get(reverse("role-list"))["ETag"]
    response = client.post(
        reverse("role-list"), role, format="json", HTTP_IF_MATCH=etag
    )
    assert response.status_code == 201, response.content


# DRF-only, to the end of the module: DRF viewsets of tests/urls.py and
# DRF internals (ninja serves no custom viewset; its create is
# routers.type_router's).

# A POST @action of the list that only calls create
# (tests.urls.ImportingResourceViewSet.import_resource): scrudful evaluates
# nothing of the nested create, whose ResourceViewMixin.create evaluates the
# preconditions itself (list_precondition_response), under the type's lock.


def import_post(client, **headers):
    return client.post(
        reverse("importing-partner-profiles"), BODY, format="json", **headers
    )


@pytest.mark.django_db
def test_an_actions_create_of_a_stale_if_match_is_412(writer):
    etag = writer.get(url())["ETag"]
    profile("another")
    with recorded_type_locks() as events:
        response = import_post(writer, HTTP_IF_MATCH=etag)
    assert response.status_code == 412, response.content
    assert response["Content-Type"] == "application/problem+json"
    assert created() == 0
    no_key = connection.features.has_select_for_no_key_update
    assert type_locks(events) == [("lock", no_key)], events


@pytest.mark.django_db
def test_an_actions_create_with_the_lists_etag_creates_under_the_type_lock(writer):
    etag = writer.get(url())["ETag"]
    with recorded_type_locks() as events:
        response = import_post(writer, HTTP_IF_MATCH=etag)
    assert response.status_code == 201, response.content
    assert created() == 1
    no_key = connection.features.has_select_for_no_key_update
    assert type_locks(events) == [("lock", no_key)], events
    assert events[0][0] == "lock", events  # before any INSERT


@pytest.mark.django_db
@pytest.mark.parametrize(
    "headers, status_code, code",
    [
        ({"HTTP_IF_NONE_MATCH": "*"}, 412, None),
        ({"HTTP_IF_MATCH": "nope"}, 400, "malformed_precondition"),
    ],
    ids=["if-none-match-any", "malformed-if-match"],
)
def test_an_actions_create_evaluates_what_needs_no_validators(
    writer, headers, status_code, code
):
    with recorded_type_locks() as events:
        response = import_post(writer, **headers)
    assert response.status_code == status_code, response.content
    if code is not None:
        assert response.json()["code"] == code, response.content
    assert created() == 0
    assert type_locks(events) == [], events


# A viewset's own Meta list validators
# (tests.urls.CustomListEtagResourceViewSet): scrudful evaluates the create
# with them, and ResourceViewMixin.create does not evaluate it again against
# scrud's.


@pytest.mark.django_db
@pytest.mark.parametrize(
    "if_match, status_code", [('"custom"', 201), ('"other"', 412)], ids=["its", "other"]
)
def test_a_create_is_evaluated_once_with_its_own_meta_list_validators(
    writer, if_match, status_code
):
    custom = reverse("custom-etag-partner-profiles-list")
    assert writer.get(custom)["ETag"] == '"custom"'
    response = writer.post(custom, BODY, format="json", HTTP_IF_MATCH=if_match)
    assert response.status_code == status_code, response.content
    assert created() == (1 if status_code == 201 else 0)


# A POST handler of the view's own, no @action (tests.urls.ResourceBulkViewSet
# .bulk), calling create: the list's POST, evaluated by
# ResourceViewMixin.create under the type's lock.


def bulk_post(client, **headers):
    return client.post(reverse("bulk-partner-profiles"), BODY, format="json", **headers)


@pytest.mark.django_db
def test_a_handlers_create_of_a_stale_if_match_is_412_under_the_type_lock(writer):
    etag = writer.get(url())["ETag"]
    profile("another")
    with recorded_type_locks() as events:
        response = bulk_post(writer, HTTP_IF_MATCH=etag)
    assert response.status_code == 412, response.content
    assert created() == 0
    no_key = connection.features.has_select_for_no_key_update
    assert type_locks(events) == [("lock", no_key)], events


@pytest.mark.django_db
def test_a_handlers_create_with_the_lists_etag_creates_under_the_type_lock(writer):
    etag = writer.get(url())["ETag"]
    with recorded_type_locks() as events:
        response = bulk_post(writer, HTTP_IF_MATCH=etag)
    assert response.status_code == 201, response.content
    assert created() == 1
    no_key = connection.features.has_select_for_no_key_update
    assert type_locks(events) == [("lock", no_key)], events


@pytest.mark.django_db
def test_a_create_outside_the_conditional_creates_transaction_is_refused(writer):
    # A dispatch that skips LockedWriteMixin's: no transaction, no lock.
    # ResourceViewMixin.create's evaluation is refused, not run unlocked.
    from django.core.exceptions import ImproperlyConfigured
    from rest_framework import viewsets
    from rest_framework.test import APIRequestFactory, force_authenticate

    from .urls import ImportingResourceViewSet

    class NoDispatch(ImportingResourceViewSet):
        def dispatch(self, request, *args, **kwargs):
            return viewsets.GenericViewSet.dispatch(self, request, *args, **kwargs)

    etag = writer.get(url())["ETag"]
    request = APIRequestFactory().post(url(), BODY, format="json", HTTP_IF_MATCH=etag)
    force_authenticate(request, UserFactory(is_superuser=True, is_staff=True))
    view = NoDispatch.as_view(
        {"post": "import_resource"}, resource_type_name="partner-profiles"
    )
    with pytest.raises(ImproperlyConfigured, match="outside a conditional create"):
        view(request)
    assert created() == 0


def authenticated(request):
    from rest_framework.test import force_authenticate

    force_authenticate(request, UserFactory(is_superuser=True, is_staff=True))
    return request


@pytest.mark.django_db
@pytest.mark.parametrize(
    "if_match, status_code", [('"custom"', 201), ('"other"', 412)], ids=["its", "other"]
)
def test_a_plain_handlers_create_is_evaluated_once_with_its_own_meta_validators(
    writer, if_match, status_code
):
    # The undecorated handler reaches scrudful's create, which evaluates with
    # the viewset's own Meta list validators: ResourceViewMixin.create does
    # not evaluate again (against scrud's list ETag, which is not "custom").
    from rest_framework.test import APIRequestFactory

    from .urls import CustomListEtagResourceViewSet

    class Plain(CustomListEtagResourceViewSet):
        def import_resource(self, request):
            return self.create(request)

    view = Plain.as_view(
        {"post": "import_resource"}, resource_type_name="partner-profiles"
    )
    request = APIRequestFactory().post(
        url(), BODY, format="json", HTTP_IF_MATCH=if_match
    )
    response = view(authenticated(request))
    assert response.status_code == status_code, response.data
    assert created() == (1 if status_code == 201 else 0)


@pytest.mark.django_db
@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "stale"])
def test_a_create_a_scrudful_function_view_delegates_to_evaluates_itself(writer, fresh):
    # scrudful skips the viewset's create (it already handles the request);
    # ResourceViewMixin.create finds nothing evaluated, and evaluates.
    from rest_framework.test import APIRequestFactory
    from scrudful.rest_framework import scrudful

    from scrud_django.views import ResourceViewSet

    @scrudful()
    def outer(request):
        return ResourceViewSet.as_view(
            {"post": "create"}, resource_type_name="partner-profiles"
        )(request)

    etag = writer.get(url())["ETag"] if fresh else '"stale"'
    request = APIRequestFactory().post(url(), BODY, format="json", HTTP_IF_MATCH=etag)
    with recorded_type_locks() as events:
        response = outer(authenticated(request))
    assert response.status_code == (201 if fresh else 412), response.content
    assert created() == (1 if fresh else 0)
    no_key = connection.features.has_select_for_no_key_update
    assert type_locks(events) == [("lock", no_key)], events
