"""Django system checks for scrud-django (registered in ``ScrudConfig.ready``)."""

from django.core import checks

from scrud_django.conf import DefaultWorkflowPolicies

HINT = 'Fix SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"].'


def check_default_workflow_policies(app_configs, **kwargs):
    """An Error (``scrud_django.E001``) for each settings default workflow
    policy that does not pass ``WorkflowPolicy``'s checks, naming its type, or
    for a ``DEFAULT_WORKFLOW_POLICIES`` setting that cannot be loaded. At request
    time such a policy fails the writes of its type with
    ``ImproperlyConfigured``; ``manage.py check`` reports it before that.
    Stored ``WorkflowPolicy`` resources are checked when they are written
    through the API, and by the database check `check_stored_workflow_policies`
    (``scrud_django.E005``)."""
    defaults = DefaultWorkflowPolicies()
    if defaults.load_error is not None:
        return [
            checks.Error(
                str(defaults.load_error),
                hint=HINT,
                id="scrud_django.E001",
            )
        ]
    return [
        checks.Error(
            f"Invalid default workflow policy for {type_uri!r}: {error}",
            hint=HINT,
            obj=type_uri,
            id="scrud_django.E001",
        )
        for type_uri, error in defaults.errors.items()
    ]


def scrud_migrations_pending(alias):
    """Whether database `alias` lacks one of scrud_django's migrations (or
    has no migration table yet): its tables are then not all there, and
    neither the API nor ``ResourceRegistration`` can write a resource."""
    from django.db import connections
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connections[alias], ignore_no_migrations=True)
    wanted = {key for key in loader.graph.nodes if key[0] == "scrud_django"}
    return bool(wanted - set(loader.applied_migrations))


def check_stored_workflow_policies(app_configs, databases=None, **kwargs):
    """A database check (``manage.py check --database default``, and
    ``migrate``) of the stored ``WorkflowPolicy`` resources, on each of
    `databases` whose scrud_django migrations are all applied: on one that
    lacks some (`scrud_migrations_pending`, such as a 0.11 database before
    ``migrate``) it reports nothing, so that ``migrate`` can apply them; the
    policies could not be fixed or deleted before (a write needs the tables
    the migrations create). Run ``check --database <alias>`` after
    ``migrate``. It reports:

    - ``scrud_django.E002`` for each resource type and context with more than
      one: `workflow_policy_for` picks none, so the writes they govern fail
      until all but one are deleted;
    - ``scrud_django.E003`` for each whose ``resource_type`` is not a string
      (stored server-side; it governs no type);
    - ``scrud_django.E005`` for each that does not pass ``WorkflowPolicy``'s
      checks (a state condition that does not compile, a malformed transition
      or one naming an undefined state, a reserved transition name, an invalid
      ``restrict_update``, or ``initial_states``, ``states``, ``transitions``
      or conditions of the wrong type), as a policy stored before 0.12.0 or
      server-side may not, or that cannot be checked at all (any other error
      compiling it is reported, never raised): every write of its type in its contexts fails with
      ``ImproperlyConfigured`` (a 500) until it is fixed or deleted;
    - ``scrud_django.W001`` for each policy stored with a list ``rbac_context``
      (before 0.12.0, or server-side). It applies in each of those contexts, and
      updating or deleting it needs rights in all of them.
    """
    from django.core.exceptions import ImproperlyConfigured
    from django.db.utils import OperationalError, ProgrammingError

    from scrud_django.mixins.resource_mixin import rbac_contexts_of
    from scrud_django.models import Resource
    from scrud_django.workflow import WORKFLOW_POLICY_TYPE_URI, WorkflowPolicy

    found = []
    for alias in databases or ():
        if scrud_migrations_pending(alias):
            continue  # Checked once migrated (see above).
        try:
            stored = list(
                Resource.objects.using(alias)
                .filter(resource_type__type_uri=WORKFLOW_POLICY_TYPE_URI)
                .order_by("pk")
            )
        except (OperationalError, ProgrammingError):
            continue  # Not migrated yet.
        by_type_and_context = {}
        listed = []
        for policy in stored:
            content = policy.content if isinstance(policy.content, dict) else {}
            type_uri = content.get("resource_type")
            if not isinstance(type_uri, str):
                found.append(
                    checks.Error(
                        f"WorkflowPolicy pk {policy.pk} has resource_type "
                        f"{type_uri!r}; it must be a type URI (a string).",
                        hint="Fix or delete it: it governs no resource type.",
                        id="scrud_django.E003",
                    )
                )
                continue
            contexts = rbac_contexts_of(content)
            for context in contexts:
                key = (type_uri, context)
                by_type_and_context.setdefault(key, []).append(policy.pk)
            where = (
                f"in context {contexts[0]!r}"
                if len(contexts) == 1
                else f"in contexts {contexts!r}"
            )
            problem = None
            try:
                WorkflowPolicy(content)
            except ImproperlyConfigured as error:
                problem = f"does not pass WorkflowPolicy's checks: {error}"
            except Exception as error:  # noqa: BLE001
                # Never fail `check --database` or `migrate` on one policy.
                problem = f"could not be checked: {type(error).__name__}: {error}"
            if problem is not None:
                found.append(
                    checks.Error(
                        f"WorkflowPolicy pk {policy.pk} for {type_uri!r} {where} "
                        f"{problem}",
                        hint=(
                            "Fix it (a PUT through the API checks it) or delete "
                            "it (through the API or ResourceRegistration.delete) "
                            "on this migrated database: until then every write "
                            "of its type in its contexts fails with "
                            "ImproperlyConfigured (a 500)."
                        ),
                        id="scrud_django.E005",
                    )
                )
            if isinstance(content.get("rbac_context"), (list, tuple)):
                listed.append(policy)
        for (type_uri, context), pks in by_type_and_context.items():
            if len(pks) > 1:
                found.append(
                    checks.Error(
                        f"{len(pks)} WorkflowPolicy resources apply to "
                        f"{type_uri!r} in context {context!r} (pks {pks}); there "
                        "must be at most one.",
                        hint=(
                            "Delete all but one of them (through the API or "
                            "ResourceRegistration.delete), or move the others "
                            "to other contexts with a PUT through the API."
                        ),
                        id="scrud_django.E002",
                    )
                )
        for policy in listed:
            found.append(
                checks.Warning(
                    f"WorkflowPolicy pk {policy.pk} has a list rbac_context "
                    f"{list(policy.content['rbac_context'])!r}; a policy has one "
                    "context.",
                    hint="Replace it with one policy per context.",
                    id="scrud_django.W001",
                )
            )
    return found


def check_write_routing(app_configs, **kwargs):
    """An Error (``scrud_django.E004``) when the database router sends writes
    of ``Resource``, ``ResourceType``, ``ResourceContext`` and
    ``ResourceRemoval`` to different aliases (asked without an instance
    hint). ``ResourceRegistration`` writes a resource, its context rows, its
    removal stamps and its type's row in one transaction, on the
    ``Resource`` alias, and the ``WorkflowPolicy`` lock is taken there: on two
    aliases of one database, a write would wait on its own lock; on two
    databases, the ``Resource.resource_type`` foreign key would cross them.
    Not a database check: it reads the routers only."""
    from django.db import router

    from scrud_django.models import (
        Resource,
        ResourceContext,
        ResourceRemoval,
        ResourceType,
    )

    aliases = {
        model.__name__: router.db_for_write(model)
        for model in (Resource, ResourceType, ResourceContext, ResourceRemoval)
    }
    if len(set(aliases.values())) <= 1:
        return []
    routed = ", ".join(f"{name} -> {alias!r}" for name, alias in aliases.items())
    return [
        checks.Error(
            f"scrud_django's models are written to different databases: {routed}.",
            hint=(
                "Route Resource, ResourceType, ResourceContext and "
                "ResourceRemoval writes to one database alias (DATABASE_ROUTERS), "
                "and migrate all four there."
            ),
            id="scrud_django.E004",
        )
    ]


# The isolation level scrud-django's concurrency control is written for: a
# statement sees what other transactions committed before it started, and
# nothing they have not committed (READ UNCOMMITTED would let the workflow
# check read a policy whose write later rolls back on MySQL and MariaDB).
SUPPORTED_ISOLATION_LEVELS = ("read committed",)

# How to ask each backend for the isolation level of a transaction, in order
# (MySQL before 5.7.20 and MariaDB before 11.1 know only ``tx_isolation``).
ISOLATION_QUERIES = {
    "postgresql": ["SHOW transaction_isolation"],
    "mysql": [
        "SELECT @@SESSION.transaction_isolation",
        "SELECT @@SESSION.tx_isolation",
    ],
}


def transaction_isolation(alias):
    """The isolation level of a transaction on database `alias`, in lower
    case with spaces (``"read committed"``), asked inside one (PostgreSQL
    applies ``OPTIONS["isolation_level"]`` per transaction). ``None`` for a
    backend this does not ask (SQLite, which has no such levels) or
    when the level cannot be read."""
    from django.db import DatabaseError, connections, transaction

    connection = connections[alias]
    for sql in ISOLATION_QUERIES.get(connection.vendor, ()):
        try:
            with transaction.atomic(using=alias), connection.cursor() as cursor:
                cursor.execute(sql)
                row = cursor.fetchone()
        except DatabaseError:
            continue
        if row and row[0]:
            return str(row[0]).replace("-", " ").lower()
    return None


def check_isolation_level(app_configs, databases=None, **kwargs):
    """A database check: a Warning (``scrud_django.W002``) when transactions
    on the database scrud-django writes resources to
    (``router.db_for_write(Resource)``), among `databases`, run at any level
    but READ COMMITTED, the supported level. Under it, a write blocked on a row or the
    ``WorkflowPolicy`` lock reads what the writer it waited for committed. At
    REPEATABLE READ or SERIALIZABLE, PostgreSQL fails that write with a
    serialization error (a 500); MySQL and MariaDB read their snapshot, so the
    ``WorkflowPolicy`` uniqueness re-check can miss a concurrent duplicate.
    At READ UNCOMMITTED, MySQL and MariaDB let those checks read a policy
    that is never committed (PostgreSQL treats it as READ COMMITTED; it is
    warned about all the same). Django's MySQL backend uses READ COMMITTED unless
    ``OPTIONS["isolation_level"]`` says otherwise."""
    from django.db import router

    from scrud_django.models import Resource

    write = router.db_for_write(Resource)
    found = []
    for alias in databases or ():
        if alias != write:
            continue
        level = transaction_isolation(alias)
        if level is None or level in SUPPORTED_ISOLATION_LEVELS:
            continue
        if level == "read uncommitted":
            consequence = (
                "On MySQL or MariaDB the workflow check and the WorkflowPolicy "
                "uniqueness check can read a policy whose write is later rolled "
                "back."
            )
        else:
            consequence = (
                "Concurrent writes of one resource, or of WorkflowPolicy "
                "resources, can fail with a serialization error (a 500) instead "
                "of a 412 or 400, and on MySQL or MariaDB two WorkflowPolicy "
                "resources can be stored for one type and context."
            )
        found.append(
            checks.Warning(
                f"Transactions on database {alias!r}, where scrud-django writes "
                f"resources, run at {level.upper()}; scrud-django supports READ "
                f"COMMITTED. {consequence}",
                hint=(
                    f'Use READ COMMITTED: leave DATABASES[{alias!r}]["OPTIONS"]'
                    '["isolation_level"] out (or set it to read committed: '
                    'psycopg.IsolationLevel.READ_COMMITTED, or "read committed" '
                    "on MySQL), and keep the server's default isolation level "
                    "at read committed."
                ),
                id="scrud_django.W002",
            )
        )
    return found


def list_filter_errors(patterns, prefix=""):
    """An Error (``scrud_django.E006``) for each view in the URL `patterns`
    (``URLPattern`` and ``URLResolver`` entries, walked recursively) that is a
    ``ResourceViewMixin`` or ``GenericViewMixin`` viewset routed for
    ``list`` without an ``RbacFilter`` among its ``filter_backends``: its
    list answers every request with ``ImproperlyConfigured`` (a 500) rather
    than listing every object."""
    from django.urls import URLPattern, URLResolver
    from scoped_rbac.filters import RbacFilter

    from scrud_django.mixins.view_mixins import ListValidatorsMixin

    errors = []
    for entry in patterns:
        route = prefix + str(entry.pattern)
        if isinstance(entry, URLResolver):
            errors.extend(list_filter_errors(entry.url_patterns, route))
            continue
        if not isinstance(entry, URLPattern):
            continue
        view_class = getattr(entry.callback, "cls", None)
        actions = getattr(entry.callback, "actions", None) or {}
        if (
            not isinstance(view_class, type)
            or not issubclass(view_class, ListValidatorsMixin)
            or "list" not in actions.values()
        ):
            continue
        backends = getattr(view_class, "filter_backends", ()) or ()
        if any(
            isinstance(backend, type) and issubclass(backend, RbacFilter)
            for backend in backends
        ):
            continue
        errors.append(
            checks.Error(
                f"{view_class.__module__}.{view_class.__qualname__} lists at "
                f"{route!r} without an RBAC filter: every list request would be "
                "ImproperlyConfigured (a 500).",
                hint=(
                    "Put scoped_rbac.filters.RbacFilter (for resources, "
                    "scrud_django.filters.ResourceRbacFilter) in its "
                    "filter_backends."
                ),
                obj=view_class,
                id="scrud_django.E006",
            )
        )
    return errors


def check_list_filters(app_configs, **kwargs):
    """`list_filter_errors` over the project's URLconf (``ROOT_URLCONF``),
    registered with Django's ``urls`` tag, as its own URL checks are."""
    from django.urls import get_resolver

    return list_filter_errors(get_resolver().url_patterns)
