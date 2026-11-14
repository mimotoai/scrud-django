import functools
import importlib.resources
import json
import logging
import threading
from contextlib import contextmanager
from uuid import uuid4

import reversion
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import router, transaction
from django.db.models.functions import Length
from django.db.utils import InterfaceError, OperationalError, ProgrammingError
from django.shortcuts import get_list_or_404, get_object_or_404
from django.urls import path
from django.utils import timezone

from scrud_django import services
from scrud_django.core.types import TypeOptions, register_type_options
from scrud_django.models import Resource, ResourceType
from scrud_django.workflow import (
    WORKFLOW_POLICY_TYPE_HELD,
    WORKFLOW_POLICY_TYPE_URI,
    lock_workflow_policy_type,
)

logger = logging.getLogger(__name__)


def write_db(instance=None):
    """The database `ResourceRegistration` writes resources to
    (``router.db_for_write(Resource)``, with `instance` as the router's
    ``instance`` hint when there is one): its transactions open there, and the
    resource, its ``ResourceContext`` rows and its ``ResourceRemoval`` stamps
    are all saved there, so that the ``WorkflowPolicy`` lock and the writes
    share one transaction. Route ``Resource``,
    ``ResourceType``, ``ResourceContext`` and ``ResourceRemoval`` writes to
    one alias (the ``scrud_django.E004`` check)."""
    if instance is None:
        return router.db_for_write(Resource)
    return router.db_for_write(Resource, instance=instance)


def lock_if_workflow_policy(resource_type, using):
    """When `resource_type` is ``WorkflowPolicy``, take
    `lock_workflow_policy_type` on database `using`. Call it first inside the
    write's transaction on that same database (``transaction.atomic(using=
    write_db())``; ``select_for_update`` outside a transaction is an error on
    PostgreSQL). `ResourceRegistration`'s writes, and so the API's, then lock
    the type row before they write the ``Resource`` row, in that one order: a
    PUT and a DELETE of the same policy cannot deadlock.

    Public: server code that locks a ``WorkflowPolicy`` resource's row itself
    (``Resource.objects.select_for_update()``) and then saves or deletes it
    must call this in the same transaction, on every database, BEFORE the
    ``select_for_update()``, with the type read without the row lock. Its
    save or delete takes this lock too, and taking it after the row's lock
    deadlocks with an API write of the policy, which holds this lock and
    waits for the row::

        db = write_db()
        with transaction.atomic(using=db):
            lock_if_workflow_policy(
                get_resource_type_for("workflow-policies", using=db), db
            )
            policy = Resource.objects.using(db).select_for_update().get(pk=pk)
            ...
            policy.save()  # or policy.delete()

    A plain ``save()`` or ``delete()`` without a row lock of its own needs
    nothing: it takes this lock before the row."""
    if resource_type.type_uri == WORKFLOW_POLICY_TYPE_URI:
        lock_workflow_policy_type(using)


@contextmanager
def type_locked(resource_type, using):
    """Lock `resource_type`'s row on database `using` (``select_for_update``,
    ``FOR NO KEY UPDATE`` where the database has it, as
    `lock_workflow_policy_type` takes it) until the caller's transaction there
    ends; call it first inside that transaction. A conditional create takes
    it (``ResourceViewMixin.conditional_create_lock``, which
    ``LockedWriteMixin.initial`` enters once the request is authorized) so
    that conditional creates of a type run one at a time. The other writes
    of the type do not take it first: a foreign key check of a row they
    insert that refers to the type (a resource, a removal stamp) may wait
    for it, holding nothing it waits for, and ``FOR NO KEY UPDATE`` does not
    block PostgreSQL's (``FOR KEY SHARE``).

    For ``WorkflowPolicy`` it is `lock_workflow_policy_type` (the same rows,
    taken in the same order its writes take them), which then is not taken
    again while the block runs."""
    from django.db import connections

    if resource_type.type_uri != WORKFLOW_POLICY_TYPE_URI:
        no_key = connections[using].features.has_select_for_no_key_update
        list(
            ResourceType.objects.using(using)
            .select_for_update(no_key=no_key)
            .filter(pk=resource_type.pk)
        )
        yield
        return
    connection = connections[using]
    lock_workflow_policy_type(using)
    previous = getattr(connection, WORKFLOW_POLICY_TYPE_HELD, False)
    setattr(connection, WORKFLOW_POLICY_TYPE_HELD, True)
    try:
        yield
    finally:
        setattr(connection, WORKFLOW_POLICY_TYPE_HELD, previous)


def type_db():
    """The database `ResourceType` rows are written to
    (``router.db_for_write(ResourceType)``). A write of resources, and the
    registration of a type (`ResourceTypeRegistration`), read the resource
    type there, never through ``db_for_read`` (a replica may be stale), with
    the type's documents (`TYPE_DOCUMENTS`, `get_resource_type_for`)."""
    return router.db_for_write(ResourceType)


# The documents of a resource type, read with the type, on its database
# (`get_resource_type_for`): its schema, which a write validates the body
# against, with its own type (whether it is a JSON Schema; the Link's route);
# its context, linked with its own type's route; its indexing policy, which
# search indexing reads.
TYPE_DOCUMENTS = (
    "schema__resource_type",
    "context__resource_type",
    "indexing_policy",
)


def latest_revision_first(queryset):
    """`queryset` of ``ResourceType`` rows ordered latest revision first: a
    longer revision, then the greater string (``"10"`` after ``"9"``), and
    rows of the same revision oldest (lowest primary key) first. Reads
    (`get_resource_type_for`) and registration (`ResourceTypeRegistration`)
    order a type's rows this way, so that registration updates the row
    every process reads."""
    return queryset.order_by(Length("revision").desc(), "-revision", "pk")


def get_resource_type_for(slug, using=None):
    """The latest revision of the resource type `slug` (404 if none), read on
    database `using` (``None``: as the router reads it). Read on a given
    database, its documents (`TYPE_DOCUMENTS`) come with it, from the same
    database: loaded later, as related objects, they would be read where
    ``db_for_read`` (given the type as hint) says, which for a router that
    ignores the hint is a replica that may not have the type's latest
    schema.

    A slug has one row, unless rows were stored past registration (which
    updates a type's row in place) or two processes registered a new type at
    once (``type-registration-race`` in ``docs/follow-ups.md``). The latest
    revision is the highest number (``"10"`` after ``"9"``): a longer
    revision, then the greater string, which orders revisions written as
    numbers without leading zeros (as registration's are) numerically on
    every database; rows of the same revision are read oldest (lowest
    primary key) first, so every process reads the same one.

    A type whose registration is pending (`register_or_defer`: it failed with
    a database error when its routes were built) is registered first
    (`retry_registration`), and that registration's error raised if it fails
    again."""
    retry_registration(slug)
    queryset = latest_revision_first(ResourceType.objects.filter(slug=slug))
    if using is not None:
        queryset = queryset.using(using).select_related(*TYPE_DOCUMENTS)
    return get_list_or_404(queryset[:1])[0]


def get_locked_resource(resource_type, slug, using):
    """The `Resource` of `resource_type` with primary key `slug` (404 if
    none), read on database `using` with its row locked
    (``select_for_update``) until the caller's transaction there ends. Take
    `lock_if_workflow_policy` first. Its type is `resource_type` itself, not
    loaded again (through the read router)."""
    resource = get_object_or_404(
        Resource.objects.using(using).select_for_update(),
        resource_type=resource_type,
        pk=int(slug),
    )
    Resource.resource_type.field.set_cached_value(resource, resource_type)
    return resource


class ResourceTypeRegistration:
    def __init__(
        self,
        resource_type_uri: str,
        revision: str = '1',
        schema_resource: Resource = None,
        schema_func: callable = None,
        schema_uri: str = None,
        context_resource: Resource = None,
        context_func: callable = None,
        context_uri: str = None,
        indexing_policy_resource: Resource = None,
        indexing_policy_func: callable = None,
        slug: str = None,
        viewset_class: object = None,
        uses_reversion: bool = True,
        register_service: bool = True,
    ):
        """
        Register resource type model.
        """
        from scrud_django import views

        # Read on the type's write database, with the documents compared
        # below with the files (register_json_file): a lagging replica would
        # leave a schema edited since in place, add a second row for a type
        # it does not have yet, and (outside DEBUG) save its stale copy of
        # the row back. Of several rows of the type (`type-registration-race`),
        # the one reads use (`latest_revision_first`) is updated.
        search = latest_revision_first(
            ResourceType.objects.using(type_db())
            .select_related(*TYPE_DOCUMENTS)
            .filter(type_uri=resource_type_uri)
        )

        self.viewset_class = viewset_class or views.ResourceViewSet

        stored = search[0] if search else None
        if stored is None:
            self.resource_type = ResourceType(
                type_uri=resource_type_uri,
                schema_uri=schema_uri,
                context_uri=context_uri,
                slug=slug,
                etag=uuid4().hex,
                modified_at=timezone.now(),
                uses_reversion=uses_reversion,
                revision=revision,
            )
        elif settings.DEBUG:
            # Under DEBUG (in development) the row follows the arguments.
            # The stored schema, context and indexing policy are updated in
            # place below (register_json_file), not stored again on every
            # process start.
            self.resource_type = ResourceType(
                pk=stored.pk,
                type_uri=resource_type_uri,
                schema_uri=schema_uri,
                context_uri=context_uri,
                slug=slug,
                etag=stored.etag,
                modified_at=stored.modified_at,
                uses_reversion=uses_reversion,
                revision=revision,
                schema=stored.schema,
                context=stored.context,
                indexing_policy=stored.indexing_policy,
            )
        else:
            self.resource_type = stored
        before = None if stored is None else stored_fields(stored)

        if schema_resource is None and schema_func:
            self.resource_type.schema = register_json_schema(
                schema_func, self.resource_type.schema
            )
        else:
            self.resource_type.schema = schema_resource

        if context_resource is None and context_func:
            self.resource_type.context = register_json_ld_context(
                context_func, self.resource_type.context
            )
        else:
            self.resource_type.context = context_resource

        if indexing_policy_resource is None and indexing_policy_func:
            self.resource_type.indexing_policy = register_indexing_policy(
                indexing_policy_func, self.resource_type.indexing_policy
            )
        else:
            self.resource_type.indexing_policy = indexing_policy_resource
        self.resource_type.uses_reversion = uses_reversion
        # A process start that changes nothing writes nothing: the row (its
        # ETag and modification time included) and its reversion history
        # stay as they are.
        if before is None or stored_fields(self.resource_type) != before:
            if before is not None and settings.DEBUG:
                self.resource_type.etag = uuid4().hex
                self.resource_type.modified_at = timezone.now()
            with reversion.create_revision():
                self.resource_type.save()
                reversion.set_comment(
                    'Update resourcetype' if search else 'Add resourcetype'
                )

        # Register with service catalog
        if register_service:
            services.add_service(self.resource_type.type_uri, self.resource_type.slug)
        self.register_urls()

    def register_urls(self):
        """Register resource type url."""
        self.urls = resource_type_urls(self.resource_type.slug, self.viewset_class)


def stored_fields(resource_type):
    """The fields of `resource_type`'s row that registration sets, other than
    its ETag and modification time, which follow a change of them."""
    return {
        field.attname: getattr(resource_type, field.attname)
        for field in ResourceType._meta.concrete_fields
        if field.attname not in ("id", "etag", "modified_at")
    }


def resource_type_urls(slug, viewset_class):
    """The list and detail routes of the resource type `slug`, served by
    `viewset_class`. They need no database: the views read the type by its
    slug (`get_resource_type_for`) on each request."""
    names = ResourceType(slug=slug)
    scrud_list = viewset_class.as_view(
        {
            'get': 'list',
            'head': 'list',
            'post': 'create',
        },  # noqa: E231
        resource_type_name=slug,
    )
    scrud_detail = viewset_class.as_view(
        {
            'get': 'retrieve',
            'head': 'retrieve',
            'put': 'update',
            'delete': 'destroy',
        },
        resource_type_name=slug,
    )
    return [
        path(f'{slug}/', scrud_list, name=names.route_name_list()),
        path(f'{slug}/<str:slug>/', scrud_detail, name=names.route_name_detail()),
    ]


# Resource types whose registration failed with a database error when their
# routes were built (`register_or_defer`), by slug: the function that
# registers each. A type is registered when it is next read
# (`retry_registration`, from `get_resource_type_for`) and forgotten here once
# that registration commits.
_PENDING_REGISTRATIONS = {}
# Database alias -> what `probe` found there: True (reachable and migrated),
# False (unreachable, or not migrated) or None (it could not be told: the
# probe failed otherwise). Kept while registrations keep failing, as at a
# URLconf import while the database is unreachable, where asking again would
# cost every registration a second connection timeout; forgotten when a
# registration succeeds or a pending one is retried (`retry_registration`).
_PROBED = {}
_PENDING_LOCK = threading.RLock()


def register_or_defer(
    register,
    type_uri,
    slug,
    viewset_class,
    register_service=True,
    retry=None,
    urls_func=None,
):
    """The routes of the resource type `slug` that `register` registers (a
    function returning its `ResourceTypeRegistration`), run in a
    transaction on `type_db`.

    When it fails with a database error (``OperationalError`` or
    ``ProgrammingError``: the database is unreachable, or not migrated yet,
    as when ``migrate`` or ``check`` imports the URLconf), the routes are
    still returned, built from `slug` and `viewset_class` (they need no
    database; `urls_func` builds them, default `resource_type_urls`), the
    type is added to the service catalog (`type_uri`) when
    `register_service`, and its registration is left pending
    (`_PENDING_REGISTRATIONS`): it is retried when the type is next read, so
    a process that started while the database was unreachable serves the
    type once it is back, without a restart. Until then a request for the
    type fails with the retry's error (a 500), not a 404. The retry calls
    `retry` (default: `register`).

    The failure is logged as a warning while the database is unreachable,
    not migrated, or its migration state cannot be read (a database error;
    `migrated_and_reachable`), and as an error on a database that is
    reachable and migrated: there it is a fault (a missing grant, a schema
    out of step with the migrations) that fails every request for the type
    until it is fixed. It is an error too when the migration state cannot
    be told for another reason (a broken migration graph), which `probe`
    logs with its traceback. The answer is kept for the registrations that
    fail after it (`_PROBED`)."""
    db = type_db()
    try:
        with transaction.atomic(using=db):
            urls = register().urls
    except (OperationalError, ProgrammingError) as e:
        migrated = probe(db)
        if migrated is None:
            logger.error(
                "Could not register resource type %r (%s: %s) on database %r, "
                "whose migration state could not be told (logged above): "
                "requests for it fail until its registration succeeds. Its "
                "routes are served, and its registration is retried when it is "
                "next read.",
                slug,
                type(e).__name__,
                e,
                db,
            )
        elif migrated:
            logger.error(
                "Could not register resource type %r (%s: %s) on database %r, "
                "which is reachable and migrated: requests for it fail until "
                "its registration succeeds. Its routes are served, and its "
                "registration is retried when it is next read.",
                slug,
                type(e).__name__,
                e,
                db,
            )
        else:
            logger.warning(
                "Could not register resource type %r (%s: %s): database %r is "
                "unreachable or not migrated (as before the first migrate). Its "
                "routes are served, and it is registered when it is next read.",
                slug,
                type(e).__name__,
                e,
                db,
            )
        with _PENDING_LOCK:
            _PENDING_REGISTRATIONS[slug] = retry or register
        if register_service:
            services.add_service(type_uri, slug)
        return (urls_func or resource_type_urls)(slug, viewset_class)
    with _PENDING_LOCK:
        _PROBED.pop(db, None)
    return urls


def probe(alias):
    """`migrated_and_reachable` for database `alias`, or ``None`` when it
    raises (not a database error: a broken migration graph, of any app, a
    misconfigured database), which is logged as an error with its traceback
    on the ``scrud_django.registration`` logger. The answer is kept
    (`_PROBED`) for the registrations that fail after it."""
    with _PENDING_LOCK:
        if alias in _PROBED:
            return _PROBED[alias]
    try:
        migrated = migrated_and_reachable(alias)
    except Exception as error:  # noqa: BLE001
        # Log and go on: the URLconf import must not fail on it.
        logger.error(
            "Could not tell whether database %r is reachable and migrated: %s: %s",
            alias,
            type(error).__name__,
            error,
            exc_info=True,
        )
        migrated = None
    with _PENDING_LOCK:
        _PROBED[alias] = migrated
    return migrated


def migrated_and_reachable(alias):
    """Whether database `alias` can be reached and has all of scrud_django's
    migrations applied (`scrud_django.checks.scrud_migrations_pending`).
    ``False`` on a database error (``OperationalError``,
    ``ProgrammingError``, ``InterfaceError``): the database is unreachable,
    or its migration table cannot be read. Any other error, such as a broken
    migration graph (``NodeNotFoundError``, ``BadMigrationError``, of any
    app), ``ConnectionDoesNotExist`` or ``ImproperlyConfigured``, is raised:
    it says nothing about the database."""
    from scrud_django.checks import scrud_migrations_pending

    try:
        return not scrud_migrations_pending(alias)
    except (OperationalError, ProgrammingError, InterfaceError):
        return False


def retry_registration(slug):
    """Register the resource type `slug` if its registration is pending
    (`register_or_defer`), in a transaction on `type_db` (a savepoint inside
    the caller's), raising its error if it fails again. It is forgotten as
    pending once that transaction commits, and logged then: if the caller's
    is rolled back (a refused write), so is the registration, and it is
    retried on the next read. A retry also forgets what `probe` found on
    `type_db`: a registration that fails after it asks again.

    The process lock guards `_PENDING_REGISTRATIONS` (and `_PROBED`) only;
    the registration runs without it. Its row locks last until the caller's
    transaction commits, so under the lock a thread waiting for them in the
    database would hold it while the thread holding them waited for it
    (reading another pending type): a deadlock the database cannot detect.
    Two threads of a process may then register the same type at once, as
    two processes may (``type-registration-race`` in
    ``docs/follow-ups.md``).

    A pending type registered under another slug (its stored row's, kept
    outside ``DEBUG``) than the one its routes were built for raises
    ``ImproperlyConfigured``: its routes would read a type that is not
    there."""
    with _PENDING_LOCK:
        register = _PENDING_REGISTRATIONS.get(slug)
    if register is None:
        return
    db = type_db()
    with _PENDING_LOCK:
        _PROBED.pop(db, None)
    with transaction.atomic(using=db):
        registered = register().resource_type
        if registered.slug != slug:
            raise ImproperlyConfigured(
                f"The resource type {registered.type_uri!r} is stored with "
                f"the slug {registered.slug!r}, but its routes were built "
                f"for {slug!r}: register it with its stored slug, or under "
                "DEBUG to store the new one."
            )

        def forget():
            with _PENDING_LOCK:
                if _PENDING_REGISTRATIONS.get(slug) is not register:
                    return
                del _PENDING_REGISTRATIONS[slug]
            logger.info("Registered resource type %r, which was pending.", slug)

        transaction.on_commit(forget, using=db)


class ResourceRegistration:
    @staticmethod
    def register(content: str, register_type: str) -> Resource:
        """
        Register resource content for the given resource type name.

        Parameters
        ----------
        content : dict
        register_type : str

        Returns
        -------
        Resource

        """
        resource_type = get_resource_type_for(register_type, using=type_db())
        resource = Resource(content=content, resource_type=resource_type)
        resource = ResourceRegistration.register_instance(resource)
        return resource

    @staticmethod
    def register_instance(resource: Resource) -> Resource:
        """
        Register the resource for the given resource type.

        Parameters
        ----------
        resource : Resource
        resource_type : ResourceType

        Returns
        -------
        Resource

        """
        db = write_db(resource)
        with transaction.atomic(using=db):
            lock_if_workflow_policy(resource.resource_type, db)
            resource.etag = uuid4().hex
            resource.modified_at = timezone.now()
            if resource.resource_type.uses_reversion:
                with reversion.create_revision():
                    resource.save(using=db)
                    reversion.set_comment('Add Resource')
            else:
                resource.save(using=db)
        return resource

    @staticmethod
    def update(content: str, register_type: str, slug: str) -> Resource:
        """
        Update resource content for the given resource type name and slug.

        In one transaction on `write_db`: take `lock_if_workflow_policy`, read
        the resource with its row locked (`get_locked_resource`) and write it
        (`update_locked`). Nothing is read through the read router.

        Parameters
        ----------
        content : dict
        register_type : str
        slug : str

        Returns
        -------
        Resource

        """
        db = write_db()
        with transaction.atomic(using=db):
            resource_type = get_resource_type_for(register_type, using=type_db())
            lock_if_workflow_policy(resource_type, db)
            # note: for now slug is used as ID
            resource = get_locked_resource(resource_type, slug, db)
            return ResourceRegistration.update_locked(
                resource, content, resource_type=resource_type, using=db
            )

    @staticmethod
    def update_locked(
        resource: Resource, content, *, resource_type: ResourceType, using: str
    ) -> Resource:
        """
        Write `content` to `resource`, a row the caller read on database
        `using`, inside its transaction there, after it took
        `lock_if_workflow_policy` and then locked the row
        (`get_locked_resource`), in that order. `resource_type` is the
        resource's type (its row is not written). The instance is saved as it is:
        the row is not read again, so what the caller checked is what is
        written over. `update` and the API's PUT use it.
        """
        resource.resource_type = resource_type
        resource.content = content
        resource.etag = uuid4().hex
        resource.modified_at = timezone.now()
        if resource_type.uses_reversion:
            with reversion.create_revision():
                resource.save(using=using)
                reversion.set_comment('Change Resource')
        else:
            resource.save(using=using)
        return resource

    @staticmethod
    def delete(register_type: str, slug: str):
        """
        Delete resource for the given resource type name and slug.

        In one transaction on `write_db`: take `lock_if_workflow_policy`, read
        the resource with its row locked (`get_locked_resource`) and delete it
        (`delete_locked`).

        Parameters
        ----------
        register_type : str
        slug : str

        """
        db = write_db()
        with transaction.atomic(using=db):
            resource_type = get_resource_type_for(register_type, using=type_db())
            lock_if_workflow_policy(resource_type, db)
            # note: for now slug is used as ID
            resource = get_locked_resource(resource_type, slug, db)
            ResourceRegistration.delete_locked(
                resource, resource_type=resource_type, using=db
            )

    @staticmethod
    def delete_locked(resource: Resource, *, resource_type: ResourceType, using: str):
        """
        Delete `resource`, read and locked as for `update_locked`.
        `resource_type` is its type (its row is not written). `delete` and the
        API's DELETE use it.
        """
        resource.delete(using=using)


JSON_SCHEMA_REGISTRATION_ = None
JSON_SCHEMA_RESOURCE_TYPE_ = None
JSON_LD_RESOURCE_TYPE_ = None
JSON_LD_REGISTRATION_ = None
INDEXING_POLICY_RESOURCE_TYPE_ = None
INDEXING_POLICY_REGISTRATION_ = None
WORKFLOW_POLICY_RESOURCE_TYPE_ = None
WORKFLOW_POLICY_REGISTRATION_ = None


def register_json_schema_resource_type(force=False):
    global JSON_SCHEMA_RESOURCE_TYPE_
    global JSON_SCHEMA_REGISTRATION_
    if JSON_SCHEMA_RESOURCE_TYPE_ is None or force:
        from scrud_django.views import JsonSchemaViewSet

        JSON_SCHEMA_REGISTRATION_ = ResourceTypeRegistration(
            resource_type_uri="http://json-schema.org/draft-04/schema",
            revision="1",
            schema_uri="http://json-schema.org/draft-04/schema",
            slug="json-schema",
            viewset_class=JsonSchemaViewSet,
        )
        JSON_SCHEMA_RESOURCE_TYPE_ = JSON_SCHEMA_REGISTRATION_.resource_type
    return JSON_SCHEMA_RESOURCE_TYPE_


def register_json_ld_resource_type(force=False):
    global JSON_LD_RESOURCE_TYPE_
    global JSON_LD_REGISTRATION_
    if JSON_LD_RESOURCE_TYPE_ is None or force:
        from scrud_django.views import JsonLdViewSet

        JSON_LD_REGISTRATION_ = ResourceTypeRegistration(
            resource_type_uri="http://www.w3.org/ns/json-ld#context",
            revision="1",
            slug="json-ld",
            viewset_class=JsonLdViewSet,
        )
        JSON_LD_RESOURCE_TYPE_ = JSON_LD_REGISTRATION_.resource_type
    return JSON_LD_RESOURCE_TYPE_


def register_indexing_policy_resource_type(force=False):
    global INDEXING_POLICY_RESOURCE_TYPE_
    global INDEXING_POLICY_REGISTRATION_
    if INDEXING_POLICY_RESOURCE_TYPE_ is None or force:
        INDEXING_POLICY_REGISTRATION_ = ResourceTypeRegistration(
            resource_type_uri="http://api.openteams.com/json-ld/IndexingPolicy",
            revision="1",
            slug="indexing-policies",
            schema_func=lambda: (
                importlib.resources.files("scrud_django.static")
                .joinpath("IndexingPolicy-schema.json")
                .read_text()
            ),
            context_func=lambda: (
                importlib.resources.files("scrud_django.static")
                .joinpath("IndexingPolicy-ld.json")
                .read_text()
            ),
        )
        INDEXING_POLICY_RESOURCE_TYPE_ = INDEXING_POLICY_REGISTRATION_.resource_type
    return INDEXING_POLICY_RESOURCE_TYPE_


def workflow_policy_resource_type(force=False):
    global WORKFLOW_POLICY_RESOURCE_TYPE_
    global WORKFLOW_POLICY_REGISTRATION_
    if WORKFLOW_POLICY_RESOURCE_TYPE_ is None or force:
        WORKFLOW_POLICY_REGISTRATION_ = ResourceTypeRegistration(
            resource_type_uri="http://api.openteams.com/json-ld/WorkflowPolicy",
            revision="1",
            slug="workflow-policies",
            schema_func=lambda: (
                importlib.resources.files("scrud_django.static")
                .joinpath("WorkflowPolicy-schema.json")
                .read_text()
            ),
            context_func=lambda: (
                importlib.resources.files("scrud_django.static")
                .joinpath("WorkflowPolicy-ld.json")
                .read_text()
            ),
        )
        WORKFLOW_POLICY_RESOURCE_TYPE_ = WORKFLOW_POLICY_REGISTRATION_.resource_type
    return WORKFLOW_POLICY_RESOURCE_TYPE_


def register_json_file(file_contents_func, previous_revision, resource_type):
    """The resource of `resource_type` holding the JSON that
    `file_contents_func` returns (a string or the parsed value):
    `previous_revision` (the resource registered before, or ``None``)
    updated with it, or kept as it is when it already holds it (so that a
    process start writes nothing and changes no validator), else a new
    one."""
    contents = file_contents_func()
    if type(contents) is str:
        contents = json.loads(contents)
    if previous_revision and previous_revision.content == contents:
        return previous_revision
    if previous_revision:
        resource = ResourceRegistration.update(
            contents, resource_type.slug, previous_revision.id
        )
    else:
        resource = ResourceRegistration.register(contents, resource_type.slug)
    return resource


def register_json_schema(schema_func, previous_revision):
    resource_type = register_json_schema_resource_type()
    return register_json_file(schema_func, previous_revision, resource_type)


def register_json_ld_context(context_func, previous_revision):
    resource_type = register_json_ld_resource_type()
    return register_json_file(context_func, previous_revision, resource_type)


def register_indexing_policy(indexing_policy_func, previous_revision):
    resource_type = register_indexing_policy_resource_type()
    return register_json_file(indexing_policy_func, previous_revision, resource_type)


def json_resource_type(
    resource_type_uri: str,
    slug: str,
    revision: str = '1',
    schema_resource: Resource = None,
    schema_func=None,
    schema_uri: str = None,
    context_resource: Resource = None,
    context_func=None,
    context_uri: str = None,
    viewset_class: object = None,
    indexing_policy_resource: Resource = None,
    indexing_policy_func=None,
    uses_reversion: bool = True,
    register_service: bool = True,
    checks=(),
    object_checks=(),
    content_filter: bool = False,
):
    """Registration of a single json resource type. Callers **should** provide one and
    only one of `schema_resource, schema_path, schema_uri` as well as one and only  one
    of `context_resource, context_path, context_uri`.

    Its hooks (`scrud_django.core.types.TypeOptions`), honoured by every front
    end, can only refuse what the grants allow, superusers' included (as an
    ``IsAuthorized`` subclass refuses them):

    - `checks`: callables ``(request, resource_type, body, context) -> bool``,
      which govern writes: asked of a write in each context it writes, of
      the stored resource of a PUT, PATCH or DELETE in each of its contexts
      (a resource in a refused context is neither deleted nor moved out of
      it: 403), of a saved resource in each of its contexts and of a
      workflow transition in each context it is authorized in. They never
      hide a read: a type that wants both registers both;
    - `object_checks`: callables ``(request, resource_type, obj) -> bool``,
      which govern reads and objects: asked of a resource's content wherever
      its object is checked (the detail route of every method, each listed
      resource, the saved resource). A refused one is hidden (404), and
      writing a resource they would hide is refused (403, after the save,
      which is rolled back);
    - `content_filter`: the list filters on its query parameters (on DRF,
      ``ResourceContentFilter`` among the type's view's ``filter_backends``,
      `content_filtered_viewset`).

    They replace any registered for the type's IRI before
    (`register_type_options`; a different set logs a warning),
    even when the registration is deferred.

    Returns the type's routes. When the database fails (unreachable, or not
    migrated yet), they are returned all the same and the registration is
    retried when the type is next read (`register_or_defer`).
    """
    from scrud_django import views

    register_type_options(
        resource_type_uri,
        TypeOptions(
            checks=tuple(checks),
            object_checks=tuple(object_checks),
            content_filter=content_filter,
        ),
        slug=slug,
    )
    viewset_class = viewset_class or views.ResourceViewSet
    if content_filter:
        viewset_class = content_filtered_viewset(viewset_class)

    def register():
        return ResourceTypeRegistration(
            resource_type_uri,
            revision,
            slug=slug,
            schema_resource=schema_resource,
            schema_func=schema_func,
            schema_uri=schema_uri,
            context_resource=context_resource,
            context_func=context_func,
            context_uri=context_uri,
            viewset_class=viewset_class,
            indexing_policy_func=indexing_policy_func,
            uses_reversion=uses_reversion,
            register_service=register_service,
        )

    return register_or_defer(
        register,
        resource_type_uri,
        slug,
        viewset_class,
        register_service=register_service,
    )


def content_filtered_viewset(viewset_class):
    """`viewset_class` with ``ResourceContentFilter`` among its
    ``filter_backends`` (last): itself when it has it, else a subclass of
    it, so that no other type's view gains it."""
    from scrud_django.filters import ResourceContentFilter

    backends = list(viewset_class.filter_backends)
    if ResourceContentFilter in backends:
        return viewset_class
    return type(
        viewset_class.__name__,
        (viewset_class,),
        {
            "__module__": viewset_class.__module__,
            "__qualname__": viewset_class.__qualname__,
            "__doc__": viewset_class.__doc__,
            "filter_backends": [*backends, ResourceContentFilter],
        },
    )


def computed_resource_type(
    resource_type_uri: str,
    slug: str,
    schema_func,
    context_func,
    viewset_class=None,
    compute=None,
):
    """Registration of a computed, read-only resource type
    (`scrud_django.computed.ComputedResourceViewSet`): its `ResourceType` row
    (so ``get_resource_type_for(slug)`` works, and a published branch's prefix
    claims the slug), but only the route ``<slug>/<key>/`` (named
    ``<slug>-detail``): no list route, and no reversion. The key is the URL
    keyword ``slug``, which the view passes to ``compute``.

    Returns the type's routes; the registration is retried when the type is
    next read after a database error (`register_or_defer`). A
    computed type has no list, so it is left out of the service catalog (the
    URL of each type's list).

    Its content is `compute`, ``(request, key) -> Mapping``, recorded as the
    type's (`register_type_options`, replacing any registered for its IRI
    before), which ``ComputedResourceViewSet.compute`` calls unless
    `viewset_class` overrides it. `viewset_class` defaults to
    ``ComputedResourceViewSet``; one of the two is needed."""
    from scrud_django.computed import ComputedResourceViewSet

    if viewset_class is None and compute is None:
        raise ImproperlyConfigured(
            f"computed_resource_type({slug!r}) needs compute= or viewset_class=."
        )
    register_type_options(resource_type_uri, TypeOptions(compute=compute), slug=slug)
    viewset_class = viewset_class or ComputedResourceViewSet

    def register():
        registration = ResourceTypeRegistration(
            resource_type_uri,
            slug=slug,
            schema_func=schema_func,
            context_func=context_func,
            viewset_class=viewset_class,
            uses_reversion=False,
            register_service=False,
        )
        registration.urls = computed_urls(slug, viewset_class)
        return registration

    return register_or_defer(
        register,
        resource_type_uri,
        slug,
        viewset_class,
        register_service=False,
        urls_func=computed_urls,
    )


def computed_urls(slug, viewset_class):
    """The detail route of a computed type: ``<slug>/<str:slug>/``."""
    detail = viewset_class.as_view(
        {'get': 'retrieve', 'head': 'retrieve'}, resource_type_name=slug
    )
    name = ResourceType(slug=slug).route_name_detail()
    return [path(f'{slug}/<str:slug>/', detail, name=name)]


def built_in_resource_type_urls():
    """The routes of the built-in resource types (JSON Schema, JSON-LD
    context, indexing policy and workflow policy documents), each registered
    first, or its registration left pending on a database error
    (`register_or_defer`). ``scrud_django.urls`` serves them."""
    from scrud_django import views

    def registered(register_type, registration_name):
        def register(force=False):
            register_type(force=force)
            return globals()[registration_name]

        return register

    built_in = (
        (
            register_json_schema_resource_type,
            "JSON_SCHEMA_REGISTRATION_",
            "http://json-schema.org/draft-04/schema",
            "json-schema",
            views.JsonSchemaViewSet,
        ),
        (
            register_json_ld_resource_type,
            "JSON_LD_REGISTRATION_",
            "http://www.w3.org/ns/json-ld#context",
            "json-ld",
            views.JsonLdViewSet,
        ),
        (
            register_indexing_policy_resource_type,
            "INDEXING_POLICY_REGISTRATION_",
            "http://api.openteams.com/json-ld/IndexingPolicy",
            "indexing-policies",
            views.ResourceViewSet,
        ),
        (
            workflow_policy_resource_type,
            "WORKFLOW_POLICY_REGISTRATION_",
            WORKFLOW_POLICY_TYPE_URI,
            "workflow-policies",
            views.ResourceViewSet,
        ),
    )
    urls = []
    for register_type, registration_name, type_uri, slug, viewset_class in built_in:
        register = registered(register_type, registration_name)
        urls.extend(
            register_or_defer(
                register,
                type_uri,
                slug,
                viewset_class,
                # A retry registers the type again rather than return the
                # one cached: one registered (as another type's document
                # type) in a transaction that was then rolled back is cached
                # but not stored.
                retry=functools.partial(register, force=True),
            )
        )
    return urls


def resource_types(*args):
    """Convenience function to make the configuration of resource type registrations
    more readable.
    """
    ret = []
    for urls in args:
        ret.extend(urls)
    return ret
