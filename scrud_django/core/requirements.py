"""What a scrud front end requires of an object as far as RBAC is concerned,
without a view: a **requirement** (`Requirement`, frozen), entries every one
of which must allow the object, each a `Leaf` (a view-free ``IsAuthorized``)
or an `AnyOf` of alternatives (DRF's ``|``), each option a tuple of entries
that must all allow it. `Requirement.of` takes the entries as scoped-rbac's
``AccessControlledAPIView.rbac_requirement`` builds them (its
``access.AnyOf`` converted to the core's `AnyOf`) and refuses one that asks
no grants; the facade's entry points take a requirement through it.

`GrantsLeaf` answers as scoped-rbac's ``IsAuthorized`` answers on a scrud
resource view. The DRF views' own permissions are leaves too
(`scrud_django.drf_requirements.ViewLeaf`). The functions here are those
`scrud_django.filters` asks of a view, taking a requirement instead.

"As a GET of the detail route" is the caller's: it passes a GET copy of its
request (`as_get`) where today's view-based code asked a GET of the detail
route (`readable_by_grants`, `readable_by_object_checks`).
"""

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
    _not_checking_write,
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


def _entries(option):
    """An ``AnyOf`` option as the entries it requires: a list or tuple of
    them as it is, any other entry (a leaf, an ``AnyOf``) alone."""
    return option if isinstance(option, (list, tuple)) else [option]


def _entry(entry):
    """`entry` as a `Requirement` holds it: scoped-rbac's ``access.AnyOf``
    as the core's `AnyOf`; a leaf (or the core's `AnyOf`) as it is."""
    if isinstance(entry, access.AnyOf):
        return AnyOf.of(*entry.options)
    return entry


@dataclass(frozen=True)
class AnyOf:
    """Alternatives (DRF's ``|``), any one of which allowing suffices: each
    option a tuple of entries (leaves, or an `AnyOf`) that must all allow.
    It has at least one option, and every option at least one entry
    (``ValueError``): an ``AnyOf`` of none would allow nothing yet have no
    leaf to check, and an empty option would allow everything. Its options
    are normalised as `of` does, whatever they were given as."""

    options: tuple[tuple["Leaf | AnyOf", ...], ...]

    def __post_init__(self):
        options = tuple(
            tuple(_entry(e) for e in _entries(option)) for option in self.options
        )
        if not options:
            raise ValueError("An AnyOf needs at least one option.")
        if not all(options):
            raise ValueError(f"An AnyOf option requires no entry: {self.options!r}.")
        object.__setattr__(self, "options", options)

    @classmethod
    def of(cls, *options) -> "AnyOf":
        """The `AnyOf` of `options`, each a leaf, a list (or tuple) of
        entries, or an ``AnyOf`` (scoped-rbac's ``access.AnyOf``, as
        ``AccessControlledAPIView.rbac_requirement`` builds one, converted
        to the core's), as a tuple of its entries."""
        return cls(options)


def to_scoped_rbac(any_of: AnyOf) -> access.AnyOf:
    """`any_of` as scoped-rbac's ``access.AnyOf`` (each option a list, a
    nested `AnyOf` converted too), for a scoped-rbac call that needs its
    own type."""

    def entry(e):
        return to_scoped_rbac(e) if isinstance(e, AnyOf) else e

    return access.AnyOf([[entry(e) for e in option] for option in any_of.options])


@dataclass(frozen=True)
class Requirement:
    """What a front end requires of a request and an object: `entries`,
    every one of which must allow it, each a `Leaf` or an `AnyOf`; iterable
    over them. Build one with `of`, which refuses one that asks no grants
    (what the facade's entry points take), or, for a DRF view's own
    requirement only (a view with no ``IsAuthorized`` asks none and is
    still served), `unchecked`. Its entries are normalised as `of` does
    (scoped-rbac's ``access.AnyOf`` as the core's `AnyOf`)."""

    entries: tuple["Leaf | AnyOf", ...]

    def __post_init__(self):
        object.__setattr__(self, "entries", tuple(map(_entry, self.entries)))

    def __iter__(self):
        return iter(self.entries)

    def __len__(self):
        return len(self.entries)

    def __getitem__(self, index):
        """An entry (a slice: a tuple of entries, not a `Requirement`)."""
        return self.entries[index]

    @classmethod
    def unchecked(cls, entries) -> "Requirement":
        """`entries` (a sequence of leaves and ``AnyOf``s, scoped-rbac's
        included) as a `Requirement`, with no grants check; a `Requirement`
        as it is. Only for a DRF view's requirement
        (`scrud_django.drf_requirements.requirement_of`) and the helpers
        the DRF adapters call."""
        if isinstance(entries, Requirement):
            return entries
        return cls(tuple(entries))

    @classmethod
    def of(cls, entries, *, caller: str = "Requirement.of") -> "Requirement":
        """`entries` as a `Requirement` (`unchecked`; a `Requirement` is
        returned as it is), refused (``ImproperlyConfigured``,
        `require_grants`, naming `caller`) unless it asks the grants
        (`asks_grants`): a `Requirement` built `unchecked` is checked
        too."""
        requirement = cls.unchecked(entries)
        require_grants(requirement, caller)
        return requirement


def as_get(request):
    """A shallow copy of `request` (a Django ``HttpRequest``) whose method is
    GET: the request as a GET of the detail route has it, whatever its own
    method. It shares the user, the query and the cached policy."""
    read = copy.copy(request)
    read.method = "GET"
    return read


# The leaf classes trusted to ask the grants (`register_grant_asking_leaf`).
_GRANT_ASKING_LEAVES: tuple[type, ...] = ()

# What a leaf's class decides about the grants: a subclass of a registered
# class that overrides either is not trusted unless registered itself.
_GRANT_ASKING_ATTRIBUTES = ("has_context_permission", "reads_each_context")


def register_grant_asking_leaf(cls: type) -> type:
    """Trust the leaves of class `cls` to ask the grants: `asks_grants`
    counts one as asking them, and, when it says it reads each context as
    the object is there (``reads_each_context``), none are asked beside it
    (`asks_grants_itself`). A subclass of `cls` is trusted too while it
    overrides neither ``has_context_permission`` nor ``reads_each_context``
    (an override, of the class or of the instance, may stop asking the
    grants); one that overrides either is trusted only when registered
    itself. Returns `cls`, so it is usable as a class decorator. Register
    only a class whose ``has_context_permission`` asks the grants
    (`GrantsLeaf`, the DRF adapter's ``ViewLeaf``): a leaf of any other
    class, whatever it says, counts as asking none and gets the grants
    asked beside it (but a type's hooks, `is_hook`, which ask none and
    get none asked)."""
    global _GRANT_ASKING_LEAVES
    if cls not in _GRANT_ASKING_LEAVES:
        _GRANT_ASKING_LEAVES = (*_GRANT_ASKING_LEAVES, cls)
    return cls


def _trusted(leaf) -> bool:
    """Whether `leaf`'s class is trusted to ask the grants
    (`register_grant_asking_leaf`): registered itself, or a subclass of the
    nearest registered class in its MRO overriding neither of
    `_GRANT_ASKING_ATTRIBUTES`, with no instance attribute of either name."""
    instance = getattr(leaf, "__dict__", {})
    if any(name in instance for name in _GRANT_ASKING_ATTRIBUTES):
        return False
    cls = type(leaf)
    for base in cls.__mro__:
        if base in _GRANT_ASKING_LEAVES:
            return all(
                getattr(cls, name, None) is getattr(base, name, None)
                for name in _GRANT_ASKING_ATTRIBUTES
            )
    return False


def _own_request_check(leaf) -> bool:
    """Whether `leaf`, of a trusted class (`_trusted`), has a request check
    of its own: its class's ``has_permission`` is not that of the nearest
    registered class in its MRO, or the instance was given one."""
    if "has_permission" in getattr(leaf, "__dict__", {}):
        return True
    cls = type(leaf)
    for base in cls.__mro__:
        if base in _GRANT_ASKING_LEAVES:
            return getattr(cls, "has_permission", None) is not getattr(
                base, "has_permission", None
            )
    return True


def asks_grants_itself(leaf) -> bool:
    """Whether `leaf` asks the grants itself, reading each context as the
    object is there: its class is trusted to (`register_grant_asking_leaf`)
    and it says so (``reads_each_context``)."""
    return _trusted(leaf) and leaf.reads_each_context


_hook_leaf_class = None


def _hook_class() -> type:
    """`scrud_django.core.types.HookLeaf`, imported on first use:
    `scrud_django.core.types` imports this module."""
    global _hook_leaf_class
    if _hook_leaf_class is None:
        from scrud_django.core.types import HookLeaf

        _hook_leaf_class = HookLeaf
    return _hook_leaf_class


# What decides that a type's hooks only refuse: a subclass of ``HookLeaf``
# (or an instance) overriding one of them is no type's hooks (`is_hook`).
_HOOK_ATTRIBUTES = (
    "contexts_for",
    "has_permission",
    "has_object_permission",
    "has_context_permission",
)


def is_hook(leaf) -> bool:
    """Whether `leaf` is a type's hooks (`scrud_django.core.types.HookLeaf`),
    which only refuse: no grants are asked for it. A subclass of it is too
    while it overrides none of `_HOOK_ATTRIBUTES` (``contexts_for`` and the
    ``has_*`` checks) and the instance was given none of them, as
    `_trusted` has it for a grants leaf: one that does may allow what a
    type's hooks would not, and is a leaf of an unknown class (the grants
    are asked beside it)."""
    hook_class = _hook_class()
    if not isinstance(leaf, hook_class):
        return False
    instance = getattr(leaf, "__dict__", {})
    if any(name in instance for name in _HOOK_ATTRIBUTES):
        return False
    cls = type(leaf)
    return all(
        getattr(cls, name) is getattr(hook_class, name) for name in _HOOK_ATTRIBUTES
    )


# The leaf classes whose ``contexts_for`` reads a resource's stored contexts
# (`register_stored_contexts_leaf`), each with what else an instance must
# meet (``None``: nothing).
_STORED_CONTEXTS_LEAVES: dict[type, Callable | None] = {}


def register_stored_contexts_leaf(cls: type, reads_stored=None) -> type:
    """Count the leaves of class `cls` as reading a resource's stored
    contexts (its ``ResourceContext`` rows, which a list's SQL filter and
    removal stamps read) when ``contexts_for`` is `cls`'s, neither
    overridden by a subclass nor given to the instance, and, with
    `reads_stored` (``(leaf) -> bool``), it allows the leaf too (the DRF
    ``ViewLeaf``'s asks its permission). Returns `cls`, so it is usable as a
    class decorator. Register only a class whose ``contexts_for`` reads
    them (`GrantsLeaf`, the type's hooks' ``HookLeaf``, the DRF
    ``ViewLeaf``): a leaf of any other class counts as reading contexts of
    its own (`reads_stored_contexts`)."""
    _STORED_CONTEXTS_LEAVES[cls] = reads_stored
    return cls


def reads_stored_contexts(leaf) -> bool:
    """Whether `leaf` reads a resource's stored contexts: its class, or the
    nearest registered class in its MRO (`register_stored_contexts_leaf`),
    is registered, its ``contexts_for`` is that class's, the instance was
    given none, and the class's own condition, if any, allows it."""
    if "contexts_for" in getattr(leaf, "__dict__", {}):
        return False
    cls = type(leaf)
    for base in cls.__mro__:
        if base in _STORED_CONTEXTS_LEAVES:
            if getattr(cls, "contexts_for", None) is not getattr(
                base, "contexts_for", None
            ):
                return False
            reads_stored = _STORED_CONTEXTS_LEAVES[base]
            return reads_stored is None or bool(reads_stored(leaf))
    return False


def require_stored_contexts(requirement, caller: str) -> None:
    """Raise ``ImproperlyConfigured`` unless every leaf of `requirement`
    (``AnyOf`` options included) reads a resource's stored contexts
    (`reads_stored_contexts`): `caller` (a list's, which filters by the
    ``ResourceContext`` rows and reads removal stamps of those contexts)
    would otherwise decide by contexts the leaf does not read."""
    custom = [
        leaf
        for leaf in requirement_leaves(requirement)
        if not reads_stored_contexts(leaf)
    ]
    if custom:
        raise ImproperlyConfigured(
            f"{caller}: the leaves {custom!r} may read contexts other than a "
            "resource's stored ones (a contexts_for of their own, or a class "
            "not registered with register_stored_contexts_leaf), which a list "
            "filters by; check each object on its detail route instead."
        )


@register_stored_contexts_leaf
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


def _leaves(entries):
    for entry in entries:
        if isinstance(entry, AnyOf):
            for option in entry.options:
                yield from _leaves(option)
        else:
            yield entry


def requirement_leaves(requirement):
    """Every `Leaf` of `requirement` (a `Requirement`, or its entries,
    `Requirement.unchecked`), `AnyOf` options included."""
    return _leaves(Requirement.unchecked(requirement))


def asks_grants(requirement) -> bool:
    """Whether `requirement` asks the grants of every request it allows:
    some entry of it does. A leaf asks them only when its class is trusted
    to (`register_grant_asking_leaf`: `GrantsLeaf`, the DRF ``ViewLeaf``);
    a type's hooks (`HookLeaf`) only refuse, and a leaf of any other class
    counts as asking none, whatever it says. An ``AnyOf`` asks them only
    when it has options and **every** option does (an option of hooks alone
    would allow what the grants refuse, and an ``AnyOf`` of none has no leaf
    to check a write after its save: `AnyOf` refuses one). `requirement`
    is a `Requirement`, or its entries (`Requirement.unchecked`)."""

    def asks(entry):
        if isinstance(entry, AnyOf):
            return all(any(map(asks, option)) for option in entry.options)
        return _trusted(entry)

    return any(map(asks, Requirement.unchecked(requirement)))


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
    requirement = Requirement.unchecked(requirement)
    if any(leaf.overrides_object_checks for leaf in requirement_leaves(requirement)):
        return requirement
    return None


def option_allows_request(option, request, body=None) -> bool:
    """Whether every entry of the ``AnyOf`` option `option` allows
    ``has_permission(request, body)`` (an ``AnyOf`` among them: any of its
    options), as DRF's ``|`` asks an operand's request check
    (``OR.has_object_permission``). `meets_object_checks` asks it without a
    body: its facade callers (a list, readability) always ask it of a GET or
    HEAD (a GET copy, `as_get`, for a write's), which has none.

    A type's hooks (a `HookLeaf`, of any subclass) are asked outside the
    write's checks (`_not_checking_write`: ``scrud_workflow`` refuses
    there), as the route's request check asks them
    (`scrud_django.core.resources.check_request`) and the DRF route does
    (``check_request_hooks``, once its ``write_checks`` is over): an
    option's pairing with its object check then answers as the request
    check did."""

    def allows(entry):
        if isinstance(entry, AnyOf):
            return any(
                option_allows_request(option, request, body) for option in entry.options
            )
        if isinstance(entry, _hook_class()):
            with _not_checking_write():
                return entry.has_permission(request, body)
        return entry.has_permission(request, body)

    return all(map(allows, option))


def meets_object_checks(requirement, request, obj, *, request_checks=False):
    """Whether every entry of `requirement` (any option of an ``AnyOf``
    suffices) allows ``has_object_permission(request, obj)``; one that raises
    a refusal (`REFUSALS`) refuses `obj` only.

    With `request_checks` (the facade's), an ``AnyOf`` option counts only
    when each of its entries also allows the request
    (`option_allows_request`, without a body) before its object checks, as
    DRF's ``|`` answers an object (``OR.has_object_permission``); a refusal
    a request check raises refuses `obj`, as it refuses DRF's whole ``|``.
    Without (the DRF views' list and readability, as on ``develop``), the
    object checks alone."""

    def satisfied(option):
        if request_checks and not option_allows_request(option, request):
            return False
        return all(meets(e) for e in option)

    def meets(entry):
        if isinstance(entry, AnyOf):
            return any(satisfied(option) for option in entry.options)
        try:
            return entry.has_object_permission(request, obj)
        except REFUSALS:
            # Raised for this object, the detail route refuses it alone.
            return False

    try:
        return all(meets(entry) for entry in Requirement.unchecked(requirement))
    except REFUSALS:
        # Raised by a request check: DRF's ``|`` raises it for the object.
        return False


def _grants_meet(requirement, request, obj, permission_of):
    def reads(leaf):
        return may_act_in_context(
            request, permission_of(leaf), obj, leaf.contexts_for(obj)
        )

    def meets(entry):
        if isinstance(entry, AnyOf):
            return any(all(map(meets, option)) for option in entry.options)
        if is_hook(entry):
            # A leaf that only refuses (a type's hooks): no grants to ask.
            return True
        return reads(entry)

    return all(map(meets, Requirement.unchecked(requirement)))


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


def readable_by_object_checks(requirement, request, obj, *, request_checks=False):
    """Whether the object checks of `requirement` allow a read of `obj`, as
    a list asks them (`filter_by_object_checks`): `request` is a GET of the
    detail route (`as_get`). True when no leaf overrides its object checks
    (`object_check_requirement`: the grants decide alone). They can only
    refuse: the grants are asked apart (`readable_by_grants`). With
    `request_checks` (the facade's), an ``AnyOf`` option also asks its
    request checks (`meets_object_checks`)."""
    narrowing = object_check_requirement(requirement)
    if narrowing is None:
        return True
    return meets_object_checks(narrowing, request, obj, request_checks=request_checks)


def filter_by_object_checks(
    queryset, request, requirement, object_for_checks, *, request_checks=False
):
    """`queryset` (a list, already RBAC-filtered) restricted to the objects
    `requirement` allows (`meets_object_checks`, given
    ``object_for_checks(obj)``, with `request_checks`), as the detail route
    asks it, with `request`'s method (GET or HEAD). An object stored in a
    context reserved once normalised is left out (its detail route is a
    404). Every object examined counts against
    ``SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]``: past it,
    ``access.ConditionalListTooLarge``."""
    requirement = Requirement.unchecked(requirement)
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
        if meets_object_checks(
            requirement, request, object_for_checks(obj), request_checks=request_checks
        ):
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
