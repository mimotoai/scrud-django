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


# Top-level keywords of a JSON Schema through which an object may have
# properties its ``properties`` does not name.
OPEN_SCHEMA_KEYWORDS = (
    "patternProperties",
    "allOf",
    "anyOf",
    "oneOf",
    "$ref",
    "if",
    "then",
    "else",
    "dependencies",
    "dependentSchemas",
    "unevaluatedProperties",
)


def schema_properties(schema):
    """The names of the top-level properties a JSON Schema (`schema`, its
    content) defines, when they are all a resource it validates is expected
    to have: its ``properties``, unless ``additionalProperties`` allows
    others (``true`` or a schema; absent counts as not saying so) or a
    keyword such as ``patternProperties``, ``allOf`` or ``$ref`` may add
    some (`OPEN_SCHEMA_KEYWORDS`). Else ``None``: nothing can be told."""
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    if schema.get("additionalProperties", False) is not False:
        return None
    if any(keyword in schema for keyword in OPEN_SCHEMA_KEYWORDS):
        return None
    return set(properties)


def check_default_policy_conditions(app_configs, databases=None, **kwargs):
    """A database check (``manage.py check --database default``, and
    ``migrate``): a Warning (``scrud_django.W005``) for each state condition
    of a settings default workflow policy that reads a top-level property
    (`scrud_django.workflow.undeclared_properties`) which the JSON Schema of
    the policy's type, as stored on a migrated database of `databases`, does
    not define (`schema_properties`; ``rbac_context`` is always allowed).
    State conditions compile without type checking, so a misspelled
    property passes ``scrud_django.E001`` and makes its state unreachable.
    Nothing is reported for a type that is not registered there, whose
    schema is not stored (``schema_uri``) or may allow other properties, or
    for a default that is invalid (``scrud_django.E001`` reports it).
    Stored ``WorkflowPolicy`` resources are not checked. A database error
    reading a migrated database is reported (`could_not_run`,
    ``scrud_django.W007``)."""
    from django.db.utils import OperationalError, ProgrammingError

    from scrud_django.conf import DefaultWorkflowPolicies
    from scrud_django.models import ResourceType
    from scrud_django.workflow import undeclared_properties

    found = []
    policies = DefaultWorkflowPolicies().policies
    for alias in databases or ():
        if not policies or scrud_migrations_pending(alias):
            continue
        try:
            schemas = list(
                ResourceType.objects.using(alias)
                .filter(type_uri__in=list(policies), schema__isnull=False)
                .order_by("pk")
                .values_list("type_uri", "schema__content")
            )
        except (OperationalError, ProgrammingError) as error:
            found.append(could_not_run("scrud_django.W005", alias, error_text(error)))
            continue
        for type_uri, schema in schemas:
            properties = schema_properties(schema)
            if properties is None:
                continue
            properties.add("rbac_context")
            policy = policies[type_uri].policy
            for key in ("initial_states", "states"):
                for state, condition in policy.get(key, {}).items():
                    for name in undeclared_properties(condition, properties):
                        warning = checks.Warning(
                            f"The condition of state {state!r} of the default "
                            f"workflow policy for {type_uri!r} reads {name!r}, "
                            f"which the JSON Schema of {type_uri!r} does not "
                            "define: content that lacks it never meets the "
                            "condition.",
                            hint=(
                                "Fix the condition (a misspelled property?) or "
                                "define the property in the schema; a schema "
                                "that expects other properties says so with "
                                '"additionalProperties": true.'
                            ),
                            obj=type_uri,
                            id="scrud_django.W005",
                        )
                        if warning not in found:
                            found.append(warning)
    return found


def scrud_migrations_pending(alias):
    """Whether database `alias` lacks one of scrud_django's migrations (or
    has no migration table yet): its tables are then not all there, and
    neither the API nor ``ResourceRegistration`` can write a resource."""
    from django.db import connections
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(connections[alias], ignore_no_migrations=True)
    wanted = {key for key in loader.graph.nodes if key[0] == "scrud_django"}
    return bool(wanted - set(loader.applied_migrations))


def could_not_run(checked, alias, problem, hint=None):
    """A Warning (``scrud_django.W007``) that the database check reporting
    `checked` (its IDs) failed on database `alias`, which is migrated,
    with `problem` (an error, as text). On a database that is not migrated
    the database checks report nothing, so that ``migrate`` can run; on a
    migrated one an error reading it is a fault (a missing grant, a schema
    out of step with the migrations), which passing would hide."""
    return checks.Warning(
        f"The {checked} database check could not run on database {alias!r}, "
        f"which is migrated ({problem}): it reports nothing there.",
        hint=hint
        or (
            "Fix the error (a missing grant on scrud_django's tables? a schema "
            "out of step with its migrations?) and run manage.py check "
            f"--database {alias} again."
        ),
        id="scrud_django.W007",
    )


def error_text(error):
    return f"{type(error).__name__}: {error}"


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
      checks (a state condition that does not compile, a state named in both
      ``initial_states`` and ``states``, a malformed transition or one naming
      an undefined state, a reserved transition name, an invalid
      ``restrict_update``, or ``initial_states``, ``states``, ``transitions``
      or conditions of the wrong type), as a policy stored before 0.12.0 or
      server-side may not, or that cannot be checked at all (any other error
      compiling it is reported, never raised): every write of its type in its contexts fails with
      ``ImproperlyConfigured`` (a 500) until it is fixed or deleted;
    - ``scrud_django.W001`` for each policy stored with a list ``rbac_context``
      (before 0.12.0, or server-side). It applies in each of those contexts, and
      updating or deleting it needs rights in all of them.

    A database error reading a migrated database is reported
    (`could_not_run`, ``scrud_django.W007``).
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
        except (OperationalError, ProgrammingError) as error:
            found.append(
                could_not_run(
                    "scrud_django.E002, E003, E005 and W001",
                    alias,
                    error_text(error),
                )
            )
            continue
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
            # A context the policy lists twice is one policy there, as
            # stored_policies_for counts it.
            for context in dict.fromkeys(contexts):
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


def check_overlong_contexts(app_configs, databases=None, **kwargs):
    """A database check (``manage.py check --database default``, and
    ``migrate``): a Warning (``scrud_django.W003``) naming the resources, on
    each migrated database of `databases` (`scrud_migrations_pending`), with
    a context longer than ``MAX_CONTEXT_BYTES`` (2048) bytes of UTF-8, which
    only a resource stored before 0.12.0 can have. Migration 0008 gave such a
    context no ``ResourceContext`` row: lists leave the resource out in it
    (superusers' lists excepted), and saving its content fails with
    ``ValueError`` until it is given a shorter context. Only resources whose
    ``rbac_context`` is over 512 characters long as JSON text are read (no
    shorter one can be over 2048 bytes). A database error reading a migrated
    database is reported (`could_not_run`, ``scrud_django.W007``)."""
    from django.db.models.fields.json import KeyTextTransform
    from django.db.models.functions import Length
    from django.db.utils import OperationalError, ProgrammingError

    from scrud_django.mixins.resource_mixin import rbac_contexts_of
    from scrud_django.models import Resource
    from scrud_django.models.resource import MAX_CONTEXT_BYTES, context_fits

    found = []
    for alias in databases or ():
        if scrud_migrations_pending(alias):
            continue
        candidates = (
            Resource.objects.using(alias)
            .alias(
                _scrud_context_length=Length(
                    KeyTextTransform("rbac_context", "content")
                )
            )
            .filter(_scrud_context_length__gt=MAX_CONTEXT_BYTES // 4)
            .order_by("pk")
            .values_list("pk", "content")
        )
        try:
            pks = [
                pk
                for pk, content in candidates.iterator()
                if not all(map(context_fits, rbac_contexts_of(content)))
            ]
        except (OperationalError, ProgrammingError) as error:
            found.append(could_not_run("scrud_django.W003", alias, error_text(error)))
            continue
        if not pks:
            continue
        shown = ", ".join(map(str, pks[:20])) + (", ..." if len(pks) > 20 else "")
        found.append(
            checks.Warning(
                f"{len(pks)} resources on database {alias!r} have an rbac_context "
                f"longer than {MAX_CONTEXT_BYTES} bytes in UTF-8 (pks [{shown}]): "
                "they have no ResourceContext row for it, so lists leave them out "
                "in that context (superusers' lists excepted), and saving their "
                "content fails with ValueError.",
                hint=(
                    "Give each a shorter rbac_context (a PUT through the API, or "
                    "ResourceRegistration.update), or delete it."
                ),
                id="scrud_django.W003",
            )
        )
    return found


def check_context_drift(app_configs, databases=None, **kwargs):
    """A database check (``manage.py check --database default``, and
    ``migrate``): a Warning (``scrud_django.W004``) naming the resources, on
    each migrated database of `databases` (`scrud_migrations_pending`), that
    have no ``ResourceContext`` row although their content names a context
    a row can hold: written without ``Resource.save()`` (a 0.11 process still
    writing after ``migrate``, ``bulk_create()``). Lists leave them out
    (superusers' lists excepted).

    It runs one anti-join (resources without a row) and reads the content of
    those only. It cannot see a resource whose rows name a context its
    content left (a 0.11 process, or ``QuerySet.update()``, moved it): that
    takes comparing every resource's content with its rows, which
    ``manage.py scrud_sync_contexts --check`` does, too costly for a check
    that every ``migrate`` runs. A database error reading a migrated database
    is reported (`could_not_run`, ``scrud_django.W007``)."""
    from django.db.utils import OperationalError, ProgrammingError

    from scrud_django.models import Resource
    from scrud_django.models.resource import context_rows_of

    found = []
    for alias in databases or ():
        if scrud_migrations_pending(alias):
            continue
        candidates = (
            Resource.objects.using(alias)
            .filter(rbac_context_rows__isnull=True)
            .order_by("pk")
            .values_list("pk", "content")
        )
        try:
            pks = [
                pk for pk, content in candidates.iterator() if context_rows_of(content)
            ]
        except (OperationalError, ProgrammingError) as error:
            found.append(could_not_run("scrud_django.W004", alias, error_text(error)))
            continue
        if not pks:
            continue
        shown = ", ".join(map(str, pks[:20])) + (", ..." if len(pks) > 20 else "")
        found.append(
            checks.Warning(
                f"{len(pks)} resources on database {alias!r} have no "
                f"ResourceContext row although their content names a context (pks "
                f"[{shown}]): they were written without Resource.save() (a 0.11 "
                "process still running after migrate, bulk_create()), and lists "
                "leave them out (superusers' lists excepted).",
                hint=(
                    f"Run manage.py scrud_sync_contexts --database {alias}, which "
                    "also re-syncs resources whose rows name a context they left "
                    "(this check does not see those)."
                ),
                id="scrud_django.W004",
            )
        )
    return found


def check_write_routing(app_configs, **kwargs):
    """An Error (``scrud_django.E004``) when the database router sends writes
    of ``Resource``, ``ResourceType``, ``ResourceContext`` and
    ``ResourceRemoval`` to different aliases (asked without an instance
    hint). ``ResourceRegistration`` writes a resource, its context rows and
    its removal stamps in one transaction, on the ``Resource`` alias, and the
    ``WorkflowPolicy`` lock is taken there: on two
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
    ``WorkflowPolicy`` lock reads what the writer it waited for committed. On
    PostgreSQL, at REPEATABLE READ or SERIALIZABLE the second of two
    concurrent writes of one resource fails with a serialization error (a
    500). At REPEATABLE READ a conditional POST to a list, and a
    ``WorkflowPolicy`` create, read a snapshot taken before the writer they
    waited for committed, so two with the same ``If-Match``, or two policies
    for one type and context, can both be created; at SERIALIZABLE the
    second of each fails with a serialization error (a 500). MySQL and
    MariaDB at REPEATABLE READ read their snapshot, so the ``WorkflowPolicy``
    uniqueness re-check can miss a concurrent duplicate (SERIALIZABLE is not
    tested there); see ``docs/follow-ups.md#policy-recheck-isolation``.
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
        elif level == "repeatable read":
            consequence = (
                "On PostgreSQL the second of two concurrent writes of one "
                "resource fails with a serialization error (a 500) instead of a "
                "412 or 400, and two POSTs to a list with the same If-Match, or "
                "two WorkflowPolicy resources for one type and context, can both "
                "be created, with no error; on MySQL or MariaDB two "
                "WorkflowPolicy resources can be stored for one type and context."
            )
        else:
            consequence = (
                "On PostgreSQL the second of two concurrent writes of one "
                "resource, of two POSTs to a list with the same If-Match, or of "
                "two WorkflowPolicy resources for one type and context fails "
                "with a serialization error (a 500) instead of a 412 or 400; on "
                "MySQL and MariaDB these writes are not tested at this level."
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


ACTIONS_HINT = (
    "Each entry of a mapping SCRUD_WORKFLOW_ACTIONS names maps a type URI to "
    "{'on_save': callable, 'on_delete': callable} (either key may be left out, "
    "or given None: no action)."
)


def check_workflow_actions(app_configs, **kwargs):
    """The entries of ``SCRUD_WORKFLOW_ACTIONS``, as `workflow_actions` merges
    them (read afresh, not as ``ScrudSignalProcessor`` keeps them loaded). At write time a bad entry runs no action and is logged on the
    ``scrud_django.workflow_actions`` logger at every write of its type;
    this reports it before that:

    - ``scrud_django.E007`` for a type whose entry is not a mapping, or for a
      setting that cannot be loaded at all (``ScrudConfig.ready()`` refuses
      that at startup, so only a setting changed since gets here);
    - ``scrud_django.E008`` for a key other than ``on_save`` and
      ``on_delete`` (a misspelled one runs nothing);
    - ``scrud_django.E009`` for a value that is neither callable nor ``None``
      (a dotted path is not imported).

    Whether each type is registered takes the database:
    `check_workflow_action_types` (``scrud_django.W006``)."""
    from collections.abc import Mapping

    from django.core.exceptions import ImproperlyConfigured

    from scrud_django.conf import WORKFLOW_ACTION_KINDS, workflow_actions

    try:
        all_actions = workflow_actions()
    except (ImproperlyConfigured, ImportError) as error:
        return [
            checks.Error(
                f"SCRUD_WORKFLOW_ACTIONS cannot be loaded: {error}",
                hint=ACTIONS_HINT,
                id="scrud_django.E007",
            )
        ]
    found = []
    allowed = ", ".join(map(repr, WORKFLOW_ACTION_KINDS))
    for type_uri, entry in all_actions.items():
        if not isinstance(entry, Mapping):
            found.append(
                checks.Error(
                    f"SCRUD_WORKFLOW_ACTIONS: the entry for {type_uri!r} is "
                    f"{entry!r}, not a mapping: no action runs for its writes.",
                    hint=ACTIONS_HINT,
                    obj=type_uri,
                    id="scrud_django.E007",
                )
            )
            continue
        for key, action in entry.items():
            if key not in WORKFLOW_ACTION_KINDS:
                found.append(
                    checks.Error(
                        f"SCRUD_WORKFLOW_ACTIONS: the entry for {type_uri!r} has "
                        f"the unknown key {key!r}, which never runs.",
                        hint=f"Its keys must be among {allowed}.",
                        obj=type_uri,
                        id="scrud_django.E008",
                    )
                )
            elif action is not None and not callable(action):
                found.append(
                    checks.Error(
                        f"SCRUD_WORKFLOW_ACTIONS: the {key} action for "
                        f"{type_uri!r} is {action!r}, which is not callable: "
                        "it never runs.",
                        hint=(
                            "Give the function itself (import it in the module "
                            "of the mapping), not its dotted path."
                        ),
                        obj=type_uri,
                        id="scrud_django.E009",
                    )
                )
    return found


def check_workflow_action_types(app_configs, databases=None, **kwargs):
    """A database check (``manage.py check --database <alias>``, and
    ``migrate``): a Warning (``scrud_django.W006``) for each type URI in
    ``SCRUD_WORKFLOW_ACTIONS`` that is not a registered resource type on the
    database ``ResourceType`` is written to (``router.db_for_write``), if it
    is among `databases` and migrated (`scrud_migrations_pending`): a
    misspelled type URI runs nothing. Resource types are registered when the
    URLconf is imported, which this does first (as Django's URL checks do).
    A setting that cannot be loaded is `check_workflow_actions`'s to
    report. A URLconf that cannot be imported, or a database error reading
    the migrated database, is reported (`could_not_run`,
    ``scrud_django.W007``)."""
    from django.core.exceptions import ImproperlyConfigured
    from django.db import router
    from django.db.utils import OperationalError, ProgrammingError
    from django.urls import get_resolver

    from scrud_django.conf import workflow_actions
    from scrud_django.models import ResourceType

    alias = router.db_for_write(ResourceType)
    if alias not in (databases or ()) or scrud_migrations_pending(alias):
        return []
    try:
        type_uris = list(workflow_actions())
    except (ImproperlyConfigured, ImportError):
        return []
    try:
        get_resolver().url_patterns  # noqa: B018 (registers the types)
    except Exception as error:  # noqa: BLE001
        return [
            could_not_run(
                "scrud_django.W006",
                alias,
                f"the URLconf could not be imported: {error_text(error)}",
                hint=(
                    "Fix the URLconf (ROOT_URLCONF), which registers the resource "
                    f"types, and run manage.py check --database {alias} again."
                ),
            )
        ]
    try:
        registered = set(
            ResourceType.objects.using(alias).values_list("type_uri", flat=True)
        )
    except (OperationalError, ProgrammingError) as error:
        return [could_not_run("scrud_django.W006", alias, error_text(error))]
    return [
        checks.Warning(
            f"SCRUD_WORKFLOW_ACTIONS has actions for {type_uri!r}, which is not a "
            f"resource type registered on database {alias!r}: they never run.",
            hint=(
                "Fix the type URI (it is the resource type's type_uri, as "
                "registered with ResourceTypeRegistration), or remove the entry."
            ),
            obj=type_uri,
            id="scrud_django.W006",
        )
        for type_uri in type_uris
        if type_uri not in registered
    ]
