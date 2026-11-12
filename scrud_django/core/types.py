"""Per-type hooks declared at registration, honoured by every front end
(``registration.json_resource_type(..., checks=, object_checks=,
content_filter=)``, ``registration.computed_resource_type(..., compute=)``):
the front-end-neutral counterpart of an ``IsAuthorized`` subclass and of a
``ComputedResourceViewSet.compute``.

The hooks only refuse: a type's requirement (`requirement_for`) is its
grants (`GrantsLeaf`) **and** its hooks (`HookLeaf`), so a hook can narrow
what the grants allow, never allow what they refuse.
"""

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from scoped_rbac import access

from scrud_django.core.authz import resource_contexts
from scrud_django.core.requirements import GrantsLeaf, Leaf, Requirement
from scrud_django.mixins.resource_mixin import rbac_contexts_of


@dataclass(frozen=True)
class TypeOptions:
    """A type's hooks, as registered:

    - ``checks``: ``(request, resource_type, body: Mapping, context: str) ->
      bool``, each asked of a write in each context it writes (the request
      check), of a saved resource in each of its contexts (the post-save
      check) and of a workflow transition in each context it is authorized
      in (``narrow``): the counterpart of ``has_context_permission``;
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

_OPTIONS: dict[str, TypeOptions] = {}
_OPTIONS_LOCK = threading.Lock()


def register_type_options(slug: str, options: TypeOptions) -> None:
    """Record `options` as the type `slug`'s. They **replace** any registered
    before for it (a URLconf imported again, or a second URLconf registering
    the slug): never merged, so no earlier hook is kept."""
    with _OPTIONS_LOCK:
        _OPTIONS[slug] = options


def type_options(slug: str) -> TypeOptions:
    """The options registered for the type `slug`; `TypeOptions()` (no
    hooks) when none are."""
    return _OPTIONS.get(slug, TypeOptions())


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
    - ``has_object_permission``: every object check;
    - ``has_context_permission``: every check.

    ``checks`` govern writes (the request check, the post-save check, which
    asks ``has_context_permission`` in each context of the saved resource
    whatever ``overrides_object_checks`` says, and a workflow transition's
    ``narrow``); ``object_checks`` govern reads and objects: only they
    make a list check its objects one by one (``overrides_object_checks``).

    It grants nothing (``grants_nothing``), so no grants are asked for it,
    and none beside it (``reads_each_context`` is true): the requirement it
    is in holds the grants (`GrantsLeaf`, or a DRF view's own permissions).
    It reads a resource's contexts from its content (`resource_contexts`),
    and names `type_iri` (by default the type's ``type_uri``)."""

    reads_each_context = True
    grants_nothing = True

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
        return all(
            check(request, self.resource_type, obj)
            for check in self.options.object_checks
        )

    def has_context_permission(self, request, obj, context):
        return all(
            check(request, self.resource_type, obj, context)
            for check in self.options.checks
        )


def hook_leaf(resource_type, slug: str, *, type_iri=None) -> HookLeaf | None:
    """The `HookLeaf` of the type `slug`'s registered hooks (`type_options`),
    or ``None`` when it has none."""
    options = type_options(slug)
    if not has_hooks(options):
        return None
    return HookLeaf(resource_type, options, type_iri=type_iri)


def requirement_for(resource_type, slug: str) -> Requirement:
    """What `resource_type` (registered as `slug`) requires of a request and
    an object: its grants (`GrantsLeaf` on its ``type_uri``) and, when it
    has hooks, its `HookLeaf`."""
    requirement: Requirement = [GrantsLeaf(resource_type.type_uri)]
    leaf = hook_leaf(resource_type, slug)
    if leaf is not None:
        requirement.append(leaf)
    return requirement
