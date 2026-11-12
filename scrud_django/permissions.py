"""The DRF side of the scrud views' permission checks. What they are given,
and the ``scrud_workflow`` operator, are in `scrud_django.core.authz` (no DRF)
and re-exported here.

Every check reads plain data, as scoped-rbac's own checks do: a request
check the parsed body itself (``request.data``), an object check of a
resource its content (`content_of`, the stored ``dict`` itself), and the
workflow check the body as sent. A check (a permission class, an operator, a
``contexts_from``) must only read what it is given: changing it changes what
is stored, and is a bug in that check, not something scrud-django guards.

A grant's CEL condition evaluated in one context reads the object as it is
in that context (`condition_resource`): a resource stored in several
contexts, or in the default one because its ``rbac_context`` is not a
string, is read with that context as its ``rbac_context``. Operator
functions and permission classes of your own still get the object as
stored (or the body as sent).
"""

from contextlib import contextmanager
from contextvars import ContextVar

from rest_framework.permissions import AND, NOT, OR
from scoped_rbac.permissions import IsAuthorized, custom_contexts_for

from scrud_django.core.authz import (  # noqa: F401
    _MALFORMED_REPORTED,
    _MALFORMED_REPORTED_LOCK,
    _MALFORMED_REPORTED_MAX,
    _OUTSIDE,
    _WORKFLOW_AUTHORIZED,
    _WRITE,
    _checking_transition,
    _checking_write,
    _mark_workflow_authorized,
    _not_checking_write,
    _report_malformed,
    _transition,
    _transition_state,
    _workflow_authorized,
    authorized_transitions,
    condition_resource,
    content_of,
    current_transition,
    in_context,
    may_act_in_context,
    may_read_in_context,
    resource_contexts,
    with_context,
)

# True while the workflow check asks the view's permissions about a context:
# a condition there reads the body with that context as its rbac_context
# whatever contexts the body names (`with_context`), the contexts an update
# moves the resource out of included (`condition_resource`).
_every_context = ContextVar("scrud_django_every_context", default=False)


def reads_each_context(leaf):
    """Whether the ``IsAuthorized`` `leaf` asks the grants reading each
    context as the object is there (`_reads_in_context`, given by
    `reading_content` to a leaf keeping scoped-rbac's
    ``has_context_permission``). One with a ``has_context_permission`` of its
    own does not: the scrud views then ask the grants so beside it."""
    return getattr(vars(leaf).get("has_context_permission"), "_scrud_in_context", False)


@contextmanager
def _reading_every_context():
    token = _every_context.set(True)
    try:
        yield
    finally:
        _every_context.reset(token)


def _reads_in_context(leaf):
    """Give the ``IsAuthorized`` `leaf`, when it keeps scoped-rbac's
    ``has_context_permission``, one that asks it with `condition_resource`:
    its conditions read the object as it is in the context asked, and an
    operator gets the object itself. scoped-rbac's ``has_object_permission``
    asks it for each of the object's contexts, so the object checks read
    each context so. A leaf whose class (or instance) has a
    ``has_context_permission`` of its own is left alone: that code gets the
    object as stored, as it always did. Idempotent. Its class is unchanged,
    so `scrud_django.filters.overrides_object_checks` still judges the
    class."""
    if "has_context_permission" in vars(leaf):
        return  # already given one, or one of the instance's own
    if type(leaf).has_context_permission is not IsAuthorized.has_context_permission:
        return
    asks = leaf.has_context_permission

    def has_context_permission(request, view, obj, context):
        resource = condition_resource(obj, context, every=_every_context.get())
        return asks(request, view, resource, context)

    has_context_permission._scrud_in_context = True
    leaf.has_context_permission = has_context_permission


def _leaves(permission):
    """The permissions composed into `permission` by DRF's ``&``, ``|`` and
    ``~`` (their ``op1``/``op2``), or `permission` itself."""
    if not isinstance(permission, (AND, OR, NOT)):
        yield permission
        return
    for name in ("op1", "op2"):
        operand = getattr(permission, name, None)
        if operand is not None:
            yield from _leaves(operand)


def reading_content(permissions):
    """`permissions` (an iterable of DRF permissions, composed ones
    included), each ``IsAuthorized`` among them whose class keeps
    scoped-rbac's ``contexts_for`` given `resource_contexts` as its
    ``contexts_for``, so that it reads a resource's contexts from the content
    the scrud views' checks give it. Judged by the class, as scoped-rbac's
    ``custom_contexts_for`` judges it, so such a permission still counts as
    keeping the default (a list stays on its SQL path). Idempotent: the same
    function every time, never a wrapper. Every ``IsAuthorized`` among them
    also reads each context as the object is in it (`_reads_in_context`).
    Returns the permissions, as a list."""
    permissions = list(permissions)
    for permission in permissions:
        for leaf in _leaves(permission):
            if not isinstance(leaf, IsAuthorized):
                continue
            if custom_contexts_for(leaf) is None:
                leaf.contexts_for = resource_contexts
            _reads_in_context(leaf)
    return permissions


@contextmanager
def checks_read_content(view):
    """While the block runs, every permission `view.get_permissions()`
    returns, whatever builds it, goes through `reading_content`: for the
    checks scoped-rbac and DRF run themselves (the detail route's object
    check and readability, which call ``get_permissions()`` on their own). A
    block inside another leaves the outer one in place."""
    own = view.get_permissions
    if getattr(own, "_scrud_reads_content", False):
        yield
        return

    def get_permissions():
        return reading_content(own())

    get_permissions._scrud_reads_content = True
    missing = object()
    previous = view.__dict__.get("get_permissions", missing)
    view.get_permissions = get_permissions
    try:
        yield
    finally:
        if previous is missing:
            del view.get_permissions
        else:
            view.get_permissions = previous
