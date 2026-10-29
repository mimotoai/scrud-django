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
    Stored ``WorkflowPolicy`` resources are checked when they are written."""
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


def check_stored_workflow_policies(app_configs, databases=None, **kwargs):
    """A database check (``manage.py check --database default``, and
    ``migrate``) of the stored ``WorkflowPolicy`` resources:

    - ``scrud_django.E002`` for each resource type and context with more than
      one: `workflow_policy_for` picks none, so the writes they govern fail
      until all but one are deleted;
    - ``scrud_django.E003`` for each whose ``resource_type`` is not a string
      (stored server-side; it governs no type);
    - ``scrud_django.W001`` for each policy stored with a list ``rbac_context``
      (before 0.12.0, or server-side). It applies in each of those contexts, and
      updating or deleting it needs rights in all of them.
    """
    from django.db.utils import OperationalError, ProgrammingError

    from scrud_django.mixins.resource_mixin import rbac_contexts_of
    from scrud_django.models import Resource
    from scrud_django.workflow import WORKFLOW_POLICY_TYPE_URI

    found = []
    for alias in databases or ():
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
            for context in rbac_contexts_of(content):
                key = (type_uri, context)
                by_type_and_context.setdefault(key, []).append(policy.pk)
            if isinstance(content.get("rbac_context"), (list, tuple)):
                listed.append(policy)
        for (type_uri, context), pks in by_type_and_context.items():
            if len(pks) > 1:
                found.append(
                    checks.Error(
                        f"{len(pks)} WorkflowPolicy resources apply to "
                        f"{type_uri!r} in context {context!r} (pks {pks}); there "
                        "must be at most one.",
                        hint="Delete all but one of them.",
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
    of ``Resource``, ``ResourceType`` and ``ResourceContext`` to different
    aliases (asked without an instance hint). ``ResourceRegistration`` writes
    a resource, its context rows and its type's row in one transaction, on the
    ``Resource`` alias, and the ``WorkflowPolicy`` lock is taken there: on two
    aliases of one database, a write would wait on its own lock; on two
    databases, the ``Resource.resource_type`` foreign key would cross them.
    Not a database check: it reads the routers only."""
    from django.db import router

    from scrud_django.models import Resource, ResourceContext, ResourceType

    aliases = {
        model.__name__: router.db_for_write(model)
        for model in (Resource, ResourceType, ResourceContext)
    }
    if len(set(aliases.values())) <= 1:
        return []
    routed = ", ".join(f"{name} -> {alias!r}" for name, alias in aliases.items())
    return [
        checks.Error(
            f"scrud_django's models are written to different databases: {routed}.",
            hint=(
                "Route Resource, ResourceType and ResourceContext writes to one "
                "database alias (DATABASE_ROUTERS)."
            ),
            id="scrud_django.E004",
        )
    ]
