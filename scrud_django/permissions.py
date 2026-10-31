"""What the scrud views' permission checks are given, and the
``scrud_workflow`` operator.

Every check reads plain data, as scoped-rbac's own checks do: a request
check the parsed body itself (``request.data``), an object check of a
resource its content (`content_of`, the stored ``dict`` itself), and the
workflow check the body as sent. A check (a permission class, an operator, a
``contexts_from``) must only read what it is given: changing it changes what
is stored, and is a bug in that check, not something scrud-django guards.
"""

import logging
import threading
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from rest_framework.permissions import AND, NOT, OR
from scoped_rbac import access
from scoped_rbac.permissions import IsAuthorized, custom_contexts_for

from scrud_django.mixins.resource_mixin import rbac_contexts_of

logger = logging.getLogger(__name__)

# What `_transition` holds outside the workflow check and the checks of a
# write it covers: ``scrud_workflow`` refuses there (fail closed).
_OUTSIDE = object()

# What it holds during the other checks of a POST or PUT that the workflow
# check covers (`_checking_write`): its request check, a PUT's object check,
# and its post-save check once the workflow check has authorized it.
_WRITE = object()

# The transition the workflow check is authorizing, while it asks the policy
# and the view's permission classes about it (`_checking_transition`); else
# `_OUTSIDE` or `_WRITE`.
_transition = ContextVar("scrud_django_transition", default=_OUTSIDE)

# The malformed ``authorized_transitions`` values already logged (their
# ``repr``), most recent last, at most `_MALFORMED_REPORTED_MAX` of them: each
# is logged once per process, not on every evaluation (a list evaluates a
# condition once per row).
_MALFORMED_REPORTED = OrderedDict()
_MALFORMED_REPORTED_MAX = 256
_MALFORMED_REPORTED_LOCK = threading.Lock()

# The attribute of a request that records that the workflow check authorized
# its write (`_mark_workflow_authorized`).
_WORKFLOW_AUTHORIZED = "_scrud_workflow_authorized"


def current_transition():
    """The name of the workflow transition the ``WorkflowValidator`` is
    authorizing (``"__INITIALIZE__"`` for a creation, ``"__SAME_STATE__"`` for
    an update that stays in its state, else the policy's name for it), while
    it asks the request's policy and the view's ``IsAuthorized`` permissions
    (``has_context_permission``) about it; ``None`` anywhere else: outside
    the workflow check (the request, object and post-save checks), and in its
    check of a context where no workflow policy applies, where there is no
    transition.

    A permission class that refuses a transition reads it here, e.g.
    ``if current_transition() == "cancel": return False``. In the workflow
    check its ``obj`` is the request body as sent (a ``dict``)."""
    transition = _transition.get()
    return None if transition is _OUTSIDE or transition is _WRITE else transition


@contextmanager
def _transition_state(state):
    """`_transition` is `state` while the block runs, and is reset when the
    block ends, however it ends."""
    token = _transition.set(state)
    try:
        yield
    finally:
        _transition.reset(token)


def _checking_transition(transition):
    """While the block runs, the workflow check is authorizing `transition`
    (``None``: a context where no workflow policy applies): what
    `current_transition` and the ``scrud_workflow`` operator read. Reset
    when the block ends, however it ends."""
    return _transition_state(transition)


def _checking_write():
    """While the block runs, the checks of a POST or PUT that the workflow
    check covers are asked (its request check, a PUT's object check, its
    post-save check): ``scrud_workflow`` allows there, as the workflow check
    authorizes the transition itself. Reset when the block ends."""
    return _transition_state(_WRITE)


def _not_checking_write():
    """While the block runs, no write's check is asked (e.g. whether a
    refused resource is readable): ``scrud_workflow`` refuses. Reset when the
    block ends."""
    return _transition_state(_OUTSIDE)


def _mark_workflow_authorized(request):
    """Record that the workflow check authorized `request`'s write in every
    context concerned (`_workflow_authorized`)."""
    setattr(request, _WORKFLOW_AUTHORIZED, True)


def _workflow_authorized(request):
    """Whether the workflow check authorized `request`'s write."""
    return getattr(request, _WORKFLOW_AUTHORIZED, False) is True


def content_of(resource):
    """What an object check of `resource` (a ``Resource``) is given: its
    content, the stored ``dict`` itself (not a copy: a check must only read
    it), a ``dict`` copy of a mapping of another type, and ``{}`` for content
    that is not a JSON object (it has no fields)."""
    content = getattr(resource, "content", None)
    if type(content) is dict:
        return content
    if isinstance(content, Mapping):
        return dict(content)
    return {}


def resource_contexts(obj):
    """The contexts of `obj` as the scrud views' object checks read them: for
    a resource's content (a mapping, what those checks give) the resource's
    own contexts (`rbac_contexts_of`, as ``Resource.rbac_contexts``), for
    anything else scoped-rbac's ``access.object_contexts``. A
    ``contexts_for`` of your own on a scrud view calls it where it would call
    ``super().contexts_for(obj)``, which reads attributes a ``dict`` does not
    have: ``return resource_contexts(obj) + ["extra"]``."""
    if isinstance(obj, Mapping):
        return rbac_contexts_of(obj)
    return access.object_contexts(obj)


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
    function every time, never a wrapper. Returns the permissions, as a
    list."""
    permissions = list(permissions)
    for permission in permissions:
        for leaf in _leaves(permission):
            if isinstance(leaf, IsAuthorized) and custom_contexts_for(leaf) is None:
                leaf.contexts_for = resource_contexts
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


def authorized_transitions(resource, configuration):
    """The ``scrud_workflow`` operator: a grant conditioned on it grants
    workflow transitions, and nothing else (it fails closed).

    - In the workflow check it allows the transition it authorizes
      (`current_transition`) when that is in the configuration's
      ``authorized_transitions``, and denies every other one, including where
      no workflow policy applies (there is no transition), so a grant
      conditioned on it allows no write there.
    - In the other checks of a POST or PUT that the workflow check covers (a
      scrud resource view's request check, a PUT's object check, and the
      post-save check of a write the workflow check authorized) it allows: the
      transition is only known to the `WorkflowValidator`, which authorizes it
      in every context of the write; otherwise the workflow validation would
      run twice.
    - Anywhere else it denies: a GET or HEAD (detail or list), a DELETE, a
      write the workflow check does not cover (e.g. on a view of your own),
      and whether a refused resource is readable (403 or 404). A role that
      should read needs its own ``http.GET`` grant.

    ``authorized_transitions`` must be a list of strings; anything else (a
    string, a mapping, a list with a non-string in it) is an invalid
    condition, which denies everywhere and is logged (at ERROR, once per
    value per process: `_report_malformed`). `resource` (the body,
    or the stored content) is not read.

    To use this `scoped_rbac.policy.Operator`, define an operator condition that
    specifies the `authorized_transitions` property. Here's an example: ```json {
    "condition": {"operator": "scrud_workflow", "authorized_transitions": ["start"]} }
    ```

    The name for the initial transition is `__INITIALIZE__`, and for an update
    that stays in the same state `__SAME_STATE__`.
    """
    transitions = configuration.get("authorized_transitions")
    if not (
        isinstance(transitions, list) and all(isinstance(t, str) for t in transitions)
    ):
        _report_malformed(transitions)
        return False
    transition = _transition.get()
    if transition is _WRITE:
        return True
    if transition is _OUTSIDE or transition is None:
        return False
    return transition in transitions


def _report_malformed(transitions):
    """Log (at ERROR) that a ``scrud_workflow`` condition's
    ``authorized_transitions`` is `transitions`, not a list of names, once
    per value per process: the most recent `_MALFORMED_REPORTED_MAX` values
    logged are remembered (a value forgotten since is logged again)."""
    key = repr(transitions)
    with _MALFORMED_REPORTED_LOCK:
        if key in _MALFORMED_REPORTED:
            _MALFORMED_REPORTED.move_to_end(key)
            return
        _MALFORMED_REPORTED[key] = True
        while len(_MALFORMED_REPORTED) > _MALFORMED_REPORTED_MAX:
            _MALFORMED_REPORTED.popitem(last=False)
    logger.error(
        "A scrud_workflow condition's authorized_transitions must be a list "
        "of transition names (strings), not %s: the condition denies (logged "
        "once per value).",
        key,
    )
