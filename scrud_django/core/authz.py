"""The authorization helpers every scrud front end shares: the workflow
transition being authorized, the content and contexts a check reads, and the
``scrud_workflow`` operator. Nothing here imports DRF.

Every check reads plain data, as scoped-rbac's own checks do: a request
check the parsed body itself, an object check of a resource its content
(`content_of`, the stored ``dict`` itself), and the workflow check the body
as sent. A check (a permission class, an operator, a ``contexts_from``) must
only read what it is given: changing it changes what is stored, and is a bug
in that check, not something scrud-django guards.

A grant's CEL condition evaluated in one context reads the object as it is
in that context (`condition_resource`).
"""

import logging
import threading
from collections import OrderedDict
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from scoped_rbac import access, policy
from scoped_rbac.policy import Permission

from scrud_django.mixins.resource_mixin import rbac_contexts_of

# The logger keeps its historical name: operators' log filters match it.
logger = logging.getLogger("scrud_django.permissions")

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


# True while the workflow check asks the view's permissions about a context:
# a condition there reads the body with that context as its rbac_context
# whatever contexts the body names (`with_context`), the contexts an update
# moves the resource out of included (`condition_resource`).
_every_context = ContextVar("scrud_django_every_context", default=False)


@contextmanager
def _reading_every_context():
    token = _every_context.set(True)
    try:
        yield
    finally:
        _every_context.reset(token)


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
    """The name of the workflow transition the workflow check
    (`scrud_django.core.workflow.workflow_check`) is authorizing (``"__INITIALIZE__"`` for a creation, ``"__SAME_STATE__"`` for
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


def with_context(obj, context):
    """`obj` (a resource's content, or a body: a mapping) as it is in
    `context`, whatever contexts it names: a ``dict`` copy with `context` as
    its ``rbac_context``, read as scoped-rbac names a context
    (``access.context_name``: a ``str`` subclass as its characters). `obj`
    itself when its ``rbac_context`` already is that ``str``, when `context`
    names no context (it is not a ``str``), and for any other object."""
    if not isinstance(obj, Mapping):
        return obj
    name = access.context_name(context)
    if type(name) is not str:
        return obj
    stored = obj.get("rbac_context")
    if type(stored) is str and stored == name:
        return obj
    return {**obj, "rbac_context": name}


def in_context(obj, context):
    """The fields a CEL condition evaluated in `context` reads for `obj`: for
    a resource's content (a mapping) stored in `context` (one of its
    `rbac_contexts_of`), `with_context`, so that a condition on
    ``rbac_context`` (``rbac_context != "x"``) reads the context it is
    evaluated in, not a list of contexts, nor a value that is not a string
    and stores the resource in the default context ``""``. `obj` itself for
    any other object or context (one a ``contexts_for`` of your own names
    that is not one of the resource's)."""
    if not isinstance(obj, Mapping):
        return obj
    name = access.context_name(context)
    if type(name) is not str or name not in rbac_contexts_of(obj):
        return obj
    return with_context(obj, name)


def condition_resource(obj, context, *, every=False):
    """What a grant's condition evaluated in `context` gets for `obj`: a
    ``scoped_rbac.policy.RequestResource`` whose fields, what a CEL
    condition reads, are `obj` as it is in `context` (`in_context`, or
    `with_context` with `every`), and whose instance, what an operator
    function gets, is `obj` itself, as stored or sent. `obj` itself where
    the two are the same. A ``has_context_permission`` of your own that
    asks ``super()`` can give it this to read each context so:
    ``super().has_context_permission(request, view,
    condition_resource(obj, context), context)``."""
    fields = with_context(obj, context) if every else in_context(obj, context)
    return obj if fields is obj else policy.RequestResource(obj, fields)


def may_act_in_context(request, permission, obj, contexts):
    """``access.may_act_on`` (`permission` allowed in any of `contexts`),
    with each context's conditions reading `obj` as it is there
    (`condition_resource`). An object with no usable context is a
    superuser's only, as there."""
    usable = access.usable_contexts(contexts)
    if not usable:
        return access.may_act_on(request, permission, obj, [])
    return any(
        access.may_act_on(request, permission, condition_resource(obj, c), [c])
        for c in usable
    )


def may_read_in_context(request, resource_type, obj, contexts):
    """`may_act_in_context` for ``http.GET`` on `resource_type`
    (``access.may_read``)."""
    return may_act_in_context(
        request, Permission("http.GET", resource_type), obj, contexts
    )


def authorized_transitions(resource, configuration):
    """The ``scrud_workflow`` operator: a grant conditioned on it grants
    workflow transitions, and nothing else (it fails closed).

    - In the workflow check it allows the transition it authorizes
      (`current_transition`) when that is in the configuration's
      ``authorized_transitions``, and denies every other one, including where
      no workflow policy applies (there is no transition), so a grant
      conditioned on it allows no write there.
    - It allows in the other checks of a write the workflow check covers
      (``authorizing_write``'s ``covered``; on DRF,
      ``ResourceViewMixin.checks_workflow``: a scrud resource viewset's
      ``create`` or ``update`` action): its request check, a PUT's object
      check, and the post-save check of a write the workflow check
      authorized. The transition is only known to the workflow check
      (`scrud_django.core.workflow.workflow_check`), which authorizes it in
      every context of the write; otherwise the workflow validation would
      run twice.
    - Anywhere else it denies: a GET or HEAD (detail or list), a DELETE, a
      write the workflow check does not cover (e.g. a POST ``@action`` of a
      scrud viewset, or a view of your own), and whether a refused resource
      is readable (403 or 404). A role that
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
