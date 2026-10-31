"""What the scrud views' permission checks are given, and the
``scrud_workflow`` operator.

Every check reads plain data, as scoped-rbac's own checks do: a request
check the parsed body itself (``request.data``), an object check of a
resource its content (`content_of`, the stored ``dict`` itself), and the
workflow check the body as sent. A check (a permission class, an operator, a
``contexts_from``) must only read what it is given: changing it changes what
is stored, and is a bug in that check, not something scrud-django guards.
"""

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar

# What `_transition` holds outside the workflow check.
_OUTSIDE = object()

# The transition the workflow check is authorizing, while it asks the policy
# and the view's permission classes about it (`checking_transition`).
_transition = ContextVar("scrud_django_transition", default=_OUTSIDE)


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
    return None if transition is _OUTSIDE else transition


@contextmanager
def checking_transition(transition):
    """While the block runs, the workflow check is authorizing `transition`
    (``None``: a context where no workflow policy applies): what
    `current_transition` and the ``scrud_workflow`` operator read. Reset
    when the block ends, however it ends."""
    token = _transition.set(transition)
    try:
        yield
    finally:
        _transition.reset(token)


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


def authorized_transitions(resource, configuration):
    """The ``scrud_workflow`` operator: in the workflow check, allows the
    transition it authorizes (`current_transition`) when it is in the
    configuration's ``authorized_transitions``, and denies every other one,
    including where no workflow policy applies (there is no transition), so
    a grant conditioned on it allows no write there.

    Outside the workflow check (scoped-rbac's request check, the object and
    post-save checks) it allows: the transition is only known to the
    `WorkflowValidator`, which authorizes it in every context of the write;
    otherwise the workflow validation would run twice. `resource` (the body,
    or the stored content) is not read.

    To use this `scoped_rbac.policy.Operator`, define an operator condition that
    specifies the `authorized_transitions` property. Here's an example: ```json {
    "condition": {"operator": "scrud_workflow", "authorized_transitions": ["start"]} }
    ```

    The name for the initial transition is `__INITIALIZE__`, and for an update
    that stays in the same state `__SAME_STATE__`.
    """
    transition = _transition.get()
    if transition is _OUTSIDE:
        return True
    return transition is not None and transition in (
        configuration.get("authorized_transitions") or []
    )
