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
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES

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
    client.force_authenticate(user)
    return client


def url():
    return reverse("partner-profiles-list")


def profile(name, context="a"):
    url()
    return ResourceRegistration.register(
        {"name": name, "rbac_context": context}, "partner-profiles"
    )


def created():
    return Resource.objects.filter(content__name="new").count()


def post(client, **headers):
    return client.post(url(), BODY, format="json", **headers)


@pytest.fixture
def writer():
    profile("existing")
    return client_with((WRITER, "a"))


@pytest.mark.django_db
def test_if_match_with_the_lists_etag_creates(writer):
    etag = writer.get(url())["ETag"]
    response = post(writer, HTTP_IF_MATCH=etag)
    assert response.status_code == 201, response.content
    assert created() == 1


@pytest.mark.django_db
def test_if_match_with_another_etag_is_412_and_creates_nothing(writer):
    response = post(writer, HTTP_IF_MATCH='"nope"')
    assert response.status_code == 412, response.content
    assert response["Content-Type"].startswith("application/problem+json")
    problem = response.json()
    assert (problem["status"], problem["code"]) == (412, "precondition_failed")
    assert "problem-details.schema.json" in response["Link"], response["Link"]
    assert created() == 0


@pytest.mark.django_db
def test_if_match_with_a_stale_etag_is_412(writer):
    etag = writer.get(url())["ETag"]
    profile("another")
    response = post(writer, HTTP_IF_MATCH=etag)
    assert response.status_code == 412, response.content
    assert created() == 0


@pytest.mark.django_db
def test_if_match_any_creates(writer):
    response = post(writer, HTTP_IF_MATCH="*")
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_if_unmodified_since_is_evaluated(writer):
    last_modified = writer.get(url())["Last-Modified"]
    earlier = http_date(
        Resource.objects.get(content__name="existing").modified_at.timestamp() - 60
    )
    response = post(writer, HTTP_IF_UNMODIFIED_SINCE=earlier)
    assert response.status_code == 412, response.content
    assert created() == 0
    response = post(writer, HTTP_IF_UNMODIFIED_SINCE=last_modified)
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_if_none_match_on_the_existing_list_is_412(writer):
    etag = writer.get(url())["ETag"]
    for value in ("*", etag):
        response = post(writer, HTTP_IF_NONE_MATCH=value)
        assert response.status_code == 412, (value, response.content)
    assert created() == 0
    response = post(writer, HTTP_IF_NONE_MATCH='"other"')
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_a_malformed_if_match_is_400(writer):
    response = post(writer, HTTP_IF_MATCH="nope")
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "malformed_precondition"
    assert created() == 0


@pytest.mark.django_db
def test_the_validators_are_the_requesters_list(writer):
    # A write the requester cannot see does not change their list's ETag:
    # their If-Match still passes.
    other = profile("elsewhere", context="b")
    etag = writer.get(url())["ETag"]
    ResourceRegistration.update(
        {"name": "elsewhere, changed", "rbac_context": "b"},
        "partner-profiles",
        other.pk,
    )
    response = post(writer, HTTP_IF_MATCH=etag)
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_without_uniform_errors_a_412_is_scrudfuls_native_body(writer):
    with override_settings(
        SCOPED_RBAC=dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=False)
    ):
        response = post(writer, HTTP_IF_MATCH='"nope"')
    assert response.status_code == 412, response.content
    assert response["Content-Type"].startswith("application/json")
    assert response.json() == {"detail": "Precondition failed."}


@pytest.mark.django_db
def test_a_create_without_preconditions_is_unchanged(writer):
    response = post(writer)
    assert response.status_code == 201, response.content
    assert response.has_header("ETag") and response.has_header("Location")


@pytest.mark.django_db
def test_authorization_comes_before_preconditions():
    profile("existing")
    reader = client_with(({"http.GET": [PP, PP_LIST]}, "a"))
    response = post(reader, HTTP_IF_MATCH='"nope"')
    assert response.status_code == 403, response.content


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
def capped_writer():
    for name in ("one", "two", "three"):
        profile(name)
    capped = dict(settings.SCOPED_RBAC, CONDITIONAL_LIST_MAX_ROWS=2)
    with override_settings(SCOPED_RBAC=capped):
        client = client_with((CONDITIONAL_WRITER, "a"))
        response = client.get(url())
        assert response.status_code == 400, response.content
        assert response.json()["code"] == "conditional_list_too_large"
        yield client


@pytest.mark.django_db
def test_if_match_any_past_the_cap_creates_as_an_unconditional_post(capped_writer):
    assert post(capped_writer).status_code == 201
    response = post(capped_writer, HTTP_IF_MATCH="*")
    assert response.status_code == 201, response.content
    assert created() == 2


@pytest.mark.django_db
def test_if_none_match_any_past_the_cap_is_412(capped_writer):
    response = post(capped_writer, HTTP_IF_NONE_MATCH="*")
    assert response.status_code == 412, response.content
    assert created() == 0


@pytest.mark.django_db
def test_a_precondition_that_needs_the_validators_past_the_cap_is_400(capped_writer):
    for headers in (
        {"HTTP_IF_MATCH": '"x"'},
        {"HTTP_IF_NONE_MATCH": '"x"'},
        {"HTTP_IF_UNMODIFIED_SINCE": "Thu, 01 Jan 2099 00:00:00 GMT"},
        {"HTTP_IF_MATCH": "*", "HTTP_IF_NONE_MATCH": '"x"'},
    ):
        response = post(capped_writer, **headers)
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
def test_a_conditional_create_locks_the_type_row_before_it_writes(writer):
    etag = writer.get(url())["ETag"]
    with recorded_type_locks() as events:
        response = post(writer, HTTP_IF_MATCH=etag)
    assert response.status_code == 201, response.content
    no_key = connection.features.has_select_for_no_key_update
    assert type_locks(events) == [("lock", no_key)], events
    assert events[0][0] == "lock", events  # before any INSERT


@pytest.mark.django_db
@pytest.mark.parametrize(
    "headers", [{}, {"HTTP_IF_MATCH": "*"}], ids=["unconditional", "if-match-any"]
)
def test_a_create_that_needs_no_validators_takes_no_type_lock(writer, headers):
    with recorded_type_locks() as events:
        response = post(writer, **headers)
    assert response.status_code == 201, response.content
    assert type_locks(events) == [], events


@pytest.mark.django_db
def test_a_conditional_create_of_a_workflow_policy_locks_its_type_once():
    from scrud_django.registration import workflow_policy_resource_type

    workflow_policy_resource_type()
    reverse("todos-list")
    client = APIClient()
    client.force_authenticate(UserFactory(is_superuser=True, is_staff=True))
    policy = {
        "resource_type": "tests://ToDo",
        "rbac_context": "pol",
        "initial_states": {"any": "true"},
        "states": {},
        "transitions": {},
    }
    with recorded_type_locks() as events:
        response = client.post(
            reverse("workflow-policies-list"),
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
    client.force_authenticate(UserFactory(is_superuser=True, is_staff=True))
    return client


@contextmanager
def paused_after_evaluation(thread_name, seconds=0.8):
    """The create in thread `thread_name` pauses for `seconds` right after
    its list preconditions passed (before it creates): scrudful evaluates
    them before the ``create`` handler runs, which then calls
    ``create_resource``. `timeline` records when (``paused``, ``resumed``)
    and `evaluated` is set as it pauses."""
    from scrud_django.mixins.view_mixins import ResourceViewMixin

    original = ResourceViewMixin.create_resource
    evaluated = threading.Event()
    timeline = {}

    def create_resource(self, request, *args, **kwargs):
        if threading.current_thread().name == thread_name and "paused" not in timeline:
            timeline["paused"] = time.monotonic()
            evaluated.set()
            time.sleep(seconds)
            timeline["resumed"] = time.monotonic()
        return original(self, request, *args, **kwargs)

    with mock.patch.object(ResourceViewMixin, "create_resource", create_resource):
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
    committed_types,
):
    profile("existing")
    first_client, second_client = superuser_client(), superuser_client()
    etag = first_client.get(url())["ETag"]
    connections.close_all()

    def create(client, name):
        return client.post(
            url(), dict(BODY, name=name), format="json", HTTP_IF_MATCH=etag
        ).status_code

    with paused_after_evaluation("first") as (evaluated, timeline):

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


def other_write(kind, stored):
    """A write of `kind` to the partner profile `stored` (or a new one), as
    the API or server code makes it; returns what a success looks like and
    the function."""
    client = superuser_client()
    detail = reverse("partner-profiles-detail", args=[stored.pk])
    if kind == "create":
        return (
            201,
            lambda: (
                client.post(
                    url(), {"name": "other", "rbac_context": "a"}, format="json"
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
    kind, first, committed_types
):
    stored = profile("existing")
    expected, other = other_write(kind, stored)
    conditional_client = superuser_client()
    connections.close_all()

    def conditional():
        # Needs the validators (so takes the type lock); passes whatever the
        # list holds.
        return conditional_client.post(
            url(), BODY, format="json", HTTP_IF_NONE_MATCH='"none"'
        ).status_code

    if first == "conditional":
        pause = paused_after_evaluation("conditional")
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
