"""Registering resource types when the URLconf is imported, and reading them.

A type whose registration fails with a database error at import (the
database unreachable for a moment, or not migrated yet, as when ``migrate``
imports the URLconf) keeps its routes: its registration is retried when the
type is next read (`registration.retry_registration`), so the process serves
it once the database is back, without a restart, and until then a request
for it fails (an error, not a 404). A slug's "latest revision" is the
highest number, not the last string."""

import json
import logging
import threading
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import (
    InterfaceError,
    OperationalError,
    ProgrammingError,
    connections,
    transaction,
)
from django.db.migrations.exceptions import BadMigrationError, NodeNotFoundError
from django.db.utils import ConnectionDoesNotExist
from django.test import override_settings
from django.urls import include, path, reverse
from django.utils.timezone import now
from rest_framework.test import APIClient

from scrud_django import registration, services
from scrud_django.models import Resource, ResourceType

from .factories import UserFactory
from .fixtures import unapply_a_scrud_migration

TYPE_URI = "tests://Deferred"
SLUG = "deferred-things"
SCHEMA = {
    "$schema": "http://json-schema.org/draft-04/schema",
    "type": "object",
    "properties": {"name": {"type": "string"}},
}

# The built-in types `scrud_django.urls` registers, by slug, and the module
# attributes `registration` caches them in.
BUILT_IN_SLUGS = ["json-schema", "json-ld", "indexing-policies", "workflow-policies"]
BUILT_IN_CACHES = [
    "JSON_SCHEMA_REGISTRATION_",
    "JSON_SCHEMA_RESOURCE_TYPE_",
    "JSON_LD_RESOURCE_TYPE_",
    "JSON_LD_REGISTRATION_",
    "INDEXING_POLICY_RESOURCE_TYPE_",
    "INDEXING_POLICY_REGISTRATION_",
    "WORKFLOW_POLICY_RESOURCE_TYPE_",
    "WORKFLOW_POLICY_REGISTRATION_",
]


def unreachable(execute, sql, params, many, context):
    """A connection's ``execute_wrapper``: the database is unreachable."""
    raise OperationalError("could not connect to server: Connection refused")


def resource_types_unreachable(execute, sql, params, many, context):
    """A connection's ``execute_wrapper``: the ``ResourceType`` table fails,
    the rest of the database (users, roles) works."""
    if ResourceType._meta.db_table in sql:
        raise OperationalError("could not connect to server: Connection refused")
    return execute(sql, params, many, context)


def type_connection():
    return connections[registration.type_db()]


def resource_types_denied(execute, sql, params, many, context):
    """A connection's ``execute_wrapper``: reading the ``ResourceType`` table
    is refused (a missing grant), the rest of the database works."""
    if ResourceType._meta.db_table in sql:
        raise ProgrammingError("permission denied for table resourcetype")
    return execute(sql, params, many, context)


@pytest.fixture(autouse=True)
def forgotten_probes():
    """No answer of `registration.migrated_and_reachable` is kept from one
    test to the next (`registration.register_or_defer` keeps them)."""
    registration._PROBED.clear()
    yield
    registration._PROBED.clear()


@pytest.fixture
def deferred_routes():
    """The routes `json_resource_type` returns for a new type while the
    database is unreachable (a process start whose URLconf import could not
    reach it), served beside the test URLconf's. Afterwards the pending
    registration and the service catalog entry are forgotten."""
    with type_connection().execute_wrapper(unreachable):
        urls = registration.json_resource_type(
            resource_type_uri=TYPE_URI,
            slug=SLUG,
            schema_func=lambda: json.dumps(SCHEMA),
        )
    urlconf = ModuleType("deferred_urls")
    urlconf.urlpatterns = [path("", include("tests.urls")), *urls]
    try:
        with override_settings(ROOT_URLCONF=urlconf):
            yield urls
    finally:
        registration._PENDING_REGISTRATIONS.pop(SLUG, None)
        services.services.pop(TYPE_URI, None)


@pytest.mark.django_db
def test_a_type_not_registered_at_import_is_served_once_the_database_is_back(
    deferred_routes, django_capture_on_commit_callbacks
):
    # The routes are there, though nothing was stored.
    assert [route.name for route in deferred_routes] == [
        f"{SLUG}-list",
        f"{SLUG}-detail",
    ]
    assert reverse(f"{SLUG}-list") == f"/{SLUG}/"
    assert not ResourceType.objects.filter(type_uri=TYPE_URI).exists()
    assert SLUG in registration._PENDING_REGISTRATIONS
    assert TYPE_URI in services.catalog()
    user = UserFactory(is_staff=True, is_superuser=True)

    # While the database still fails, a request for the type fails: the
    # registration is retried and its error raised (a 500), not a 404. (The
    # test settings propagate a view's exception out of the handler; served,
    # it is a 500.)
    failing = APIClient(raise_request_exception=False)
    failing.force_authenticate(user)
    with (
        override_settings(DEBUG_PROPAGATE_EXCEPTIONS=False),
        type_connection().execute_wrapper(resource_types_unreachable),
    ):
        response = failing.get(reverse(f"{SLUG}-list"))
    assert response.status_code == 500, response.content
    assert isinstance(response.exc_info[1], OperationalError), response.exc_info
    assert not ResourceType.objects.filter(type_uri=TYPE_URI).exists()
    assert SLUG in registration._PENDING_REGISTRATIONS

    # Once it is back, the same process registers the type on its next read
    # and serves it.
    client = APIClient()
    client.force_authenticate(user)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.get(reverse(f"{SLUG}-list"))
    assert response.status_code == 200, response.content
    assert response.json()["count"] == 0
    stored = ResourceType.objects.get(type_uri=TYPE_URI)
    assert stored.slug == SLUG
    assert stored.schema.content == SCHEMA
    assert SLUG not in registration._PENDING_REGISTRATIONS

    response = client.post(reverse(f"{SLUG}-list"), {"name": "a"}, format="json")
    assert response.status_code == 201, response.content
    created = Resource.objects.get(resource_type=stored)
    assert created.content == {"name": "a"}
    response = client.post(reverse(f"{SLUG}-list"), {"name": 1}, format="json")
    assert response.status_code == 400, response.content
    assert ResourceType.objects.filter(slug=SLUG).count() == 1


@pytest.mark.django_db
def test_a_pending_type_stored_under_another_slug_is_refused(deferred_routes):
    # Outside DEBUG, registration keeps a stored row's slug; the routes were
    # built for the new one, which no row has: refused, not a 404.
    ResourceType.objects.create(
        type_uri=TYPE_URI,
        slug="old-slug",
        revision="1",
        etag=uuid4().hex,
        modified_at=now(),
    )
    with pytest.raises(ImproperlyConfigured, match="old-slug"):
        registration.get_resource_type_for(SLUG)
    assert SLUG in registration._PENDING_REGISTRATIONS


@pytest.mark.django_db
def test_a_retried_registration_is_pending_until_its_transaction_commits(
    deferred_routes, django_capture_on_commit_callbacks
):
    # The first read of the type registers it inside a transaction (a
    # write's, or ATOMIC_REQUESTS'). Until that commits it stays pending: if
    # the transaction is rolled back (a refused write), the type is not
    # stored, and it is retried on its next read rather than forgotten.
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        registration.get_resource_type_for(SLUG)
    assert ResourceType.objects.filter(type_uri=TYPE_URI).exists()
    assert SLUG in registration._PENDING_REGISTRATIONS
    # The commit forgets it.
    for callback in callbacks:
        callback()
    assert SLUG not in registration._PENDING_REGISTRATIONS


class Refused(Exception):
    """A write refused after it read a pending type."""


@pytest.mark.django_db
def test_a_retried_registration_is_rolled_back_with_its_transaction(
    deferred_routes, django_capture_on_commit_callbacks, caplog
):
    # A write reads the pending type (registering it), then is refused: its
    # transaction is rolled back, the registration with it, and nothing says
    # it was registered.
    caplog.set_level(logging.INFO, logger="scrud_django.registration")
    resources = Resource.objects.count()
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with pytest.raises(Refused), transaction.atomic():
            registration.get_resource_type_for(SLUG)
            assert ResourceType.objects.filter(type_uri=TYPE_URI).exists()
            raise Refused
    assert callbacks == []
    assert not ResourceType.objects.filter(type_uri=TYPE_URI).exists()
    assert Resource.objects.count() == resources  # Nor its schema.
    assert SLUG in registration._PENDING_REGISTRATIONS
    assert registration_logs(caplog) == []
    # The next read registers it again, and it is forgotten (and logged as
    # registered) once that commits.
    with django_capture_on_commit_callbacks(execute=True):
        registered = registration.get_resource_type_for(SLUG)
    assert registered.type_uri == TYPE_URI
    assert ResourceType.objects.get(slug=SLUG).schema.content == SCHEMA
    assert SLUG not in registration._PENDING_REGISTRATIONS
    assert registration_logs(caplog) == [
        (logging.INFO, f"Registered resource type {SLUG!r}, which was pending.")
    ]


@pytest.mark.django_db
def test_a_pending_registration_runs_without_the_process_lock(
    deferred_routes, django_capture_on_commit_callbacks
):
    # A registration writes rows, whose locks last until the caller's
    # transaction commits. Run under the process lock, a thread waiting in
    # the database for those row locks would hold it, and the thread holding
    # them would block on it reading another pending type: a deadlock the
    # database cannot detect. The lock guards _PENDING_REGISTRATIONS only.
    register = registration._PENDING_REGISTRATIONS[SLUG]
    acquired = []

    def try_the_lock():
        if registration._PENDING_LOCK.acquire(blocking=False):
            registration._PENDING_LOCK.release()
            acquired.append(True)
        else:
            acquired.append(False)

    def register_while_another_thread_tries_the_lock():
        other = threading.Thread(target=try_the_lock)
        other.start()
        other.join()
        return register()

    registration._PENDING_REGISTRATIONS[SLUG] = (
        register_while_another_thread_tries_the_lock
    )
    with django_capture_on_commit_callbacks(execute=True):
        registered = registration.get_resource_type_for(SLUG)
    assert acquired == [True]
    assert registered.type_uri == TYPE_URI
    assert SLUG not in registration._PENDING_REGISTRATIONS


UNREACHABLE_OR_NOT_MIGRATED = (
    f"Could not register resource type {SLUG!r} ({{error}}): database "
    "'default' is unreachable or not migrated (as before the first migrate). "
    "Its routes are served, and it is registered when it is next read."
)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "migrated, failure, level, message",
    [
        # Unreachable (it cannot tell whether it is migrated): a transient
        # failure.
        (
            True,
            unreachable,
            logging.WARNING,
            UNREACHABLE_OR_NOT_MIGRATED.format(
                error="OperationalError: could not connect to server: "
                "Connection refused"
            ),
        ),
        # Not migrated (migrate, makemigrations or check before migrate).
        (
            False,
            resource_types_denied,
            logging.WARNING,
            UNREACHABLE_OR_NOT_MIGRATED.format(
                error="ProgrammingError: permission denied for table resourcetype"
            ),
        ),
        # Reachable and migrated: a fault (a missing grant, a schema bug)
        # that fails every request for the type until it is fixed.
        (
            True,
            resource_types_denied,
            logging.ERROR,
            f"Could not register resource type {SLUG!r} (ProgrammingError: "
            "permission denied for table resourcetype) on database 'default', "
            "which is reachable and migrated: requests for it fail until its "
            "registration succeeds. Its routes are served, and its "
            "registration is retried when it is next read.",
        ),
    ],
    ids=["unreachable", "not migrated", "migrated"],
)
def test_a_failed_registration_is_an_error_on_a_migrated_database(
    migrated, failure, level, message, probes, caplog
):
    if not migrated:
        unapply_a_scrud_migration(registration.type_db())
    try:
        with type_connection().execute_wrapper(failure):
            urls = registration.json_resource_type(
                resource_type_uri=TYPE_URI,
                slug=SLUG,
                schema_func=lambda: json.dumps(SCHEMA),
            )
        # Deferred either way: the URLconf import does not fail.
        assert [route.name for route in urls] == [f"{SLUG}-list", f"{SLUG}-detail"]
        assert SLUG in registration._PENDING_REGISTRATIONS
    finally:
        registration._PENDING_REGISTRATIONS.pop(SLUG, None)
        services.services.pop(TYPE_URI, None)
    logged = [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "scrud_django.registration"
    ]
    assert logged == [(level, message)]


@pytest.fixture
def probes(monkeypatch):
    """The databases `registration.migrated_and_reachable` asks
    `scrud_migrations_pending` about, in order; its answers are forgotten
    before and after the test."""
    from scrud_django import checks

    asked = []
    pending = checks.scrud_migrations_pending

    def counted(alias):
        asked.append(alias)
        return pending(alias)

    monkeypatch.setattr(checks, "scrud_migrations_pending", counted)
    registration._PROBED.clear()
    yield asked
    registration._PROBED.clear()


def register_deferred(slug=SLUG, type_uri=TYPE_URI):
    return registration.json_resource_type(
        resource_type_uri=type_uri,
        slug=slug,
        schema_func=lambda: json.dumps(SCHEMA),
    )


def forget_deferred(*slugs_and_type_uris):
    for slug, type_uri in slugs_and_type_uris:
        registration._PENDING_REGISTRATIONS.pop(slug, None)
        services.services.pop(type_uri, None)


def registration_logs(caplog):
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "scrud_django.registration"
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "error",
    [
        NodeNotFoundError(
            "Migration app.0002 dependencies reference nonexistent parent node",
            ("app", "0001"),
        ),
        BadMigrationError("Migration 0002 in app app has no Migration class"),
        ImproperlyConfigured("The database alias is misconfigured."),
        ConnectionDoesNotExist("The connection 'replica' doesn't exist."),
    ],
    ids=[
        "NodeNotFoundError",
        "BadMigrationError",
        "ImproperlyConfigured",
        "ConnectionDoesNotExist",
    ],
)
def test_a_registration_failure_is_an_error_when_the_probe_fails_otherwise(
    error, monkeypatch, probes, caplog
):
    # Not a database error: a broken migration graph (another app's), or a
    # misconfiguration. It does not say the database is unreachable or not
    # migrated, so the failure is not a warning: the probe's error is
    # logged with its traceback, and the failure as an error.
    from scrud_django import checks

    def broken(alias):
        raise error

    monkeypatch.setattr(checks, "scrud_migrations_pending", broken)
    try:
        with type_connection().execute_wrapper(resource_types_denied):
            urls = register_deferred()
        assert [route.name for route in urls] == [f"{SLUG}-list", f"{SLUG}-detail"]
        assert SLUG in registration._PENDING_REGISTRATIONS
    finally:
        forget_deferred((SLUG, TYPE_URI))
    assert registration_logs(caplog) == [
        (
            logging.ERROR,
            "Could not tell whether database 'default' is reachable and "
            f"migrated: {type(error).__name__}: {error}",
        ),
        (
            logging.ERROR,
            f"Could not register resource type {SLUG!r} (ProgrammingError: "
            "permission denied for table resourcetype) on database 'default', "
            "whose migration state could not be told (logged above): requests "
            "for it fail until its registration succeeds. Its routes are "
            "served, and its registration is retried when it is next read.",
        ),
    ]
    probe_record = next(
        record
        for record in caplog.records
        if record.name == "scrud_django.registration"
    )
    assert probe_record.exc_info is not None
    assert probe_record.exc_info[1] is error


@pytest.mark.django_db
@pytest.mark.parametrize("error", [OperationalError, ProgrammingError, InterfaceError])
def test_a_database_error_reading_the_migration_state_is_a_warning(
    error, monkeypatch, probes, caplog
):
    from scrud_django import checks

    def failing(alias):
        raise error("permission denied for table django_migrations")

    monkeypatch.setattr(checks, "scrud_migrations_pending", failing)
    try:
        with type_connection().execute_wrapper(resource_types_denied):
            register_deferred()
    finally:
        forget_deferred((SLUG, TYPE_URI))
    assert registration_logs(caplog) == [
        (
            logging.WARNING,
            UNREACHABLE_OR_NOT_MIGRATED.format(
                error="ProgrammingError: permission denied for table resourcetype"
            ),
        )
    ]


@pytest.mark.django_db
def test_the_probe_is_asked_once_per_database_until_a_retry(
    probes, django_capture_on_commit_callbacks
):
    # While the database is unreachable each registration at URLconf import
    # fails, after a connection timeout; asking again whether it is
    # migrated would cost another. One answer serves them all, until the
    # registration is retried (a request reads the type), after which the
    # database is asked again.
    other_slug, other_uri = "other-deferred-things", "tests://OtherDeferred"
    try:
        with type_connection().execute_wrapper(unreachable):
            register_deferred()
            register_deferred(other_slug, other_uri)
        assert probes == ["default"]
        with (
            type_connection().execute_wrapper(unreachable),
            pytest.raises(OperationalError),
        ):
            registration.get_resource_type_for(SLUG)
        with type_connection().execute_wrapper(unreachable):
            register_deferred()
        assert probes == ["default", "default"]
        # A registration that succeeds also forgets the answer.
        with django_capture_on_commit_callbacks(execute=True):
            register_deferred(other_slug, other_uri)
        with type_connection().execute_wrapper(unreachable):
            register_deferred()
        assert probes == ["default", "default", "default"]
    finally:
        forget_deferred((SLUG, TYPE_URI), (other_slug, other_uri))


@pytest.fixture
def forgotten_built_in_types():
    """The built-in types `registration` caches forgotten, as in a process
    that has not registered them yet, and restored afterwards (with their
    pending registrations forgotten)."""
    saved = {name: getattr(registration, name) for name in BUILT_IN_CACHES}
    for name in BUILT_IN_CACHES:
        setattr(registration, name, None)
    try:
        yield
    finally:
        for slug in BUILT_IN_SLUGS:
            registration._PENDING_REGISTRATIONS.pop(slug, None)
        for name, value in saved.items():
            setattr(registration, name, value)


@pytest.mark.django_db
def test_the_built_in_types_keep_their_routes_when_registration_fails(
    forgotten_built_in_types, django_capture_on_commit_callbacks
):
    stored = set(ResourceType.objects.values_list("pk", "etag", "modified_at"))
    with type_connection().execute_wrapper(unreachable):
        urls = registration.built_in_resource_type_urls()
    assert [route.name for route in urls] == [
        name for slug in BUILT_IN_SLUGS for name in (f"{slug}-list", f"{slug}-detail")
    ]
    assert sorted(registration._PENDING_REGISTRATIONS) == sorted(BUILT_IN_SLUGS)
    # Read once the database is back, each is registered: its row was there
    # already, and is kept as it was.
    with django_capture_on_commit_callbacks(execute=True):
        policies = registration.get_resource_type_for("workflow-policies")
    assert policies.type_uri == "http://api.openteams.com/json-ld/WorkflowPolicy"
    assert "workflow-policies" not in registration._PENDING_REGISTRATIONS
    assert registration.WORKFLOW_POLICY_RESOURCE_TYPE_ is not None
    assert set(ResourceType.objects.values_list("pk", "etag", "modified_at")) == (
        stored
    )


@pytest.mark.django_db
def test_a_pending_built_in_type_is_registered_again_not_read_from_its_cache(
    forgotten_built_in_types, django_capture_on_commit_callbacks
):
    with type_connection().execute_wrapper(unreachable):
        registration.built_in_resource_type_urls()
    # Registered since, as another type's document type, in a transaction
    # that was rolled back: cached, but not stored.
    ghost = ResourceType(pk=0, type_uri="http://json-schema.org/draft-04/schema")
    ghost.slug = "json-schema"
    registration.JSON_SCHEMA_RESOURCE_TYPE_ = ghost
    registration.JSON_SCHEMA_REGISTRATION_ = SimpleNamespace(
        resource_type=ghost, urls=[]
    )
    with django_capture_on_commit_callbacks(execute=True):
        schema_type = registration.get_resource_type_for("json-schema")
    assert schema_type.pk == ResourceType.objects.get(slug="json-schema").pk
    assert registration.JSON_SCHEMA_RESOURCE_TYPE_.pk == schema_type.pk
    assert "json-schema" not in registration._PENDING_REGISTRATIONS


def a_type_row(slug, revision):
    return ResourceType.objects.create(
        type_uri=f"tests://Revisioned{uuid4().hex}",
        slug=slug,
        revision=revision,
        etag=uuid4().hex,
        modified_at=now(),
    )


@pytest.mark.django_db
def test_the_latest_revision_of_a_slug_is_the_highest_number():
    a_type_row("revisioned", "9")
    ten = a_type_row("revisioned", "10")
    a_type_row("revisioned", "2")
    assert registration.get_resource_type_for("revisioned").pk == ten.pk
    using = registration.type_db()
    assert registration.get_resource_type_for("revisioned", using=using).pk == ten.pk


@pytest.mark.django_db
def test_rows_of_the_same_revision_are_read_oldest_first():
    # Two rows of one revision (two processes that registered a new type at
    # once: `type-registration-race`): every read takes the first stored.
    first = a_type_row("raced", "1")
    a_type_row("raced", "1")
    assert registration.get_resource_type_for("raced").pk == first.pk


@pytest.mark.django_db
@pytest.mark.parametrize(
    "latest_first", [True, False], ids=["latest stored first", "latest stored last"]
)
def test_registration_updates_the_row_that_reads_use(latest_first):
    # Two rows of one type (stored past registration, or by two processes
    # at once: `type-registration-race`). A process start updates the row
    # `get_resource_type_for` reads, the latest revision, not whichever the
    # database returns first, nor the oldest or newest primary key: "10"
    # is stored before "9" (the lower key) and after it.
    def stored(revision):
        return ResourceType.objects.create(
            type_uri=TYPE_URI,
            slug=SLUG,
            revision=revision,
            etag=uuid4().hex,
            modified_at=now(),
        )

    if latest_first:
        latest = stored("10")
        older = stored("9")
        assert latest.pk < older.pk
    else:
        older = stored("9")
        latest = stored("10")
        assert latest.pk > older.pk
    registered = registration.ResourceTypeRegistration(
        TYPE_URI,
        slug=SLUG,
        schema_func=lambda: SCHEMA,
        register_service=False,
    ).resource_type
    assert registered.pk == latest.pk
    assert registration.get_resource_type_for(SLUG).pk == latest.pk
    latest.refresh_from_db()
    older.refresh_from_db()
    assert latest.schema.content == SCHEMA
    assert older.schema is None


def test_the_catalog_leaves_out_a_service_no_route_names(monkeypatch):
    monkeypatch.setattr(services, "services", {})
    services.add_service("tests://Routed", lambda request=None: "/routed/")
    services.add_service("tests://NotRouted", "no-such-route")
    services.add_service("tests://NotRoutedEither", ["nor-this", "nor-that"])

    assert services.catalog() == {"tests://Routed": "/routed/"}
    assert services.etag_for() == services.etag_of({"tests://Routed": "/routed/"})
