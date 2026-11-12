"""A DRF view's permissions as a neutral requirement
(`scrud_django.core.requirements`): each ``IsAuthorized`` of
``view.rbac_requirement()`` becomes a `ViewLeaf`, keeping DRF's ``|`` as
``access.AnyOf``, and the hooks registered for the view's type
(`type_hook_leaf`, a `scrud_django.core.types.HookLeaf`) are required beside
them. DRF's ``~`` over an ``IsAuthorized`` has no neutral form
(`NotNeutral`): such a view keeps its view-based path in
`scrud_django.filters`."""

from contextlib import contextmanager, nullcontext

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework.exceptions import NotAuthenticated, NotFound, PermissionDenied
from rest_framework.permissions import AND, NOT, OR
from rest_framework.request import clone_request
from scoped_rbac import access
from scoped_rbac.permissions import IsAuthorized, custom_contexts_for

from scrud_django.core.authz import resource_contexts
from scrud_django.core.requirements import (
    _entries,
    register_grant_asking_leaf,
    register_stored_contexts_leaf,
    requirement_leaves,
)
from scrud_django.core.types import HookLeaf
from scrud_django.permissions import (
    _leaves,
    checks_read_content,
    reading_content,
    reads_each_context,
)


class NotNeutral(Exception):
    """A view's permissions have no neutral requirement: DRF's ``~`` wraps an
    ``IsAuthorized`` in them, which scoped-rbac's requirement leaves out
    while its ``is_readable`` still asks it."""


def overrides_object_checks(permission):
    """Whether `permission` (an ``IsAuthorized``) overrides
    ``has_object_permission`` or ``has_context_permission``: its object check
    may then refuse what the grants allow, which the SQL filter cannot see."""
    cls = type(permission)
    return (
        cls.has_object_permission is not IsAuthorized.has_object_permission
        or cls.has_context_permission is not IsAuthorized.has_context_permission
    )


@contextmanager
def as_detail_route(view):
    """`view` as its detail route has it while the block runs: its
    ``action`` is ``"retrieve"``, so that ``resource_type_iri_for`` (and so
    ``IsAuthorized``'s object check) names the item type, not the list type."""
    missing = object()
    previous = view.__dict__.get("action", missing)
    view.action = "retrieve"
    try:
        yield view
    finally:
        if previous is missing:
            del view.action
        else:
            view.action = previous


@contextmanager
def _requested_by(view, request):
    """`view`'s ``request`` is `request` while the block runs (restored when
    it ends); nothing changes when it already is."""
    missing = object()
    previous = view.__dict__.get("request", missing)
    if previous is request:
        yield
        return
    view.request = request
    try:
        yield
    finally:
        if previous is missing:
            del view.request
        else:
            view.request = previous


@contextmanager
def as_detail_read(view, request):
    """`view` as a GET of its detail route has it while the block runs, for
    asking whether an object is readable whatever the request's method: its
    ``action`` is ``"retrieve"`` (`as_detail_route`) and its ``request`` a
    copy of `request` whose method is GET (DRF's ``clone_request``), which the
    block gets. The view's own ``request`` is restored when it ends."""
    read = clone_request(request, "GET")
    missing = object()
    previous = view.__dict__.get("request", missing)
    view.request = read
    try:
        with as_detail_route(view):
            yield read
    finally:
        if previous is missing:
            del view.request
        else:
            view.request = previous


def _permission_reads_stored_contexts(leaf):
    """Whether the permission of the `ViewLeaf` `leaf` reads a resource's
    stored contexts: its class keeps scoped-rbac's ``contexts_for``
    (``custom_contexts_for``), and the instance was given none but the
    content's (`resource_contexts`, as `reading_content` gives it)."""
    permission = leaf.permission
    if custom_contexts_for(permission) is not None:
        return False
    given = getattr(permission, "__dict__", {}).get("contexts_for")
    return given is None or given is resource_contexts


@register_grant_asking_leaf
class ViewLeaf:
    """The `Leaf` of the ``IsAuthorized`` `permission` of DRF view `view`:
    each check is the permission's, given `view`. Its checks read a
    resource's contexts from its content and each context as the resource is
    there (`reading_content`, applied to `permission` when a check, or
    ``reads_each_context``, is first asked), as every scrud check of a view
    does. ``contexts_for`` is the permission's as it is: the grants are
    asked with the view's permissions reading the content
    (`checks_read_content`), and a ``contexts_for`` an instance was given
    after its permissions read it is kept.

    Its object check runs as on the detail route: with the view's
    permissions reading the content (`checks_read_content`), and, for a GET
    or HEAD (a list, or a GET copy asking whether the object is readable),
    with the view as its detail route has it (`as_detail_route`) and
    `request` as its ``request``, restored after. DRF's refusals
    (``PermissionDenied``, ``NotAuthenticated``; ``NotFound``) are raised as
    Django's (``PermissionDenied``; ``Http404``).

    The core trusts it to ask the grants (`register_grant_asking_leaf`):
    an ``IsAuthorized``'s checks do, and its ``reads_each_context`` says
    whether they read each context as the resource is there."""

    def __init__(self, permission, view):
        self.permission = permission
        self.view = view
        self._reads_content = False

    def __repr__(self):
        return f"ViewLeaf({self.permission!r}, {self.view!r})"

    def _leaf(self):
        if not self._reads_content:
            reading_content([self.permission])
            self._reads_content = True
        return self.permission

    @property
    def type_iri(self):
        return self.view.resource_type_iri

    @property
    def reads_each_context(self):
        return reads_each_context(self._leaf())

    @property
    def overrides_object_checks(self):
        return overrides_object_checks(self.permission)

    def contexts_for(self, obj):
        return self.permission.contexts_for(obj)

    def has_permission(self, request, body):
        """The permission's request check (DRF's reads `request`'s own
        body; `body` is not read)."""
        return self.permission.has_permission(request, self.view)

    def has_object_permission(self, request, obj):
        view = self.view
        reads = request.method in ("GET", "HEAD")
        content = (
            checks_read_content(view)
            if callable(getattr(view, "get_permissions", None))
            else nullcontext()
        )
        with (
            as_detail_route(view) if reads else nullcontext(),
            _requested_by(view, request) if reads else nullcontext(),
            content,
        ):
            try:
                return self._leaf().has_object_permission(request, view, obj)
            except NotFound as exc:
                raise Http404 from exc
            except (PermissionDenied, NotAuthenticated) as exc:
                raise DjangoPermissionDenied from exc

    def has_context_permission(self, request, obj, context):
        return self._leaf().has_context_permission(request, self.view, obj, context)


# Its contexts are its permission's: stored ones only while the permission
# keeps them.
register_stored_contexts_leaf(ViewLeaf, _permission_reads_stored_contexts)


def type_hook_leaf(view):
    """The `HookLeaf` of the hooks registered for `view`'s type
    (``view.type_hooks()``, ``ResourceViewMixin``'s), or ``None`` for none
    (or a view without ``type_hooks``)."""
    type_hooks = getattr(view, "type_hooks", None)
    return type_hooks() if callable(type_hooks) else None


def hook_leaves(view):
    """`type_hook_leaf` of `view` as a requirement's entries: ``[]`` for
    none."""
    leaf = type_hook_leaf(view)
    return [] if leaf is None else [leaf]


def leaves_of(requirement, view):
    """`requirement` (``IsAuthorized`` permissions of `view` and
    ``access.AnyOf`` alternatives, as ``rbac_requirement`` gives them) with
    each permission as its `ViewLeaf`; each ``AnyOf`` option is a list. A
    `HookLeaf` (`hook_leaves`) is kept as it is."""

    def leaf(entry):
        if isinstance(entry, access.AnyOf):
            return access.AnyOf(
                [[leaf(e) for e in _entries(option)] for option in entry.options]
            )
        if isinstance(entry, HookLeaf):
            return entry
        return ViewLeaf(entry, view)

    return [leaf(entry) for entry in requirement]


def permissions_of(requirement):
    """`requirement` (`leaves_of`'s) with each `ViewLeaf` as its
    permission; a `HookLeaf` is kept as it is."""

    def permission(entry):
        if isinstance(entry, access.AnyOf):
            return access.AnyOf(
                [[permission(e) for e in _entries(option)] for option in entry.options]
            )
        if isinstance(entry, HookLeaf):
            return entry
        return entry.permission

    return [permission(entry) for entry in requirement]


def _negates_is_authorized(permission):
    """Whether DRF's ``~`` wraps an ``IsAuthorized`` in `permission`."""
    if isinstance(permission, NOT):
        return any(isinstance(leaf, IsAuthorized) for leaf in _leaves(permission))
    if isinstance(permission, (AND, OR)):
        return any(
            _negates_is_authorized(operand)
            for operand in (
                getattr(permission, "op1", None),
                getattr(permission, "op2", None),
            )
            if operand is not None
        )
    return False


def requirement_of(view):
    """`view`'s requirement: ``view.rbac_requirement()`` (scoped-rbac's
    ``AccessControlledAPIView``), or for a view without one each
    ``IsAuthorized`` of ``get_permissions()``, as `ViewLeaf` entries
    (`leaves_of`). Raises `NotNeutral` when DRF's ``~`` wraps an
    ``IsAuthorized`` among them: ``rbac_requirement`` leaves out exactly
    those, so a view whose ``_rbac_permissions()`` (every ``IsAuthorized``)
    holds more than its requirement has one under ``~``. The hooks
    registered for the view's type follow them (`hook_leaves`)."""
    rbac_requirement = getattr(view, "rbac_requirement", None)
    if callable(rbac_requirement):
        requirement = list(rbac_requirement())
        rbac_permissions = getattr(view, "_rbac_permissions", None)
        every = rbac_permissions() if callable(rbac_permissions) else ()
        if len(list(requirement_leaves(requirement))) < len(every):
            raise NotNeutral(f"{type(view).__name__}: ~ over an IsAuthorized")
    else:
        permissions = getattr(view, "get_permissions", None)
        permissions = permissions() if callable(permissions) else []
        if any(map(_negates_is_authorized, permissions)):
            raise NotNeutral(f"{type(view).__name__}: ~ over an IsAuthorized")
        requirement = [p for p in permissions if isinstance(p, IsAuthorized)]
    return leaves_of(requirement, view) + hook_leaves(view)
