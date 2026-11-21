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

They run the type's write hooks (`scrud_django.core.types.TypeOptions`):
`create` and `update` its ``prepare_write`` on the body (`prepared_body`),
`save_checked` its ``around_save`` around the save and its
``after_write`` once the saved resource passed its check, in the save's
transaction, `destroy` its ``before_destroy`` and ``after_destroy``. The
hooks of one write share `hook_state`, which the core clears before the
``prepare_write`` and once the save is over. A front end answers a
`create` or `update` with `answered_content`. A write by a method the
type does not serve (``TypeOptions.methods``) is refused
(`MethodNotAllowedError`, 405) once the request check passed (the API
front ends answer it before any authorization: follow-up
``core-unserved-order``).

The whole sequence of a route's write, in the DRF route's order, is
written once here, for every front end but DRF's viewsets (which keep
their own): `create_answer` and `item_write_answer` (the body and request
checks, `check_write_request`, unless the front end asked them already;
the preconditions, under the conditional-create lock for a create that
needs the list's validators; the write and its answer), and `write_item`
(an item's write and answer, once the front end's route looked it up and
evaluated its preconditions). Each answers a `WriteAnswer` the front end
only renders.
"""

import copy
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime

import fastjsonschema
import reversion
from django.db import transaction
from django.db.models import ProtectedError
from scoped_rbac import access
from scoped_rbac.policy import Permission
from scrudful._django import precondition_headers

from scrud_django.core import lists
from scrud_django.core.authz import (
    _consume_workflow_mark,
    _workflow_decision,
    content_of,
    may_act_in_context,
    request_actor,
)
from scrud_django.core.bodies import check_body
from scrud_django.core.decisions import WorkflowDecision, applying
from scrud_django.core.errors import precondition_answer
from scrud_django.core.requirements import (
    Requirement,
    asks_grants_itself,
    is_hook,
    requirement_leaves,
)
from scrud_django.core.signals import acting_as
from scrud_django.core.types import (
    BODY_METHODS,
    HookLeaf,
    TypeOptions,
    ensure_hooks_loaded,
    type_options,
)
from scrud_django.core.urls import absolute_reverse
from scrud_django.core.workflow import (
    Create,
    Update,
    authorizing_write,
    check_policy_is_unique,
    prior_of,
    validate_policy_write,
    workflow_check,
)
from scrud_django.exceptions import (
    JsonSchemaInvalidError,
    JsonValidationError,
    MethodNotAllowedError,
    RequestBodyNotAnObjectError,
    ResourceInUseError,
)
from scrud_django.models import Resource, ResourceType
from scrud_django.registration import ResourceRegistration, type_locked
from scrud_django.workflow import (
    WORKFLOW_POLICY_TYPE_URI,
    lock_workflow_policy_type,
)

# The logger keeps its historical name: operators' log filters match it.
logger = logging.getLogger("scrud_django.validators")


class ValidatorEntry:
    def __init__(self, resource_type: ResourceType, validate: Callable):
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


def write_hooks(resource_type) -> TypeOptions:
    """The hooks registered for `resource_type` (by its ``type_uri``), once
    they are loaded (`ensure_hooks_loaded`)."""
    ensure_hooks_loaded()
    return type_options(resource_type.type_uri)


# The request attribute holding the state the write hooks of one write
# share (`hook_state`).
HOOK_STATE_ATTR = "_scrud_write_hook_state"
# The attribute of a saved resource holding what its type's ``after_write``
# answered (`written_answer`).
ANSWER_ATTR = "_scrud_written_answer"


def hook_state(request) -> dict:
    """The state the write hooks of the write `request` makes share (a
    ``dict``, keyed as the hooks choose): the core clears it before the
    type's ``prepare_write`` (`prepared_body`) and once the save is over
    (`save_checked`, however it ends), so one ``prepare_write`` covers at
    most one save; a save with no ``prepare_write`` before it finds it
    empty. Its hooks must be given the same `request` object."""
    state = getattr(request, HOOK_STATE_ATTR, None)
    if not isinstance(state, dict):
        state = {}
        setattr(request, HOOK_STATE_ATTR, state)
    return state


def _clear_hook_state(request) -> None:
    setattr(request, HOOK_STATE_ATTR, None)


def prepared_body(
    request,
    resource_type,
    body,
    *,
    instance: Resource | None,
    using: str,
    prepare_write: Callable | None,
):
    """`body` as `prepare_write` (the type's ``prepare_write`` hook, or
    ``None``: `body` itself) returns it for the write `request` makes over
    `instance` (``None`` for a create) on `using`, the write's
    `hook_state` cleared first. `create` and `update` give it a copy of the
    body; the DRF views give it ``request.data`` itself."""
    _clear_hook_state(request)
    if prepare_write is None:
        return body
    return prepare_write(request, resource_type, body, instance=instance, using=using)


def refuse_unserved(resource_type, method: str) -> None:
    """Refuse (`MethodNotAllowedError`, 405) a write by `method` when the
    type's ``TypeOptions.methods`` (`write_hooks`) are set and do not name
    it. The core asks it after the request check, while DRF's view, and the
    ninja routes, answer such a method 405 before any authorization
    (scoped-rbac's request check leaves it to the 405): follow-up
    ``core-unserved-order``."""
    methods = write_hooks(resource_type).methods
    if methods is not None and method not in methods:
        raise MethodNotAllowedError(method)


def written_answer(saved) -> Mapping | None:
    """What the type's ``after_write`` answered for the write that saved
    `saved` (`save_checked`), or ``None``."""
    return getattr(saved, ANSWER_ATTR, None)


def validate_write(
    request,
    resource_type,
    body: Mapping,
    *,
    stored: Resource | None,
    requirement: Requirement | Sequence,
    narrow: Sequence[Callable] | None = None,
    readable: Callable[[], bool] | None,
    type_iri: str | None = None,
    covered: bool | None = None,
    prepare: Callable[[object], object] | None = None,
) -> WorkflowDecision:
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
    cannot read. For an update of a ``WorkflowPolicy``, then its
    every-context check (`check_every_context_of_policy`: the write allowed
    in every context the stored policy applies to, ``access.AccessDenied``
    otherwise), as DRF's ``update`` asks it after ``get_object()``, with
    `covered` (by default, as the object check, whether the request is the
    PUT the workflow check covers). Right after the request check, a write
    by a method the type does not serve (``POST`` for a create, ``PUT`` for
    an update) is refused (`refuse_unserved`, 405); DRF's view and the
    ninja routes answer it before any authorization (follow-up
    ``core-unserved-order``). Then `prepare`, when given, is called
    with the body, and the body it returns is the one validated from there
    on (`create` and `update` give the type's ``prepare_write``, as the DRF
    views run it after the request and object checks, before the
    serializer). Then a body that is not a JSON object
    (a list, a string, a number) is refused (`RequestBodyNotAnObjectError`,
    400), as DRF's serializer refuses it. Then, in the order DRF's serializer validators
    check it:
    its JSON Schema (`validate_schema`), then, for a ``WorkflowPolicy``, the
    policy's own checks (`validate_policy_write`), then the workflow check
    (`scrud_django.core.workflow.workflow_check`, given `stored`'s content
    as its ``prior``, `prior_of`, and `narrow` and `readable` as they are).
    Returns the workflow check's `WorkflowDecision` (the transition it
    authorized in each context with a policy); the request then carries
    its mark for this one write (``(type_iri, stored.pk)``, ``None`` as the
    pk for a create), which the save that uses it consumes (`save_checked`).
    `type_iri` is the type the write's post-save check names (by default
    `resource_type`'s ``type_uri``): the save must be given the same one,
    or its check does not find the mark.

    `requirement` (required: the type's, `scrud_django.core.types
    .requirement_for`) gives the `narrow`: the checks of each type's hooks
    in it (each `HookLeaf`'s ``narrow``), so that a registered check can
    refuse a transition, as on DRF. A given `narrow` replaces them.

    `readable` says whether the requester may read `stored` (whether its
    workflow errors are hidden): required for an update, where ``None``
    raises ``TypeError`` (once any earlier mark is removed); ignored for a
    create.

    `requirement` is taken through
    `scrud_django.core.requirements.Requirement.of` (once any earlier mark
    is removed): one that asks no grants (empty, or hooks only) is
    ``ImproperlyConfigured``.

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
    if stored is not None and readable is None:
        raise TypeError(
            "validate_write of an update needs readable: whether the requester "
            "may read the stored resource."
        )
    requirement = Requirement.of(requirement, caller="validate_write")
    check_request(
        request, resource_type, requirement, list_route=stored is None, body=body
    )
    refuse_unserved(resource_type, "POST" if stored is None else "PUT")
    if stored is not None:
        # DRF's get_object() asks the object check of the stored resource:
        # a caller that looked it up itself must not skip it. With the body,
        # as DRF's | pairs an option's request check (with the body) with
        # its object check, which a body-less load_for_read could not.
        covered_item = request.method == COVERED_ITEM_METHOD
        check_object(
            request,
            resource_type,
            stored,
            requirement,
            covered=covered_item,
            body=body,
        )
        # DRF's update asks it after get_object(), before the serializer.
        check_every_context_of_policy(
            request,
            stored,
            requirement,
            covered=covered_item if covered is None else covered,
            type_iri=type_iri,
        )
    if prepare is not None:
        body = prepare(body)
    # DRF's serializer refuses a body that is not a JSON object (400) once
    # the request and object checks have passed, before it validates.
    if not isinstance(body, Mapping):
        raise RequestBodyNotAnObjectError()
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
    write: Create | Update
    if stored is None:
        write = Create(proposed=body)
    else:
        assert readable is not None  # checked above, before the lookups
        write = Update(
            prior=prior_of(stored),
            proposed=body,
            readable=readable,
            stored_pk=stored.pk,
        )
    return workflow_check(
        request,
        type_iri=resource_type.type_uri,
        write=write,
        narrow=narrow,
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
    requirement: Requirement | Sequence | None,
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
    `recheck` names another type passes it as `type_iri`. The decision
    the mark carries (`_workflow_decision`) is pending while `save` runs
    (`scrud_django.core.decisions.applying`), so that the
    ``Resource.save()`` it matches sends it instead of computing its own
    (not the post-save check, nor ``after_write``). The mark is
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
    `serialized_policy_save`). The type's ``around_save`` hook
    (`write_hooks`), if any, wraps `save`, inside the transaction. Its
    ``after_write``, if any, runs in the same transaction once the saved
    resource passed its check (and the workflow check's mark is consumed),
    given the resource the save wrote (``created``: `instance` is
    ``None``), which it may change; the resource returned is then read
    again. A mapping it returns is kept as the write's answer, on both
    (`written_answer`, `answered_content`); an answer an earlier write left
    on either is dropped first, so that a caller writing the same object
    twice never reads the first write's. The write's `hook_state` is
    cleared once the save is over, however it ends.

    The check refreshes the saved resource from its row, which empties its
    cache of related objects: `resource_type` is cached on it again, and on
    what is returned (`cache_resource_type`), so that what reads it later
    (a ``Location``, and what runs on commit with it: search indexing, the
    ``SCRUD_WORKFLOW_ACTIONS`` `ScrudSignalProcessor` defers) does not load
    it through the read router. The check and this re-caching run in one
    transaction on `using`, so that a write outside a transaction of the
    caller's commits only once the type is cached again.

    Without a `recheck`, `requirement` is taken through `Requirement.of`:
    one that asks no grants (empty, or hooks only) is
    ``ImproperlyConfigured``, before anything is saved: its post-save check would let any write land
    anywhere. (`check_post_save` itself takes one: a DRF view with no
    ``IsAuthorized`` checks nothing after the save.) `create` and `update`
    give no `recheck`.

    Raises ``access.AccessError``s for the caller to answer."""
    try:
        if recheck is None:
            requirement = Requirement.of(requirement, caller="save_checked")
        if type_iri is None:
            type_iri = resource_type.type_uri
        stored_pk = instance.pk if instance is not None else None
        # The workflow check's decision, read before anything consumes the
        # mark it rides with: the save it matches sends it.
        decision = _workflow_decision(request)
        saved = []

        def recorded_save():
            result = save()
            saved.append(instance if result is None else result)
            return result

        around_save = write_hooks(resource_type).around_save

        def run():
            with applying(decision):
                if around_save is None:
                    return recorded_save()
                return around_save(
                    request,
                    resource_type,
                    recorded_save,
                    instance=instance,
                    using=using,
                )

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
        if resource_type.type_uri == WORKFLOW_POLICY_TYPE_URI:
            run = (serialize or serialized_policy_save)(run, using)
        after_write = write_hooks(resource_type).after_write
        # The request's user acts for every save of the write: the one
        # checked, and those its post-save receivers and after_write make.
        with acting_as(request_actor(request)), transaction.atomic(using=using):
            if instance is None:
                result = check.create(run, db=using)
            else:
                result = check.update(instance, run, db=using)
            cache_resource_type(resource_type, *saved, result)
            # The object the save wrote: a create's is the one its caller
            # holds (DRF's serializer.instance, whose validators a 201
            # sends); the result is read again once the hook ran.
            written = saved[-1] if saved and isinstance(saved[-1], Resource) else result
            # This write's answer only: not one an earlier write of the same
            # object left on it.
            for obj in (written, result):
                obj.__dict__.pop(ANSWER_ATTR, None)
            if after_write is not None:
                # What follows the save authorizes nothing by the mark.
                _consume_workflow_mark(request)
                answered = after_write(
                    request,
                    resource_type,
                    written,
                    created=instance is None,
                    using=using,
                )
                if written is not result:
                    result.refresh_from_db(using=using)
                    cache_resource_type(resource_type, result)
                if answered is not None:
                    setattr(written, ANSWER_ATTR, answered)
                    setattr(result, ANSWER_ATTR, answered)
        return result
    finally:
        # One validation authorizes at most one save, however it ends; one
        # prepare_write covers at most one save.
        _consume_workflow_mark(request)
        _clear_hook_state(request)


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
    using: str,
    covered: bool | None = None,
):
    """Validate `request`'s write of `body` over `stored` (``None`` for a
    create) with `validate_write` (given `covered`), whether `stored` is
    readable (`object_readable`) telling whether its errors are hidden:
    what `create` and `update` run on the body they save, always. The
    type's ``prepare_write`` (`write_hooks`), if any, is given a copy of
    `body` (a shallow one, of a mapping) as `validate_write`'s `prepare`
    (`prepared_body`, which clears the write's `hook_state` first).
    Returns the body to save: the one validated."""
    # Imported here: scrud_django.core.resources imports this module.
    from scrud_django.core.resources import object_readable

    readable = None
    if stored is not None:

        def readable():
            return object_readable(request, resource_type, stored, requirement)

    validated = [body]
    prepare_write = write_hooks(resource_type).prepare_write

    def prepare(sent):
        if prepare_write is not None and isinstance(sent, Mapping):
            sent = dict(sent)
        validated[0] = prepared_body(
            request,
            resource_type,
            sent,
            instance=stored,
            using=using,
            prepare_write=prepare_write,
        )
        return validated[0]

    validate_write(
        request,
        resource_type,
        body,
        stored=stored,
        readable=readable,
        requirement=requirement,
        type_iri=type_iri,
        covered=covered,
        prepare=prepare,
    )
    return validated[0]


def create(
    request,
    resource_type,
    body,
    *,
    requirement: Requirement | Sequence,
    using: str,
    covered: bool = True,
    type_iri: str | None = None,
) -> Resource:
    """Create a resource of `resource_type` whose content is `body` with
    ``ResourceRegistration.register_instance`` (in `creating_revision`),
    checked as stored (`save_checked`), once `body` itself is validated
    (`_validated`: `validate_write`, always; the body saved is the one the
    type's ``prepare_write`` returned). `type_iri` is the type the
    post-save check names (by default `resource_type`'s ``type_uri``). The
    caller answers with `answered_content`, in the same transaction.

    It takes no ``recheck``: its post-save check is `requirement`'s
    (`check_post_save`). A front end needing a post-save check of its own
    saves with `save_checked` itself (follow-up ``core-write-no-recheck``).

    `requirement` is taken through `Requirement.of` (once any earlier mark
    is removed, as `validate_write` removes it)."""
    _consume_workflow_mark(request)
    _clear_hook_state(request)
    requirement = Requirement.of(requirement, caller="create")
    # One type IRI for the workflow check's mark and the post-save check.
    if type_iri is None:
        type_iri = resource_type.type_uri
    body = _validated(
        request,
        resource_type,
        body,
        stored=None,
        requirement=requirement,
        type_iri=type_iri,
        using=using,
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
    requirement: Requirement | Sequence,
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
    `scrud_django.core.resources.load_for_read`. The body saved is the one
    the type's ``prepare_write`` returned. `type_iri` is the type the
    post-save check names (by default `resource_type`'s ``type_uri``). The
    caller answers with `answered_content`, in the same transaction.

    It takes no ``recheck``: its post-save check is `requirement`'s
    (`check_post_save`). A front end needing a post-save check of its own
    saves with `save_checked` itself (follow-up ``core-write-no-recheck``).

    The validation asks a ``WorkflowPolicy``'s every-context check
    (`check_every_context_of_policy`, with `covered`) once the object check
    passed and before the body is validated, as DRF's ``update`` asks it
    after ``get_object()``.

    `requirement` is taken through `Requirement.of` (once any earlier mark
    is removed, as `validate_write` removes it). A ``None`` `instance` is a
    ``TypeError``, before anything is validated."""
    if instance is None:
        raise TypeError("update needs the stored instance to write to, not None")
    _consume_workflow_mark(request)
    _clear_hook_state(request)
    requirement = Requirement.of(requirement, caller="update")
    # One type IRI for the workflow check's mark and the post-save check.
    if type_iri is None:
        type_iri = resource_type.type_uri
    body = _validated(
        request,
        resource_type,
        body,
        stored=instance,
        requirement=requirement,
        type_iri=type_iri,
        using=using,
        covered=covered,
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
    requirement: Requirement | Sequence | None,
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

    A type that does not serve DELETE (``TypeOptions.methods``) refuses it
    (`refuse_unserved`, 405) first: its caller asked the request check
    already (`load_for_read`). Then the type's ``before_destroy``
    (`write_hooks`), if any, runs (a refusal raises, nothing deleted), and
    its ``after_destroy`` once `instance` is deleted.

    Without a `check`, `requirement` is taken through `Requirement.of`: one
    that asks no grants (empty, or hooks only) is ``ImproperlyConfigured``,
    before anything is deleted, as in `save_checked`: its every-context check of a
    ``WorkflowPolicy`` would allow anyone. (DRF's ``destroy`` passes the
    view's own check.)"""
    if check is None:
        requirement = Requirement.of(requirement, caller="destroy")
    refuse_unserved(resource_type, "DELETE")
    hooks = write_hooks(resource_type)
    if hooks.before_destroy is not None:
        hooks.before_destroy(request, resource_type, instance, using=using)
    check_every_context_of_policy(
        request,
        instance,
        requirement,
        covered=covered,
        type_iri=type_iri,
        check=check,
    )
    # The request's user acts for the delete and for what its receivers and
    # after_destroy save.
    with acting_as(request_actor(request)):
        try:
            ResourceRegistration.delete_locked(
                instance, resource_type=resource_type, using=using
            )
        except ProtectedError as exc:
            raise ResourceInUseError(instance, exc.protected_objects) from exc
        if hooks.after_destroy is not None:
            hooks.after_destroy(request, resource_type, instance, using=using)


def answered_content(
    request, resource_type, saved: Resource, *, created: bool, using: str
) -> Mapping:
    """The content a front end answers a write with, once `create` (`created`
    true) or `update` returned `saved`: what the type's ``after_write``
    answered for it (`written_answer`; it ran in `save_checked`), else the
    saved content (`content_of`). It has no effect: the write is done."""
    answered = written_answer(saved)
    return content_of(saved) if answered is None else answered


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


@dataclass(frozen=True)
class WriteAnswer:
    """What a resource route answers a write with, for a front end to
    render: its `status`, the `content` of its body (``None``: no body), the
    written resource's validators (`etag` unquoted, `last_modified`), a
    create's `location` (the API URL of the created item, absolute), and
    `written`, the resource the write saved (``None`` for a delete). A
    failed precondition is the `problem` ``(status, body, headers)`` of
    `scrud_django.core.errors.precondition_answer` (nothing written; its
    `status` is the answer's)."""

    status: int
    content: Mapping | None = None
    etag: str | None = None
    last_modified: datetime | None = None
    location: str | None = None
    problem: tuple[int, Mapping | list, Mapping[str, str]] | None = None
    written: Resource | None = None


def _failed(problem) -> WriteAnswer:
    return WriteAnswer(problem[0], problem=problem)


def check_write_request(
    request,
    resource_type,
    body,
    *,
    requirement: Requirement | Sequence,
    list_route: bool,
    present: bool = True,
) -> None:
    """The checks a write's route asks first, as DRF's ``initial()`` asks
    them: the body of a POST or PUT (`scrud_django.core.bodies.check_body`,
    `present` saying whether the request carried one: a malformed body is a
    400 before any 403), then the route's request check with it
    (`scrud_django.core.resources.check_request` of the list's POST, for
    `list_route`, else of the item's route; ``access.AccessDenied``). A
    front end with checks of its own after these (a UI page's prefix and
    headers) asks them next, then answers with `create_answer` or
    `item_write_answer` given ``request_checked=True``."""
    # Imported here: scrud_django.core.resources imports this module.
    from scrud_django.core.resources import check_request

    if request.method in BODY_METHODS:
        check_body(body, present=present)
    check_request(request, resource_type, requirement, list_route=list_route, body=body)


def create_answer(
    request,
    resource_type,
    body,
    *,
    requirement: Requirement | Sequence,
    using: str,
    request_checked: bool = False,
    present: bool = True,
    ignored: frozenset[str] | None = None,
    uniform: bool | None = None,
) -> WriteAnswer:
    """The answer of the POST `request` of `body` to `resource_type`'s
    list, as the DRF list route creates, inside the write's transaction on
    `using` (the caller's, `write_transaction`), its steps in the route's
    order:

    1. unless `request_checked` (the caller asked it, `check_write_request`),
       the body and request checks (`check_write_request`, with `present`);
    2. the list's preconditions (`scrud_django.core.lists
       .evaluate_list_preconditions`, against the list's validators read on
       `using` with `ignored` as `create_list_validators` takes it): a
       conditional create, whose preconditions need those validators, first
       takes the type's lock (`conditional_create_lock`), held until the
       caller's transaction ends. A failed one is answered (`WriteAnswer
       .problem`, `precondition_answer` with `uniform`), nothing written;
    3. the create (`create`) and its answer: 201, `answered_content`, the
       created item's validators and its ``Location``.

    A page of the list that is not one of its pages (the POST's query asks
    it) raises ``django.core.paginator.InvalidPage``, for the front end to
    answer as its paginator answers it (404 ``"Invalid page."``). Refusals
    are raised, as `create` raises them."""
    if not request_checked:
        check_write_request(
            request,
            resource_type,
            body,
            requirement=requirement,
            list_route=True,
            present=present,
        )
    headers = precondition_headers(request)
    with ExitStack() as locks:
        # Inside the caller's transaction, which holds it until it ends:
        # conditional creates of the type run one at a time, each evaluated
        # against the list the previous one left.
        if lists.list_validators_needed(headers):
            locks.enter_context(conditional_create_lock(resource_type, using))
        failed = lists.evaluate_list_preconditions(
            request.method,
            headers,
            lambda: lists.create_list_validators(
                request,
                resource_type,
                requirement=requirement,
                ignored=ignored,
                using=using,
            ),
        )
        if failed is not None:
            return _failed(precondition_answer(request, failed, uniform=uniform))
        obj = create(request, resource_type, body, requirement=requirement, using=using)
        content = answered_content(
            request, resource_type, obj, created=True, using=using
        )
    location = absolute_reverse(
        resource_type.route_name_detail(), args=[obj.pk], request=request
    )
    return WriteAnswer(
        201,
        content,
        etag=obj.etag,
        last_modified=obj.modified_at,
        location=location,
        written=obj,
    )


def item_write_answer(
    request,
    resource_type,
    pk,
    method: str,
    body,
    *,
    requirement: Requirement | Sequence,
    using: str,
    request_checked: bool = False,
    present: bool = True,
    loaded: Callable[[Resource], None] | None = None,
) -> WriteAnswer:
    """The answer of the `method` request `request` (``PUT`` of `body`, or
    ``DELETE``) to the item `pk` of `resource_type`, as the DRF detail
    route writes, inside the write's transaction on `using` (the
    caller's, `write_transaction`), its steps in the route's order:

    1. unless `request_checked` (the caller asked it, `check_write_request`),
       the body and request checks (`check_write_request`, with `present`);
    2. the item looked up for `method` with its row locked on `using`, and
       checked (`scrud_django.core.resources.load_for_read`, given `body`,
       ``request_checked``), then given to `loaded` when one is (a UI page
       keeps it to render a refused PUT's form);
    3. its preconditions (`scrud_django.core.resources
       .item_precondition_answer`: 428 without ``If-Match``, 412 for a
       stale one): a failed one is answered (`WriteAnswer.problem`),
       nothing written;
    4. the write and its answer (`write_item`).

    Refusals are raised. `method` must be `request`'s own (a ``ValueError``
    otherwise): `check_write_request` decides from ``request.method``
    whether there is a body to check."""
    # Imported here: scrud_django.core.resources imports this module.
    from scrud_django.core.resources import item_precondition_answer, load_for_read

    if method not in ("PUT", "DELETE"):
        raise ValueError(f"item_write_answer writes with PUT or DELETE, not {method}.")
    if request.method != method:
        raise ValueError(
            f"item_write_answer: a {method} answering a {request.method} "
            "request (its checks follow the request's method)."
        )
    if not request_checked:
        check_write_request(
            request,
            resource_type,
            body,
            requirement=requirement,
            list_route=False,
            present=present,
        )
    obj = load_for_read(
        request,
        resource_type,
        pk,
        requirement=requirement,
        method=method,
        lock_using=using,
        body=body,
        request_checked=True,
    )
    if loaded is not None:
        loaded(obj)
    failed = item_precondition_answer(
        request, etag=obj.etag, last_modified=obj.modified_at, method=method
    )
    if failed is not None:
        return _failed(failed)
    return write_item(
        request, resource_type, obj, method, body, requirement=requirement, using=using
    )


def write_item(
    request,
    resource_type,
    obj: Resource,
    method: str,
    body,
    *,
    requirement: Requirement | Sequence,
    using: str,
) -> WriteAnswer:
    """The `method` write of the item `obj` (looked up for it with its row
    locked, its preconditions passed: `item_write_answer`, or a front end
    whose route evaluated them, as scrudful's item decorator does on
    ninja) and its answer: a ``PUT`` of `body` (`update`) answers 200 with
    `answered_content` and the saved item's validators; a ``DELETE``
    (`destroy`) answers 204. Refusals are raised."""
    if method == "DELETE":
        destroy(request, resource_type, obj, requirement=requirement, using=using)
        return WriteAnswer(204)
    if method != "PUT":
        raise ValueError(f"write_item writes with PUT or DELETE, not {method}.")
    saved = update(
        request, resource_type, obj, body, requirement=requirement, using=using
    )
    content = answered_content(
        request, resource_type, saved, created=False, using=using
    )
    return WriteAnswer(
        200,
        content,
        etag=saved.etag,
        last_modified=saved.modified_at,
        written=saved,
    )
