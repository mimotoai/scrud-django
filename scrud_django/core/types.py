"""Per-type hooks declared at registration, honoured by every front end
(``registration.json_resource_type(..., checks=, object_checks=,
content_filter=)``, ``registration.computed_resource_type(..., compute=)``):
the front-end-neutral counterpart of an ``IsAuthorized`` subclass and of a
``ComputedResourceViewSet.compute``.

The hooks only refuse: a type's requirement (`requirement_for`) is its
grants (`GrantsLeaf`) **and** its hooks (`HookLeaf`), so a hook can narrow
what the grants allow, never allow what they refuse.
"""

import importlib
import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from scoped_rbac import access

from scrud_django.core.authz import content_of, resource_contexts
from scrud_django.core.requirements import GrantsLeaf, Leaf, Requirement
from scrud_django.mixins.resource_mixin import rbac_contexts_of


@dataclass(frozen=True)
class TypeOptions:
    """A type's hooks, as registered:

    - ``checks``: ``(request, resource_type, body: Mapping, context: str) ->
      bool``, each asked of a write in each context it writes (the request
      check), of the stored resource of a PUT, PATCH or DELETE in each of its
      contexts (the object check: a write acts where the resource is, so a
      refused context can be neither deleted from nor moved out of), of a
      saved resource in each of its contexts (the post-save check) and of a
      workflow transition in each context it is authorized in (``narrow``):
      the counterpart of ``has_context_permission``;
    - ``object_checks``: ``(request, resource_type, obj: Mapping) -> bool``,
      each asked of a resource's content wherever its object is checked (the
      detail route, a list, the post-save check): the counterpart of
      ``has_object_permission``; a refused read hides the resource (404);
    - ``content_filter``: a list filters on its query parameters
      (``scrud_django.filters.ResourceContentFilter`` on DRF);
    - ``compute``: ``(request, key: str) -> Mapping``, a computed type's
      content for `key`."""

    checks: tuple[Callable, ...] = ()
    object_checks: tuple[Callable, ...] = ()
    content_filter: bool = False
    compute: Callable | None = None


# The methods whose body the request check reads (the scrud views' own
# ``BODY_METHODS``).
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

# The methods that write nothing (DRF's ``SAFE_METHODS``): their object
# check asks the object checks alone.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_OPTIONS: dict[str, TypeOptions] = {}
_SLUG_IRIS: dict[str, str] = {}
_IRI_SLUGS: dict[str, str] = {}
_OPTIONS_LOCK = threading.RLock()
_LOAD_LOCK = threading.Lock()
_loaded = False
_loading_thread: int | None = None

logger = logging.getLogger(__name__)


def _name_of(hook) -> str:
    return (
        f"{getattr(hook, '__module__', '')}.{getattr(hook, '__qualname__', repr(hook))}"
    )


def _hook_names(options: TypeOptions) -> tuple:
    """`options` by the hooks' qualified names: a URLconf imported again
    builds new closures, which are the same hooks."""
    return (
        tuple(_name_of(c) for c in options.checks),
        tuple(_name_of(c) for c in options.object_checks),
        options.content_filter,
        None if options.compute is None else _name_of(options.compute),
    )


def register_type_options(type_iri: str, options: TypeOptions, *, slug: str) -> None:
    """Record `options` as the hooks of the type `type_iri`, registered as
    `slug`. The same slug registering again **replaces** them (a URLconf
    imported again): never merged, so no earlier hook is kept, and a WARNING
    names the type when the hook set changed (hooks compared by qualified
    name). A different slug registering an IRI already registered must carry
    equal options (`ImproperlyConfigured` naming both slugs otherwise): its
    options would replace the other slug's, and a dropped hook only ever
    allows more."""
    with _OPTIONS_LOCK:
        previous = _OPTIONS.get(type_iri)
        owner = _IRI_SLUGS.get(type_iri)
        if previous is not None:
            same = _hook_names(previous) == _hook_names(options)
            if owner != slug and not same:
                raise ImproperlyConfigured(
                    f"The slugs {owner!r} and {slug!r} both register the type "
                    f"{type_iri!r} with different hooks: register equal "
                    "options, or give each its own type IRI."
                )
            if owner == slug and not same:
                logger.warning(
                    "The hooks of the type %s (slug %r) replace different "
                    "ones registered before for it.",
                    type_iri,
                    slug,
                )
        _OPTIONS[type_iri] = options
        _SLUG_IRIS[slug] = type_iri
        _IRI_SLUGS[type_iri] = slug


def type_options(type_iri: str) -> TypeOptions:
    """The options registered for the type `type_iri`; `TypeOptions()` (no
    hooks) when none are."""
    return _OPTIONS.get(type_iri, TypeOptions())


def type_options_for_slug(slug: str) -> TypeOptions:
    """The options of the type registered under `slug` (`type_options` of its
    IRI); `TypeOptions()` when the slug was never registered."""
    type_iri = _SLUG_IRIS.get(slug)
    return TypeOptions() if type_iri is None else type_options(type_iri)


def hooks_loaded() -> bool:
    """Whether any type's options have been registered in this process."""
    return bool(_SLUG_IRIS)


def ensure_hooks_loaded() -> None:
    """Import ``settings.ROOT_URLCONF`` once per process, so that a process
    that never served a request (a worker, a shell) has the hooks its
    URLconf registers; with no ROOT_URLCONF set there is nothing to import.
    A second thread waits until the first has finished; a call from the
    importing thread (the URLconf's own import may ask for requirements)
    returns at once; a failed import raises and is tried again by the next
    call."""
    global _loaded, _loading_thread
    if _loaded or _loading_thread == threading.get_ident():
        return
    with _LOAD_LOCK:
        if _loaded:
            return
        _loading_thread = threading.get_ident()
        try:
            urlconf = getattr(settings, "ROOT_URLCONF", None)
            if urlconf:
                importlib.import_module(urlconf)
            _loaded = True
        finally:
            _loading_thread = None


def has_hooks(options: TypeOptions) -> bool:
    """Whether `options` hold an authorization hook (``checks`` or
    ``object_checks``)."""
    return bool(options.checks or options.object_checks)


class HookLeaf(Leaf):
    """The `Leaf` of a type's ``checks`` and ``object_checks`` (`options`),
    each called with `resource_type`:

    - ``has_permission``: a POST, PUT or PATCH (`BODY_METHODS`; not
      scrudful's body-less copy of an OPTIONS, ``access.checks_allow``)
      whose body is a JSON object must be allowed by every check in each
      context it writes (`rbac_contexts_of`); any other request is (a body
      that is no object is left to the body's validation, as for a type
      without hooks);
    - ``has_object_permission``: every object check and, for a request
      whose method is not a safe one (`SAFE_METHODS`: a PUT, PATCH or
      DELETE, or any method it cannot tell), every check in **each** context
      the object is stored in (``contexts_for``), so that a write refused
      in a context can neither delete a resource stored there nor move it
      out (a refused write of a readable resource stays a 403: the
      readability check asks a GET); a GET, HEAD or OPTIONS asks the object
      checks alone;
    - ``has_context_permission``: every check.

    A hook is given a mapping (a resource's content, a body) as it is, and
    anything else (the `Resource` scrudful's OPTIONS checks) as its content.

    ``checks`` govern writes (the request check, the object check of a PUT,
    PATCH or DELETE in each stored context, the post-save check, which
    asks ``has_context_permission`` in each context of the saved resource
    whatever ``overrides_object_checks`` says, and a workflow transition's
    ``narrow``); ``object_checks`` govern reads and objects: only they
    make a list check its objects one by one (``overrides_object_checks``).

    It grants nothing, so no grants are asked for it, and none beside it
    (the core knows it by its class, `scrud_django.core.requirements.is_hook`):
    the requirement it is in holds the grants (`GrantsLeaf`, or a DRF view's
    own permissions).
    It reads a resource's contexts from its content (`resource_contexts`),
    and names `type_iri` (by default the type's ``type_uri``)."""

    reads_each_context = True

    def __init__(self, resource_type, options: TypeOptions, *, type_iri=None):
        self.resource_type = resource_type
        self.options = options
        self.type_iri = resource_type.type_uri if type_iri is None else type_iri

    def __repr__(self):
        return f"HookLeaf({self.type_iri!r})"

    @property
    def overrides_object_checks(self):
        return bool(self.options.object_checks)

    @property
    def narrow(self):
        """Each check as a workflow ``narrow`` callable, ``(request, body,
        context) -> bool``."""

        def narrowing(check):
            def narrow(request, body, context):
                return check(request, self.resource_type, body, context)

            return narrow

        return tuple(narrowing(check) for check in self.options.checks)

    def contexts_for(self, obj):
        return resource_contexts(obj)

    def has_permission(self, request, body):
        if request.method not in BODY_METHODS or access.checks_allow(request):
            return True
        content = access.contexts_body(body)
        if not isinstance(content, Mapping):
            return True
        return all(
            self.has_context_permission(request, content, context)
            for context in rbac_contexts_of(content)
        )

    def has_object_permission(self, request, obj):
        obj = _as_content(obj)
        if not all(
            check(request, self.resource_type, obj)
            for check in self.options.object_checks
        ):
            return False
        if getattr(request, "method", None) in SAFE_METHODS:
            return True
        # A write (PUT, PATCH, DELETE) acts in the contexts the resource is
        # stored in: every check must allow it in each of them (all, where
        # an IsAuthorized's has_object_permission takes any: a refused
        # context refuses the write whatever the resource's other ones).
        return all(
            self.has_context_permission(request, obj, context)
            for context in self.contexts_for(obj)
        )

    def has_context_permission(self, request, obj, context):
        obj = _as_content(obj)
        return all(
            check(request, self.resource_type, obj, context)
            for check in self.options.checks
        )


def _as_content(obj):
    """`obj` as a hook is given it: a mapping (a resource's content, or a
    body) as it is; anything else (the `Resource` scrudful's OPTIONS checks,
    as ``get_object()`` returns it) as its content (`content_of`)."""
    return obj if isinstance(obj, Mapping) else content_of(obj)


def hook_leaf(resource_type, slug: str, *, type_iri=None) -> HookLeaf | None:
    """The `HookLeaf` of the hooks registered for the type `slug`
    (`type_options_for_slug`), or ``None`` when it has none."""
    return _leaf_of(resource_type, type_options_for_slug(slug), type_iri)


def _leaf_of(resource_type, options, type_iri=None) -> HookLeaf | None:
    if not has_hooks(options):
        return None
    return HookLeaf(resource_type, options, type_iri=type_iri)


def requirement_for(resource_type, slug: str | None = None) -> Requirement:
    """What `resource_type` requires of a request and an object: its grants
    (`GrantsLeaf` on its ``type_uri``) and, when it has hooks (found by its
    ``type_uri``), its `HookLeaf`. `slug` defaults to ``resource_type.slug``;
    a given one that differs is a mistake (`ImproperlyConfigured`). The hooks
    are loaded first (`ensure_hooks_loaded`)."""
    if slug is not None and slug != resource_type.slug:
        raise ImproperlyConfigured(
            f"requirement_for was given the slug {slug!r} for the type "
            f"{resource_type.type_uri!r}, whose slug is {resource_type.slug!r}."
        )
    ensure_hooks_loaded()
    requirement: Requirement = [GrantsLeaf(resource_type.type_uri)]
    leaf = _leaf_of(resource_type, type_options(resource_type.type_uri))
    if leaf is not None:
        requirement.append(leaf)
    return requirement
