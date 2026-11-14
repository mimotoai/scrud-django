"""A resource's write without DRF: the body's validation (its type's JSON
Schema, a ``WorkflowPolicy``'s own checks, the workflow check), the create,
update or delete, and the check of the resource as stored before the write
commits. Every scrud front end writes through these (DRF through
``ResourceViewMixin``), with a Django request, the `ResourceType`, the
parsed body and a neutral requirement
(`scrud_django.core.requirements`).

A front end validates the body first (`validate_write`, or its parts: DRF's
serializer validators run `validate_schema`, then the policy checks and the
workflow check, each once), then writes it (`create`, `update`, `destroy`,
or `save_checked` with a save of its own) inside its transaction
(`write_transaction`; a conditional create also under
`conditional_create_lock`). `create` and `update` always validate the
body they save themselves (`validate_write`); `save_checked` trusts the
workflow check's mark for one save (it consumes it), and cannot tell
which body was validated. Access errors (`scoped_rbac.access.AccessError`)
are raised for the front end to answer as it answers them.
"""

import copy
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager

import fastjsonschema
import reversion
from django.db import transaction
from django.db.models import ProtectedError
from scoped_rbac import access
from scoped_rbac.policy import Permission

from scrud_django.core.authz import (
    _consume_workflow_mark,
    content_of,
    may_act_in_context,
)
from scrud_django.core.requirements import (
    asks_grants_itself,
    is_hook,
    require_grants,
    requirement_leaves,
)
from scrud_django.core.types import HookLeaf
from scrud_django.core.workflow import (
    authorizing_write,
    check_policy_is_unique,
    prior_of,
    validate_policy_write,
    workflow_check,
)
from scrud_django.exceptions import (
    JsonSchemaInvalidError,
    JsonValidationError,
    ResourceInUseError,
)
from scrud_django.models import Resource, ResourceType
from scrud_django.registration import ResourceRegistration, type_locked
from scrud_django.workflow import (
    WORKFLOW_POLICY_TYPE_URI,
    TransitionData,
    WorkflowPolicy,
    lock_workflow_policy_type,
)

# The logger keeps its historical name: operators' log filters match it.
logger = logging.getLogger("scrud_django.validators")


class ValidatorEntry:
    def __init__(self, resource_type: ResourceType, validate: callable):
        self.type_uri = resource_type.type_uri
        self.revision = resource_type.revision
        self.etag = resource_type.schema.etag
        self.modified_at = resource_type.schema.modified_at
        self.validate = validate

    def matches(self, resource_type: ResourceType):
        return (
            self.type_uri == resource_type.type_uri
            and self.revision == resource_type.revision
            and self.etag == resource_type.schema.etag
            and self.modified_at == resource_type.schema.modified_at
        )


JSON_SCHEMA_TYPE_URIS = (
    "http://json-schema.org/draft-04/schema",
    "http://json-schema.org/draft-06/schema",
    "http://json-schema.org/draft-07/schema",
)


class RefusedRemoteRef(fastjsonschema.JsonSchemaDefinitionException):
    pass


def refuse_remote_ref(uri):
    raise RefusedRemoteRef(
        f"$ref {uri!r} is not allowed: a $ref must point into the schema itself "
        "(start with '#')."
    )


class RefuseEveryScheme(dict):
    """fastjsonschema's ``handlers``: it fetches a ``$ref`` outside the schema
    with the handler for its URI scheme, else with ``urlopen``. This mapping has
    a handler for every scheme (``http``, ``file``, none...), which refuses."""

    def __contains__(self, scheme):
        return True

    def __getitem__(self, scheme):
        return refuse_remote_ref

    def get(self, scheme, default=None):
        return refuse_remote_ref


def compile_json_schema(content):
    """Compile JSON Schema `content` with fastjsonschema, resolving ``$ref`` only
    within it. Raises ``JsonSchemaInvalidError`` if it does not compile, a
    ``$ref`` to anything else (a URL, a file, another document) included. The
    content is not changed. The validator applies no ``default``
    (``use_default=False``): it validates the data as it is, and leaves it as
    it is."""
    content = copy.deepcopy(content)
    if isinstance(content, dict):
        content.pop("$id", None)
    try:
        return fastjsonschema.compile(
            content, handlers=RefuseEveryScheme(), use_default=False
        )
    except Exception as e:
        raise JsonSchemaInvalidError(f"The JSON Schema does not compile: {e}") from e


class JsonValidator:
    """Validates a body against its type's JSON Schema (`validate_schema`),
    as a DRF serializer validator (``requires_context``: its context names
    the ``resource_type``). A body written to a JSON Schema type is itself a
    schema, and must compile (`compile_json_schema`). A stored schema that
    does not compile fails closed: every write of its type is refused with
    ``JsonSchemaInvalidError`` (400).

    JSON Schema ``default`` values are not applied: the validator is compiled
    with ``use_default=False`` (`compile_json_schema`), so it validates `data`,
    the body that is stored, exactly as it was sent (a default can neither
    make it valid, for example through ``if``/``then``, nor be stored), and
    leaves it unchanged."""

    requires_context = True

    def __init__(self):
        self.validators = {}

    def __call__(self, data, context):
        validate_schema(context.resource_type, data, validators=self)
        return data

    def is_json_schema(self, schema):
        return (
            schema is not None
            and schema.resource_type.type_uri in JSON_SCHEMA_TYPE_URIS
        )

    def validator_for(self, resource_type):
        schema = resource_type.schema
        if not self.is_json_schema(schema):
            return lambda r: r
        validator = self.validators.get(resource_type.type_uri, None)
        if validator is None or not validator.matches(resource_type):
            try:
                validation_func = compile_json_schema(schema.content)
            except JsonSchemaInvalidError as error:
                # The writer of a resource may not be the schema's author: the
                # reason is logged, not sent.
                logger.error(
                    "The JSON Schema of %s does not compile, so its writes are "
                    "refused: %s",
                    resource_type.type_uri,
                    error.detail,
                )

                def validate(content):
                    raise JsonSchemaInvalidError(
                        "The JSON Schema of this resource type does not compile, "
                        "so it cannot be validated."
                    )

            else:

                def validate(content):
                    try:
                        return validation_func(content)
                    except fastjsonschema.JsonSchemaException as e:
                        raise JsonValidationError(e) from e

            validator = ValidatorEntry(resource_type, validate)
            self.validators[resource_type.type_uri] = validator
        return validator.validate


# The compiled schemas `validate_schema` uses unless given others.
SCHEMA_VALIDATORS = JsonValidator()


def validate_schema(resource_type, body, *, validators=None) -> None:
    """Refuse `body` unless it is valid under `resource_type`'s JSON Schema
    (``JsonValidationError``; ``JsonSchemaInvalidError`` when that schema
    does not compile), compiled once by `validators` (a `JsonValidator`, by
    default `SCHEMA_VALIDATORS`). A body written to a JSON Schema type must
    itself compile (``JsonSchemaInvalidError``)."""
    if validators is None:
        validators = SCHEMA_VALIDATORS
    if resource_type.type_uri in JSON_SCHEMA_TYPE_URIS:
        compile_json_schema(body)
    validators.validator_for(resource_type)(body)


def validate_write(
    request,
    resource_type,
    body: Mapping,
    *,
    stored: Resource | None,
    requirement,
    narrow: Sequence[Callable] | None = None,
    readable: Callable[[], bool] | None,
    type_iri: str | None = None,
) -> list[tuple[str | None, WorkflowPolicy, TransitionData]]:
    """Validate `request`'s write of `body` (the body as sent) to
    `resource_type`, over the resource `stored` (``None`` for a create).

    The front end runs `scrud_django.core.bodies.check_body` on the body
    first: DRF answers a malformed body (empty, too deep, with a NUL, an
    ``rbac_context`` that cannot be stored) 400 before any 403.

    First, any mark an earlier validation left on the request is removed
    (`_consume_workflow_mark`): this validation replaces it, and one that
    fails leaves none for a save to use. Then the request check of the
    write with its body (`scrud_django.core.resources.check_request`: of
    the list's POST for a create, of the item's route for an update), whose
    refusal is ``access.AccessDenied`` (403), as the DRF route asks
    ``has_permission`` with the body before anything else validates it.
    For an update, then the object check of `stored` with the body
    (`scrud_django.core.resources.check_object` given `body`,
    ``scrud_workflow`` allowing for the PUT the workflow check covers), as
    DRF's ``get_object()`` asks it: a caller that looked `stored` up itself
    (no `load_for_read`) is checked all the same, so a writer cannot move a
    resource out of a context where they may not write it; and DRF's ``|``
    pairs each option's request check, reading the body, with that option's
    object check, which a ``load_for_read`` given no body could not (it
    asks whether the requester may PUT in some context). A refusal is
    ``access.AccessDenied``, or ``Http404`` for a resource the requester
    cannot read. Then, in the order DRF's serializer validators check it:
    its JSON Schema (`validate_schema`), then, for a ``WorkflowPolicy``, the
    policy's own checks (`validate_policy_write`), then the workflow check
    (`scrud_django.core.workflow.workflow_check`, given `stored`'s content
    as its ``prior``, `prior_of`, and `narrow` and `readable` as they are).
    Returns the transitions the workflow check authorized; the request
    then carries their mark for this one write (``(type_iri,
    stored.pk)``, ``None`` as the pk for a create), which the save that
    uses it consumes (`save_checked`). `type_iri` is the type the write's
    post-save check names (by default `resource_type`'s ``type_uri``): the
    save must be given the same one, or its check does not find the mark.

    `requirement` (required: the type's, `scrud_django.core.types
    .requirement_for`) gives the `narrow`: the checks of each type's hooks
    in it (each `HookLeaf`'s ``narrow``), so that a registered check can
    refuse a transition, as on DRF. A given `narrow` replaces them.

    A `requirement` that asks no grants (empty, or hooks only) is
    ``ImproperlyConfigured`` (`scrud_django.core.requirements.require_grants`).

    `create` and `update` always call it themselves, on the body they save
    (a caller that called it first gets the workflow check, and an update's
    object check, run twice, with no effect but the mark). A front end that
    saves with `save_checked` itself calls it first, with the same request
    and body, inside the write's transaction."""
    # Imported here: scrud_django.core.resources imports this module.
    from scrud_django.core.resources import (
        COVERED_ITEM_METHOD,
        check_object,
        check_request,
    )

    # Whatever an earlier validation on the request authorized, this one
    # replaces it: refused, it leaves no mark for a save to use.
    _consume_workflow_mark(request)
    check_request(
        request, resource_type, requirement, list_route=stored is None, body=body
    )
    if stored is not None:
        # DRF's get_object() asks the object check of the stored resource:
        # a caller that looked it up itself must not skip it. With the body,
        # as DRF's | pairs an option's request check (with the body) with
        # its object check, which a body-less load_for_read could not.
        check_object(
            request,
            resource_type,
            stored,
            requirement,
            covered=request.method == COVERED_ITEM_METHOD,
            body=body,
        )
    if narrow is None:
        narrow = tuple(
            check
            for leaf in requirement_leaves(requirement)
            if isinstance(leaf, HookLeaf)
            for check in leaf.narrow
        )
    validate_schema(resource_type, body)
    if resource_type.type_uri == WORKFLOW_POLICY_TYPE_URI:
        validate_policy_write(body, stored.pk if stored is not None else None)
    return workflow_check(
        request,
        type_iri=resource_type.type_uri,
        prior=prior_of(stored),
        proposed=body,
        narrow=narrow,
        readable=readable,
        stored_pk=stored.pk if stored is not None else None,
        post_save_type_iri=type_iri,
    )


def check_post_save(
    request,
    resource_type,
    obj: Resource,
    requirement,
    *,
    type_iri: str | None = None,
    content: Mapping | None = None,
) -> None:
    """Deny (``access.AccessDenied``) unless the request's action (POST for a
    create, PUT for an update) on the type `type_iri` (by default
    `resource_type`'s ``type_uri``; a front end passes the type it names
    the resource by) is allowed in **every** context the saved `obj` is in,
    for each leaf of `requirement` (``AnyOf`` alternatives included: each
    must allow). ``rbac_contexts`` is any-of for reads: a write may land the
    resource only where the writer may write, in each of those contexts.

    Each leaf is given `content`, the saved resource as object checks get it
    (by default its content, `content_of`): its ``has_object_permission``
    must allow it (superusers only for a resource in no usable context), and
    its ``has_context_permission`` must allow it in each context its
    ``contexts_for`` names; for a leaf that does not ask the grants itself,
    each context read as the resource is there (`asks_grants_itself`: of a
    trusted class, ``reads_each_context`` true), the grants must too, each
    context read so (`may_act_in_context`), unless it is a type's hooks
    (`is_hook`). A leaf of any other class gets them asked, whatever it
    says."""
    check = content_of(obj) if content is None else content
    if type_iri is None:
        type_iri = resource_type.type_uri
    permission = Permission(access.action_iri_for(request.method), type_iri)
    for leaf in requirement_leaves(requirement):
        contexts = access.usable_contexts(leaf.contexts_for(check))
        allowed = leaf.has_object_permission(request, check) and all(
            leaf.has_context_permission(request, check, context) for context in contexts
        )
        if allowed and not (asks_grants_itself(leaf) or is_hook(leaf)):
            # Its own has_context_permission (and its super() call) read
            # the content as stored, or its class is not trusted to ask
            # the grants: they are asked too, each context as the resource
            # is there. Object checks only narrow.
            allowed = all(
                may_act_in_context(request, permission, check, [context])
                for context in contexts
            )
        if not allowed:
            raise access.AccessDenied()


def check_every_context_of_policy(
    request,
    instance: Resource,
    requirement,
    *,
    covered: bool,
    type_iri: str | None = None,
    check: Callable | None = None,
) -> None:
    """For a ``WorkflowPolicy`` `instance`, deny (``access.AccessDenied``)
    an update or delete unless the request's action is allowed in **every**
    context the stored policy applies to: it governs each of them. The
    object check is any-of; a policy written through the API has one
    context, but one stored with a list (before 0.12.0, or server-side) has
    several.

    The check is `check` ``(request, instance) -> None``, by default
    `check_post_save` of `requirement` naming the type `type_iri` (neither
    is read when a `check` is given); either runs in `authorizing_write`
    with `covered`."""
    if instance.resource_type.type_uri != WORKFLOW_POLICY_TYPE_URI:
        return
    if check is None:

        def check(request, instance):
            check_post_save(
                request,
                instance.resource_type,
                instance,
                requirement,
                type_iri=type_iri,
            )

    with authorizing_write(request, covered=covered):
        check(request, instance)


def serialized_policy_save(save, using: str, *, lock=None):
    """`save` for a ``WorkflowPolicy``: inside the write's transaction on
    `using`, take the ``WorkflowPolicy`` type lock (`lock`, by default
    `lock_workflow_policy_type`), save, and re-check that the saved policy
    is the only one for its type in its contexts (`check_policy_is_unique`,
    400 and rolled back otherwise). The validation's check ran before the
    lock; a concurrent write of the same type and context committed since is
    seen here."""

    def locked_save():
        (lock or lock_workflow_policy_type)(using)
        saved = save()
        check_policy_is_unique(saved.content, saved.pk)
        return saved

    return locked_save


def cache_resource_type(resource_type, *objects) -> None:
    """Cache `resource_type` on each `Resource` of `objects` (lists and
    tuples of them too) that is of it."""
    for obj in objects:
        if isinstance(obj, (list, tuple)):
            cache_resource_type(resource_type, *obj)
        elif isinstance(obj, Resource) and obj.resource_type_id == resource_type.pk:
            Resource.resource_type.field.set_cached_value(obj, resource_type)


def save_checked(
    request,
    resource_type,
    save: Callable[[], Resource | None],
    *,
    instance: Resource | None,
    requirement,
    using: str,
    covered: bool,
    queryset=None,
    serialize=None,
    type_iri: str | None = None,
    recheck: Callable | None = None,
) -> Resource:
    """Run `save` (which returns the saved resource, or ``None`` for
    `instance`) in a transaction on `using` and check the resource as stored
    before it commits: a reserved context is a 400 and the post-save check a
    403, either rolling the write back. The post-save check is `recheck`
    ``(request, obj) -> None``, by default `check_post_save` of
    `requirement` naming the type `type_iri` (by default `resource_type`'s
    ``type_uri``; `requirement` is not read when a `recheck` is given);
    either runs in `authorizing_write` with `covered`, ``scrud_workflow``
    allowing only once the workflow check has authorized this write: the
    request carries the mark (`_workflow_authorized`) of `type_iri` and
    `instance`'s pk (``None`` for a create), so a front end whose
    `recheck` names another type passes it as `type_iri`. The mark is
    consumed (`_consume_workflow_mark`) once the save is over, however it
    ends: one validation authorizes at most one save. The mark names the
    write, not the body: `save_checked` cannot tell whether the body `save`
    writes is the one validated, and trusts its caller to save that body
    (`create` and `update` validate the body they save; follow-up
    ``core-save-checked-mark-body``). The request was
    authorized by the context its body names; this checks where the
    resource actually landed (scoped-rbac's ``SaveCheck``). `instance` is
    the resource an update replaces (``None`` for a create). Returns the
    saved resource as read back, through `queryset` (by default
    `resource_type`'s resources).

    A ``WorkflowPolicy``'s save is serialised (`serialize`, by default
    `serialized_policy_save`).

    The check refreshes the saved resource from its row, which empties its
    cache of related objects: `resource_type` is cached on it again, and on
    what is returned (`cache_resource_type`), so that what reads it later
    (a ``Location``, and what runs on commit with it: search indexing, the
    ``SCRUD_WORKFLOW_ACTIONS`` `ScrudSignalProcessor` defers) does not load
    it through the read router. The check and this re-caching run in one
    transaction on `using`, so that a write outside a transaction of the
    caller's commits only once the type is cached again.

    Without a `recheck`, a `requirement` that asks no grants (empty, or
    hooks only) is ``ImproperlyConfigured`` (`require_grants`), before
    anything is saved: its post-save check would let any write land
    anywhere. (`check_post_save` itself takes one: a DRF view with no
    ``IsAuthorized`` checks nothing after the save.) `create` and `update`
    give no `recheck`.

    Raises ``access.AccessError``s for the caller to answer."""
    try:
        if recheck is None:
            require_grants(requirement, "save_checked")
        if type_iri is None:
            type_iri = resource_type.type_uri
        stored_pk = instance.pk if instance is not None else None
        saved = []

        def recorded_save():
            result = save()
            saved.append(instance if result is None else result)
            return result

        if recheck is None:

            def recheck(request, obj):
                check_post_save(
                    request, resource_type, obj, requirement, type_iri=type_iri
                )

        def recheck_saved(obj, _moved_to):
            with authorizing_write(
                request,
                covered=covered,
                authorized=True,
                type_iri=type_iri,
                stored_pk=stored_pk,
            ):
                recheck(request, obj)

        if queryset is None:
            queryset = Resource.objects.filter(resource_type=resource_type)
        check = access.SaveCheck(
            request,
            written_contexts=lambda obj: obj.rbac_contexts,
            recheck=recheck_saved,
            queryset=queryset,
        )
        run = recorded_save
        if resource_type.type_uri == WORKFLOW_POLICY_TYPE_URI:
            run = (serialize or serialized_policy_save)(recorded_save, using)
        with transaction.atomic(using=using):
            if instance is None:
                result = check.create(run, db=using)
            else:
                result = check.update(instance, run, db=using)
            cache_resource_type(resource_type, *saved, result)
        return result
    finally:
        # One validation authorizes at most one save, however it ends.
        _consume_workflow_mark(request)


@contextmanager
def creating_revision(resource_type):
    """The block in which a resource of `resource_type` is created: a
    reversion revision commented ``Add Resource`` when the type uses
    reversion (``uses_reversion``), else nothing."""
    if not resource_type.uses_reversion:
        yield
        return
    with reversion.create_revision():
        yield
        reversion.set_comment('Add Resource')


def _validated(
    request,
    resource_type,
    body,
    *,
    stored: Resource | None,
    requirement,
    type_iri: str,
):
    """Validate `request`'s write of `body` over `stored` (``None`` for a
    create) with `validate_write`, whether `stored` is readable
    (`object_readable`) telling whether its errors are hidden: what
    `create` and `update` run on the body they save, always."""
    # Imported here: scrud_django.core.resources imports this module.
    from scrud_django.core.resources import object_readable

    readable = None
    if stored is not None:

        def readable():
            return object_readable(request, resource_type, stored, requirement)

    validate_write(
        request,
        resource_type,
        body,
        stored=stored,
        readable=readable,
        requirement=requirement,
        type_iri=type_iri,
    )


def create(
    request,
    resource_type,
    body,
    *,
    requirement,
    using: str,
    covered: bool = True,
    type_iri: str | None = None,
) -> Resource:
    """Create a resource of `resource_type` whose content is `body` with
    ``ResourceRegistration.register_instance`` (in `creating_revision`),
    checked as stored (`save_checked`), once `body` itself is validated
    (`_validated`: `validate_write`, always). `type_iri` is the type the
    post-save check names (by default `resource_type`'s ``type_uri``).

    It takes no ``recheck``: its post-save check is `requirement`'s
    (`check_post_save`). A front end needing a post-save check of its own
    saves with `save_checked` itself (follow-up ``core-write-no-recheck``)."""
    # One type IRI for the workflow check's mark and the post-save check.
    if type_iri is None:
        type_iri = resource_type.type_uri
    _validated(
        request,
        resource_type,
        body,
        stored=None,
        requirement=requirement,
        type_iri=type_iri,
    )

    def save():
        with creating_revision(resource_type):
            return ResourceRegistration.register_instance(
                Resource(content=dict(body), resource_type=resource_type)
            )

    return save_checked(
        request,
        resource_type,
        save,
        instance=None,
        requirement=requirement,
        using=using,
        covered=covered,
        type_iri=type_iri,
    )


def update(
    request,
    resource_type,
    instance: Resource,
    body,
    *,
    requirement,
    using: str,
    covered: bool = True,
    type_iri: str | None = None,
) -> Resource:
    """Write `body` to `instance` (read with its row locked inside the
    caller's transaction on `using`) with
    ``ResourceRegistration.update_locked``, checked as stored
    (`save_checked`), once `body` itself is validated over `instance`
    (`_validated`: `validate_write`, always, whether `instance` is readable
    telling whether its errors are hidden). That validation asks the
    object check of `instance` with `body` (`check_object`), as DRF's
    ``get_object()`` does, whether or not the caller loaded it with
    `scrud_django.core.resources.load_for_read`. `type_iri` is the type the
    post-save check names (by default `resource_type`'s ``type_uri``).

    It takes no ``recheck``: its post-save check is `requirement`'s
    (`check_post_save`). A front end needing a post-save check of its own
    saves with `save_checked` itself (follow-up ``core-write-no-recheck``)."""
    # One type IRI for the workflow check's mark and the post-save check.
    if type_iri is None:
        type_iri = resource_type.type_uri
    _validated(
        request,
        resource_type,
        body,
        stored=instance,
        requirement=requirement,
        type_iri=type_iri,
    )
    return save_checked(
        request,
        resource_type,
        lambda: ResourceRegistration.update_locked(
            instance, body, resource_type=resource_type, using=using
        ),
        instance=instance,
        requirement=requirement,
        using=using,
        covered=covered,
        type_iri=type_iri,
    )


def destroy(
    request,
    resource_type,
    instance: Resource,
    *,
    requirement,
    using: str,
    # A DELETE is no write the workflow check covers: scrud_workflow refuses.
    covered: bool = False,
    type_iri: str | None = None,
    check: Callable | None = None,
) -> None:
    """Delete `instance` (read with its row locked inside the caller's
    transaction on `using`) with ``ResourceRegistration.delete_locked``,
    once a ``WorkflowPolicy`` is allowed in every context it applies to
    (`check_every_context_of_policy`, given `requirement`, `covered`,
    `type_iri` and `check`). One that a resource type uses (its schema,
    context or indexing policy) is kept: `ResourceInUseError` (409). No
    workflow check runs (follow-up ``delete-no-workflow-check``).

    It runs no object check: a caller loads `instance` through
    `scrud_django.core.resources.load_for_read` with ``method="DELETE"``
    first (DRF's ``get_object()``), which asks it.

    Without a `check`, a `requirement` that asks no grants (empty, or hooks
    only) is ``ImproperlyConfigured`` (`require_grants`), before anything is
    deleted, as in `save_checked`: its every-context check of a
    ``WorkflowPolicy`` would allow anyone. (DRF's ``destroy`` passes the
    view's own check.)"""
    if check is None:
        require_grants(requirement, "destroy")
    check_every_context_of_policy(
        request,
        instance,
        requirement,
        covered=covered,
        type_iri=type_iri,
        check=check,
    )
    try:
        ResourceRegistration.delete_locked(
            instance, resource_type=resource_type, using=using
        )
    except ProtectedError as exc:
        raise ResourceInUseError(instance, exc.protected_objects) from exc


@contextmanager
def write_transaction(using: str):
    """A write's transaction on database `using`, yielding a ``rollback()``
    that marks it to roll back when it ends rather than commit."""
    with transaction.atomic(using=using):
        yield lambda: transaction.set_rollback(True, using=using)


def conditional_create_lock(resource_type, using: str):
    """A conditional create's lock: `resource_type`'s row (`type_locked`), so
    that two conditional creates of the type run one at a time and the
    second is evaluated against the list the first left."""
    return type_locked(resource_type, using)
