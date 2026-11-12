"""What a scrud front end requires of an object as far as RBAC is concerned,
without a view: a **requirement**, a list of entries every one of which must
allow the object, each a `Leaf` (a view-free ``IsAuthorized``) or an
``scoped_rbac.access.AnyOf`` of alternatives (DRF's ``|``), each option a
list of entries that must all allow it, as scoped-rbac's
``AccessControlledAPIView.rbac_requirement`` builds one.

`GrantsLeaf` answers as scoped-rbac's ``IsAuthorized`` answers on a scrud
resource view. The DRF views' own permissions are leaves too
(`scrud_django.drf_requirements.ViewLeaf`). The functions here are those
`scrud_django.filters` asks of a view, taking a requirement instead.

"As a GET of the detail route" is the caller's: it passes a GET copy of its
request (`as_get`) where today's view-based code asked a GET of the detail
route (`readable_by_grants`, `readable_by_object_checks`).
"""

import copy
from collections.abc import Mapping
from typing import Protocol

from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db.models import Max, Q
from django.http import Http404
from scoped_rbac import access
from scoped_rbac.conf import conditional_list_max_rows
from scoped_rbac.policy import Permission
from scoped_rbac.rbac_contexts import SOME_CONTEXT

from scrud_django.core.authz import (
    _every_context,
    condition_resource,
    may_act_in_context,
    resource_contexts,
)
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import ResourceRemoval

# What a leaf's object check may raise to refuse one object (Django's
# idioms): a list leaves that object out, as its detail route refuses it.
REFUSALS = (Http404, PermissionDenied)

# Rows fetched per chunk while candidates are evaluated one by one, as
# scoped-rbac's ``access._CANDIDATE_CHUNK_SIZE``.
CANDIDATE_CHUNK_SIZE = 2000


class Leaf(Protocol):
    """A view-free ``IsAuthorized``: what one permission of a requirement
    answers about a request, an object and a context.

    - ``type_iri``: the item type it authorizes;
    - ``reads_each_context``: whether its own ``has_context_permission``
      asks the grants reading each context as the object is there; when
      not, a caller asks the grants beside it. Only a leaf of a trusted
      class (`register_grant_asking_leaf`) is taken at its word: beside any
      other (but a type's hooks, `is_hook`) the grants are asked anyway;
    - ``overrides_object_checks``: whether its object checks may refuse what
      the grants allow (a list then asks them object by object);
    - ``contexts_for(obj)``: the contexts it reads `obj` (a resource's
      content) in;
    - ``has_permission(request, body)``: the request check, `body` the parsed
      body (``None`` for none);
    - ``has_object_permission(request, obj)``: the object check, which may
      also refuse by raising one of `REFUSALS`;
    - ``has_context_permission(request, obj, context)``: the check in one
      context."""

    type_iri: str
    reads_each_context: bool
    overrides_object_checks: bool

    def contexts_for(self, obj: Mapping) -> list[str]: ...

    def has_permission(self, request, body: Mapping | None) -> bool: ...

    def has_object_permission(self, request, obj: Mapping) -> bool: ...

    def has_context_permission(self, request, obj: Mapping, context: str) -> bool: ...


Requirement = list[Leaf | access.AnyOf]


def as_get(request):
    """A shallow copy of `request` (a Django ``HttpRequest``) whose method is
    GET: the request as a GET of the detail route has it, whatever its own
    method. It shares the user, the query and the cached policy."""
    read = copy.copy(request)
    read.method = "GET"
    return read


# The leaf classes trusted to ask the grants (`register_grant_asking_leaf`).
_GRANT_ASKING_LEAVES: tuple[type, ...] = ()


def register_grant_asking_leaf(cls: type) -> type:
    """Trust the leaves of class `cls` (its subclasses included) to ask the
    grants: `asks_grants` counts one as asking them, and, when it says it
    reads each context as the object is there (``reads_each_context``),
    none are asked beside it (`asks_grants_itself`). Returns `cls`, so it
    is usable as a class decorator. Register only a class whose
    ``has_context_permission`` asks the grants (`GrantsLeaf`, the DRF
    adapter's ``ViewLeaf``): a leaf of any other class, whatever it says,
    counts as asking none and gets the grants asked beside it."""
    global _GRANT_ASKING_LEAVES
    if cls not in _GRANT_ASKING_LEAVES:
        _GRANT_ASKING_LEAVES = (*_GRANT_ASKING_LEAVES, cls)
    return cls


def asks_grants_itself(leaf) -> bool:
    """Whether `leaf` asks the grants itself, reading each context as the
    object is there: it is of a registered class
    (`register_grant_asking_leaf`) and says so (``reads_each_context``)."""
    return isinstance(leaf, _GRANT_ASKING_LEAVES) and leaf.reads_each_context


def is_hook(leaf) -> bool:
    """Whether `leaf` is a type's hooks (`scrud_django.core.types.HookLeaf`),
    which only refuse: no grants are asked for it."""
    from scrud_django.core.types import HookLeaf

    return isinstance(leaf, HookLeaf)


@register_grant_asking_leaf
class GrantsLeaf:
    """scoped-rbac's default ``IsAuthorized`` answers for `type_iri`, as a
    scrud resource view gives them: the request's action
    (``access.action_iri_for(request.method)``) on `type_iri`, a resource's
    contexts read from its content (`resource_contexts`), and each context's
    conditions reading the content as it is there (`condition_resource`).
    Its object checks only ask the grants. A request check asks for
    `type_iri` whatever the request: a list's check passes the list type."""

    overrides_object_checks = False
    reads_each_context = True

    def __init__(self, type_iri: str):
        self.type_iri = type_iri

    def __repr__(self):
        return f"GrantsLeaf({self.type_iri!r})"

    def _permission(self, request) -> Permission:
        return Permission(access.action_iri_for(request.method), self.type_iri)

    def contexts_for(self, obj):
        return resource_contexts(obj)

    def has_permission(self, request, body):
        """``IsAuthorized.has_permission`` on a scrud resource view: OPTIONS
        needs no grant; a PUT or POST (not scrudful's copy of an OPTIONS,
        ``access.checks_allow``) is authorized in the contexts `body` writes
        (`rbac_contexts_of`, the default context ``""`` for none), with
        `body` as the resource; any other request in ``SOME_CONTEXT``. A
        refusal is ``False``; another access error (a reserved context, 400)
        is raised. A method the route does not serve is its caller's 405."""
        if request.method == "OPTIONS":
            return True
        if request.method in ("PUT", "POST") and not access.checks_allow(request):
            content = access.contexts_body(body)
            rbac_contexts, resource, written = access.request_contexts(
                request.method,
                content,
                access.call_contexts_from(
                    rbac_contexts_of, content, where=f"{self!r}.contexts_for"
                ),
                put_keeps_context=False,
            )
        else:
            resource = None
            rbac_contexts = [SOME_CONTEXT]
            written = False
        try:
            access.authorize_request(
                request,
                self._permission(request),
                rbac_contexts,
                resource,
                list_request=False,
                written=written,
            )
        except access.AccessDenied:
            return False
        return True

    def has_object_permission(self, request, obj):
        contexts = access.usable_contexts(self.contexts_for(obj))
        if not contexts:
            # Granted by no role: only superusers may act on it.
            return access.may_act_on(request, self._permission(request), obj, [])
        return any(
            self.has_context_permission(request, obj, context) for context in contexts
        )

    def has_context_permission(self, request, obj, context):
        resource = condition_resource(obj, context, every=_every_context.get())
        return access.may_act_in(request, self._permission(request), context, resource)


def _entries(option):
    """An ``AnyOf`` option as the entries it requires."""
    return option if isinstance(option, (list, tuple)) else [option]


def requirement_leaves(requirement):
    """Every `Leaf` of `requirement`, ``AnyOf`` options included."""
    for entry in requirement:
        if isinstance(entry, access.AnyOf):
            for option in entry.options:
                yield from requirement_leaves(_entries(option))
        else:
            yield entry


def asks_grants(requirement) -> bool:
    """Whether `requirement` asks the grants of every request it allows:
    some entry of it does. A leaf asks them only when its class is trusted
    to (`register_grant_asking_leaf`: `GrantsLeaf`, the DRF ``ViewLeaf``);
    a type's hooks (`HookLeaf`) only refuse, and a leaf of any other class
    counts as asking none, whatever it says. An ``AnyOf`` asks them only
    when it has options and **every** option does (an option of hooks alone
    would allow what the grants refuse, and an ``AnyOf`` of none has no leaf
    to check a write after its save)."""

    def asks(entry):
        if isinstance(entry, access.AnyOf):
            return bool(entry.options) and all(
                any(map(asks, _entries(option))) for option in entry.options
            )
        return isinstance(entry, _GRANT_ASKING_LEAVES)

    return any(map(asks, requirement))


def require_grants(requirement, caller: str) -> None:
    """Raise ``ImproperlyConfigured`` unless `requirement` asks the grants
    (`asks_grants`): an empty requirement, one of hooks only, or one whose
    ``AnyOf`` has an option of hooks only, would allow every requester, so
    `caller` (a public entry point of the core) refuses it rather than fail
    open. Give it a `GrantsLeaf` of the type
    (`scrud_django.core.types.requirement_for`)."""
    if not asks_grants(requirement):
        raise ImproperlyConfigured(
            f"{caller}: the requirement {requirement!r} asks no grants, so it "
            "would allow every requester; give it a GrantsLeaf of the type "
            "(scrud_django.core.types.requirement_for)."
        )


def object_check_requirement(requirement):
    """`requirement` when a leaf of it overrides its object checks
    (``overrides_object_checks``: they may refuse what the grants allow,
    which the SQL filter cannot see); else ``None``, and a list is decided by
    the grants alone."""
    if any(leaf.overrides_object_checks for leaf in requirement_leaves(requirement)):
        return requirement
    return None


def meets_object_checks(requirement, request, obj):
    """Whether every entry of `requirement` (any option of an ``AnyOf``
    suffices) allows ``has_object_permission(request, obj)``; one that raises
    a refusal (`REFUSALS`) refuses `obj` only."""

    def meets(entry):
        if isinstance(entry, access.AnyOf):
            return any(
                all(meets(e) for e in _entries(option)) for option in entry.options
            )
        try:
            return entry.has_object_permission(request, obj)
        except REFUSALS:
            # Raised for this object, the detail route refuses it alone.
            return False

    return all(meets(entry) for entry in requirement)


def _grants_meet(requirement, request, obj, permission_of):
    def reads(leaf):
        return may_act_in_context(
            request, permission_of(leaf), obj, leaf.contexts_for(obj)
        )

    def meets(entry):
        if isinstance(entry, access.AnyOf):
            return any(all(map(meets, _entries(option))) for option in entry.options)
        if is_hook(entry):
            # A leaf that only refuses (a type's hooks): no grants to ask.
            return True
        return reads(entry)

    return all(map(meets, requirement))


def grants_allow(requirement, request, obj, permission):
    """Whether the grants allow `permission` on `obj` (the object as its
    object check gets it) in the contexts each leaf of `requirement` reads
    (``contexts_for``; ``AnyOf`` alternatives included), each context read
    with the object as it is there (`may_act_in_context`): scoped-rbac's
    ``AccessControlledAPIView.is_readable`` for `permission`. An empty
    requirement allows."""
    return _grants_meet(requirement, request, obj, lambda leaf: permission)


def readable_by_grants(requirement, request, obj):
    """`grants_allow` for ``http.GET`` on each leaf's ``type_iri``: which
    tells a 403 from a 404. `request` is a GET of the detail route
    (`as_get`)."""
    return _grants_meet(
        requirement, request, obj, lambda leaf: Permission("http.GET", leaf.type_iri)
    )


def readable_by_object_checks(requirement, request, obj):
    """Whether the object checks of `requirement` allow a read of `obj`, as
    a list asks them (`filter_by_object_checks`): `request` is a GET of the
    detail route (`as_get`). True when no leaf overrides its object checks
    (`object_check_requirement`: the grants decide alone). They can only
    refuse: the grants are asked apart (`readable_by_grants`)."""
    narrowing = object_check_requirement(requirement)
    if narrowing is None:
        return True
    return meets_object_checks(narrowing, request, obj)


def filter_by_object_checks(queryset, request, requirement, object_for_checks):
    """`queryset` (a list, already RBAC-filtered) restricted to the objects
    `requirement` allows (`meets_object_checks`, given
    ``object_for_checks(obj)``), as the detail route asks it, with
    `request`'s method (GET or HEAD). An object stored in a context reserved
    once normalised is left out (its detail route is a 404). Every object
    examined counts against ``SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]``:
    past it, ``access.ConditionalListTooLarge``."""
    max_rows = conditional_list_max_rows()
    allowed_pks = []
    bounded = queryset.order_by()[: max_rows + 1]
    for count, obj in enumerate(
        bounded.iterator(chunk_size=CANDIDATE_CHUNK_SIZE), start=1
    ):
        if count > max_rows:
            raise access.ConditionalListTooLarge()
        if access.stored_in_reserved_context(obj):
            continue
        if meets_object_checks(requirement, request, object_for_checks(obj)):
            allowed_pks.append(obj.pk)
    return queryset.filter(pk__in=allowed_pks)


def _plainly_reserved():
    """Contexts reserved in their plain form (``__``-prefixed, or ``*``), the
    form SQL can match, as ``scoped_rbac.access.exclude_reserved`` matches
    them. A context reserved only once normalised (e.g. ``"\\u200b__x"``) is
    matched by `access.is_reserved_context` in Python only; the API refuses to
    store one."""
    return Q(rbac_context__startswith="__") | Q(rbac_context="*")


def _usable(contexts):
    """`contexts` (a policy's, from ``list_scope``) without reserved ones: an
    object's reserved context grants nothing (``access.usable_contexts``)."""
    return {c for c in contexts if not access.is_reserved_context(c)}


def readable_removal(request, requirement, resource_type, using=None, *, type_iri=None):
    """The newest `ResourceRemoval` stamp of `resource_type` a list's
    ``Last-Modified`` takes for `request` (``None`` for none): of the
    contexts in which the requester may read **every** resource (``http.GET``
    on `type_iri`, by default the type's ``type_uri``), so that a stamp only
    ever tells them that a resource they could list left: every context for
    a superuser, every context not plainly reserved for a grant in every
    context, else the contexts that grant it outright. A context that grants
    it only by a condition is left out: the resource that left may be one the
    condition refuses. With a leaf of `requirement` that overrides its object
    checks (`object_check_requirement`: it may refuse the resource that
    left) there is none. Every leaf must read a resource's contexts as
    stamped (its own); a caller whose leaves read others has none. Read on
    database `using` (the list's)."""
    if object_check_requirement(requirement) is not None:
        return None
    stamps = ResourceRemoval.objects.filter(resource_type=resource_type)
    if using is not None:
        stamps = stamps.using(using)
    user = access.request_user(request)
    if user is None or not user.is_superuser:
        if type_iri is None:
            type_iri = resource_type.type_uri
        permission = Permission("http.GET", type_iri)
        scope = access.policy_for(request).list_scope(
            permission, exclude=(SOME_CONTEXT,)
        )
        if scope.unrestricted:
            stamps = stamps.exclude(_plainly_reserved())
        else:
            stamps = stamps.filter(rbac_context__in=_usable(scope.outright))
    return stamps.aggregate(newest=Max("removed_at"))["newest"]
