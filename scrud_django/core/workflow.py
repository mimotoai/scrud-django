"""The workflow check of a write, without DRF: every scrud front end asks it
(``scrud_django.validators.WorkflowValidator`` on DRF), and so may an app's
own models, with a type IRI and plain JSON mappings.

A write is checked under the workflow policy of every context concerned
(`scrud_django.workflow.workflow_policies_for`: the context's stored policy,
else the settings default): those the stored content is in and those the
proposed content names, so that moving a resource out of a context is
checked under that context's policy too. In each of them that has a policy,
a create must be a valid initial state and an update a valid transition, and
the user must be authorized for the transition (`current_transition`, which
the ``scrud_workflow`` operator reads) in that context. A context an update
moves the resource into (one the proposed content names and the stored
content is not in) is checked as a create there: the content must be a valid
initial state and the user authorized for ``__INITIALIZE__`` by the
request's method (``http.PUT``, not ``http.POST``). All of them must pass. A
resource in no context is checked under the settings default, and only a
superuser may make its transition, as scoped-rbac's object checks allow. In
a context where no policy applies, a grant conditioned on ``scrud_workflow``
allows nothing (`NoWorkflowPolicyDenied`). Operators and the `narrow`
checks read the body as sent, a ``dict``; a CEL condition reads it with the
context checked as its ``rbac_context`` (`condition_resource`).

An error under a stored policy the requester may not read, or about a stored
resource they may not read, names neither the policy nor its states
(`HiddenWorkflowTransitionError`).
"""

from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from django.core.exceptions import ImproperlyConfigured
from scoped_rbac import access
from scoped_rbac.policy import Permission

from scrud_django.core.authz import (
    _checking_transition,
    _checking_write,
    _consume_workflow_mark,
    _mark_workflow_authorized,
    _not_checking_write,
    _reading_every_context,
    _workflow_authorized,
    condition_resource,
    content_of,
    may_read_in_context,
)
from scrud_django.exceptions import (
    HiddenWorkflowTransitionError,
    WorkflowPolicyInvalidError,
    WorkflowTransitionError,
)
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import Resource
from scrud_django.workflow import (
    WORKFLOW_POLICY_TYPE_URI,
    TransitionData,
    WorkflowPolicy,
    default_workflow_policy_for,
    stored_policies_in,
    workflow_policies_for,
)


class WorkflowRequestRequired(ImproperlyConfigured):
    """`workflow_check` was asked without a request where a workflow policy
    applies: there is no user to authorize the transition for."""

    def __init__(self, message=None):
        super().__init__(
            message
            or "The workflow check needs the request to authorize workflow transitions."
        )


class NoWorkflowPolicyDenied(access.AccessDenied):
    """A write refused in a context where no workflow policy applies: a grant
    conditioned on ``scrud_workflow`` is the only reason it would be allowed
    there (`authorize_without_policy`). Its message and code are
    ``access.AccessDenied``'s."""


def authorizing_write(
    request,
    *,
    covered: bool,
    authorized: bool = False,
    type_iri: str | None = None,
    stored_pk: int | None = None,
):
    """The block in which a check of `request` runs: for a write the
    workflow check covers (`covered`), with the ``scrud_workflow`` operator
    allowing (the workflow check decides the transition); with `authorized`
    (the post-save check), only once the workflow check has authorized the
    write of the resource `stored_pk` (``None`` for a create) of the type
    `type_iri` (`_workflow_authorized`: never without a `type_iri`).
    Anywhere else ``scrud_workflow`` refuses."""
    if covered and (
        not authorized
        or _workflow_authorized(request, type_iri=type_iri, stored_pk=stored_pk)
    ):
        return _checking_write()
    return _not_checking_write()


def check_policy_is_unique(content, pk=None):
    """Refuse (`WorkflowPolicyInvalidError`, 400) the ``WorkflowPolicy``
    `content` (of the resource `pk`, ``None`` for a create) when another
    stored policy for its type applies in one of its contexts. Run by
    `validate_policy_write`, and again after the save under
    `lock_workflow_policy_type`
    (`scrud_django.core.writes.serialized_policy_save`), so that
    of two concurrent writes the second is refused."""
    resource_type = content.get("resource_type")
    contexts = rbac_contexts_of(content)
    stored = stored_policies_in(resource_type, contexts)
    for rbac_context in contexts:
        others = [policy for policy in stored[rbac_context] if policy.pk != pk]
        if others:
            raise WorkflowPolicyInvalidError(
                f"A WorkflowPolicy for {resource_type!r} already exists in "
                f"context {rbac_context!r}; update or delete it instead."
            )


def validate_policy_write(body: Mapping, stored_pk: int | None) -> None:
    """Refuse (`WorkflowPolicyInvalidError`, 400, with the reason as its
    ``detail``) a ``WorkflowPolicy`` write `body` (replacing the stored
    policy `stored_pk`, ``None`` for a create) that fails `WorkflowPolicy`'s
    checks, whose ``rbac_context`` is present but not a string (a policy is
    in exactly one context), or that would be a second stored policy for its
    type in its context (`check_policy_is_unique`)."""
    if "rbac_context" in body and not isinstance(body["rbac_context"], str):
        raise WorkflowPolicyInvalidError(
            "A WorkflowPolicy's rbac_context must be a string (one context), "
            f"not {body['rbac_context']!r}."
        )
    try:
        WorkflowPolicy(body)
    except ImproperlyConfigured as e:
        raise WorkflowPolicyInvalidError(str(e)) from e
    check_policy_is_unique(body, stored_pk)


class NullContent:
    """The type of `NULL_CONTENT`, its one instance (``NullContent()`` is
    it): falsy, ``repr`` ``NULL_CONTENT``."""

    __slots__ = ()
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self):
        return False

    def __repr__(self):
        return "NULL_CONTENT"

    def __reduce__(self):
        # Copied or pickled, it stays the one instance.
        return "NULL_CONTENT"


# An `Update`'s stored content (`prior`) when the resource's content is JSON
# null, or not an object (content stored before 0.12.0 could be a list):
# the check reads it in the default context (`rbac_contexts_of`), in no
# state (`WorkflowPolicy.state`), and an error about it is hidden unless the
# stored resource is readable. A create is a `Create`, never null content.
NULL_CONTENT = NullContent()

# An alias of `NULL_CONTENT`, its earlier name (re-exported by
# ``scrud_django.validators``).
NULL_STORED_CONTENT = NULL_CONTENT


def _contexts(value, name):
    """`value` (a `Create`'s or `Update`'s `name`) as a tuple of contexts,
    or ``None``; a ``str`` is one context, not a sequence of them."""
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence of contexts, not {value!r}.")
    return tuple(value)


def _content(value, name):
    """Refuse (``TypeError``) a `value` (a write's `name`) that is not a
    ``Mapping``: a body, or stored content, that is a JSON object."""
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a Mapping, not {type(value).__name__}.")


@dataclass(frozen=True)
class Create:
    """A create of `proposed` (the body as sent), the input `workflow_check`
    checks as a valid initial state in each context concerned.

    - `contexts_after` are the contexts the write names; by default
      ``rbac_contexts_of(proposed)``.

    Raises ``TypeError`` for a `proposed` that is not a ``Mapping``."""

    proposed: Mapping
    contexts_after: Sequence[str] | None = None

    def __post_init__(self):
        _content(self.proposed, "proposed")
        object.__setattr__(
            self, "contexts_after", _contexts(self.contexts_after, "contexts_after")
        )


@dataclass(frozen=True)
class Update:
    """An update of the stored resource `stored_pk` from `prior` (its
    stored content: a ``Mapping``, or `NULL_CONTENT`, `prior_from`) to
    `proposed` (the body as sent), the input `workflow_check` checks as a
    transition in each context concerned.

    - `readable` says whether the requester may read the stored resource:
      an error about an update of one they may not read is hidden
      (`errors_hidden_unless_readable`). Required: a caller asks it.
    - `stored_pk` is the resource the workflow mark names
      (`_mark_workflow_authorized`); ``None``, an update that does not say
      which resource it writes, is not marked, and the post-save check of
      no save matches it.
    - `contexts_before` and `contexts_after` are the contexts the resource
      is in and those the write names; by default ``rbac_contexts_of`` of
      `prior` and of `proposed`.

    Raises ``TypeError`` for a `prior` or `proposed` of another type or a
    `readable` that is not callable."""

    prior: Mapping | NullContent
    proposed: Mapping
    readable: Callable[[], bool]
    stored_pk: int | None
    contexts_before: Sequence[str] | None = None
    contexts_after: Sequence[str] | None = None

    def __post_init__(self):
        if not isinstance(self.prior, NullContent):
            _content(self.prior, "prior (or NULL_CONTENT)")
        _content(self.proposed, "proposed")
        if not callable(self.readable):
            raise TypeError(
                "An Update's readable must be callable (whether the requester "
                f"may read the stored resource), not {self.readable!r}."
            )
        for name in ("contexts_before", "contexts_after"):
            object.__setattr__(self, name, _contexts(getattr(self, name), name))


def prior_from(value: object) -> Mapping | NullContent:
    """`value` (stored content) as an `Update`'s ``prior``: `NULL_CONTENT`
    for ``None`` (JSON null), a ``Mapping`` itself. Anything else raises
    ``TypeError``. For an app's own models; a ``Resource``'s is
    `prior_of`."""
    if value is None:
        return NULL_CONTENT
    if isinstance(value, Mapping):
        return value
    raise TypeError(
        f"Stored content must be a Mapping or None, not {type(value).__name__}."
    )


def prior_of(instance: Resource) -> Mapping | NullContent:
    """The stored content of the ``Resource`` `instance` as an `Update`'s
    ``prior`` (`prior_from`). Content that is not an object, which only a
    resource stored before 0.12.0 can have, is `NULL_CONTENT`, which the
    check reads as it always read such content: in the default context and
    in no state."""
    content = instance.content
    return content if isinstance(content, Mapping) else NULL_CONTENT


def plain_body(data):
    """`data` (a parsed body) as the workflow check gives it to operators and
    `narrow` checks: itself when it is a ``dict``, a ``dict`` copy of a
    mapping of another type, ``{}`` for a body that is not a JSON object."""
    if type(data) is dict:
        return data
    if isinstance(data, Mapping):
        return dict(data)
    return {}


def workflow_check(
    request,
    *,
    type_iri: str,
    write: Create | Update,
    narrow: Sequence[Callable] = (),
    post_save_type_iri: str | None = None,
) -> list[tuple[str | None, WorkflowPolicy, TransitionData]]:
    """Check `request`'s `write` (a `Create`, or an `Update` of its stored
    content) of the type `type_iri`, as the module describes, and return the
    transitions it authorized: ``(context, policy, transition data)`` for
    each context with a policy (``None`` as the context: the resource is in
    none).

    `narrow` checks, ``(request, body, context) -> bool``, may refuse a
    transition the request's policy allows, never allow one it refuses:
    each is asked, while the transition is being authorized, with the body
    as sent (`plain_body`) and the context.

    It first removes any mark an earlier check left on the request
    (`_consume_workflow_mark`): this check replaces it, and one that fails
    leaves none. When every context passes, it marks the request authorized
    (`_mark_workflow_authorized`) for this one write: of the stored
    resource (an `Update`'s ``stored_pk``; ``None`` for a `Create`), named
    by the type `post_save_type_iri` (the type the write's post-save check
    names; by default `type_iri`). An `Update` without a ``stored_pk`` is
    not marked: the post-save check of no save matches it. The save that
    uses the mark consumes it (`scrud_django.core.writes.save_checked`).

    Raises ``TypeError`` for a `write` that is neither (a delete runs no
    workflow check: follow-up ``delete-no-workflow-check``),
    `WorkflowTransitionError` (or a subclass) for a transition that is
    invalid or not authorized, `NoWorkflowPolicyDenied` in a context
    without a policy, and `WorkflowRequestRequired` without a request where
    a policy applies."""
    _consume_workflow_mark(request)
    if isinstance(write, Create):
        prior = None
        readable = None
        stored_pk = None
        contexts_before: Sequence[str] = ()
    elif isinstance(write, Update):
        prior = write.prior
        readable = write.readable
        stored_pk = write.stored_pk
        stored_before = write.contexts_before
        contexts_before = (
            rbac_contexts_of(prior) if stored_before is None else stored_before
        )
    else:
        raise TypeError(
            "workflow_check checks a Create or an Update, not "
            f"{type(write).__name__}; a delete runs no workflow check "
            "(follow-up delete-no-workflow-check)."
        )
    proposed = write.proposed
    contexts_after = write.contexts_after
    if contexts_after is None:
        contexts_after = rbac_contexts_of(proposed)
    contexts = list(dict.fromkeys(list(contexts_before) + list(contexts_after)))
    if contexts:
        policies = list(workflow_policies_for(type_iri, contexts))
    else:
        policies = [(None, default_workflow_policy_for(type_iri))]
    # The request is needed only where a policy applies; without one (a
    # serializer used outside a request) there is no user whose grants a
    # context without a policy could condition.
    if request is None:
        if any(p is not None for _, p in policies):
            raise WorkflowRequestRequired()
        return []
    transitions = []
    for c, policy in policies:
        if policy is None:
            continue
        # A context the resource enters (not one it is stored in) has
        # no prior state there: the write creates it there.
        entering = prior is not None and c is not None
        entering = entering and c not in contexts_before
        with errors_hidden_unless_readable(policy, request, prior, readable):
            transitions.append(
                (c, policy, transition_for(policy, proposed, prior, entering))
            )
    for c, policy, transition_data in transitions:
        with errors_hidden_unless_readable(policy, request, prior, readable):
            authorize(request, type_iri, policy, transition_data, c, proposed, narrow)
    for c, policy in policies:
        if policy is None:
            authorize_without_policy(request, type_iri, c, proposed, narrow)
    # The post-save check lets a grant conditioned on scrud_workflow allow
    # only the one write authorized here: of this resource, of this type.
    if prior is None or stored_pk is not None:
        _mark_workflow_authorized(
            request,
            type_iri=type_iri if post_save_type_iri is None else post_save_type_iri,
            stored_pk=stored_pk,
        )
    return transitions


@contextmanager
def errors_hidden_unless_readable(workflow_policy, request, prior=None, readable=None):
    """A workflow error becomes a `HiddenWorkflowTransitionError`, which
    names neither the policy nor any state (the same status, ``type`` and
    ``title``), when the requester may not read what it would reveal:

    - a stored policy they may not read (``http.GET`` on ``WorkflowPolicy``
      in none of its contexts): the resource may be in a context of someone
      else's;
    - for an update (a `prior`), the stored resource, when `readable` says
      they may not read it: the error's prior state, its message and which
      error it is all tell the stored state, which a grant to write (one
      conditioned on ``scrud_workflow``, say) does not let them read.

    Other errors (under readable policies and settings defaults, for a
    create, or for a reader of the resource) are unchanged."""
    try:
        yield
    except WorkflowTransitionError as e:
        stored = workflow_policy.resource
        if stored is not None and not may_read_in_context(
            request,
            WORKFLOW_POLICY_TYPE_URI,
            content_of(stored),
            stored.rbac_contexts,
        ):
            raise HiddenWorkflowTransitionError() from e
        if prior is not None and readable is not None and not readable():
            raise HiddenWorkflowTransitionError() from e
        raise


def transition_for(workflow_policy, proposed, prior, entering=False):
    """The `TransitionData` of writing `proposed` under `workflow_policy`: a
    create's ``__INITIALIZE__`` (`proposed` must be an initial state) when
    there is no `prior` content, or when the write moves the resource into
    this context (`entering`); else the transition from `prior`."""
    if prior is None or entering:
        return workflow_policy.initial_transition(proposed)
    return workflow_policy.determine_transition(proposed, prior)


def authorize(
    request, type_iri, workflow_policy, transition_data, rbac_context, data, narrow=()
):
    """Raise unless the user may make `transition_data`'s transition in
    `rbac_context` (``None``: the resource is in no context)."""
    if not allowed(
        request, type_iri, rbac_context, transition_data.transition, data, narrow
    ):
        raise WorkflowTransitionError(
            workflow_policy.reference,
            transition_data.prior_state,
            transition_data.proposed_state,
            message="The user isn't authorized to perform this state transition on this resource.",
        )


def authorize_without_policy(request, type_iri, rbac_context, data, narrow=()):
    """Where no workflow policy applies there is no transition: a grant
    conditioned on ``scrud_workflow`` allows nothing there (it is asked
    with no transition, ``None``), and the write is refused
    (`NoWorkflowPolicyDenied`, 403) when such a grant is the reason it would
    otherwise be allowed. Where the user holds no grant at all, or an
    unconditional or CEL one, the object and post-save checks decide."""
    if allowed(request, type_iri, rbac_context, None, data, narrow):
        return
    if not allowed(
        request,
        type_iri,
        rbac_context,
        data=data,
        narrow=narrow,
        transition_check=False,
    ):
        return
    raise NoWorkflowPolicyDenied()


def allowed(
    request,
    type_iri,
    rbac_context,
    transition=None,
    data=None,
    narrow=(),
    transition_check=True,
):
    """Whether the request's action on `type_iri` is allowed in
    `rbac_context` (``None``: no context, superusers only, as scoped-rbac's
    object checks) for `transition`: the ``scrud_workflow`` operator, and
    each `narrow` check, reads it as `current_transition` (set only while
    this check asks), and every operator reads the body `data` as sent, a
    ``dict`` (`plain_body`); a CEL condition reads it with `rbac_context` as
    its ``rbac_context`` (`condition_resource`), as scoped-rbac's request
    check reads each value of a list. Without `transition_check`, on the
    body alone, as the write's request and post-save checks ask it
    (``scrud_workflow`` allows there)."""
    permission = Permission(access.action_iri_for(request.method), type_iri)
    body = plain_body(data)
    if not transition_check:
        with _checking_write():
            return allowed_on(request, permission, rbac_context, body, narrow)
    with _checking_transition(transition):
        return allowed_on(request, permission, rbac_context, body, narrow)


def allowed_on(request, permission, rbac_context, body, narrow=()):
    """`allowed`'s decision for `body` in `rbac_context`: the policy's,
    narrowed by each `narrow` check ``(request, body, context) -> bool``."""
    if rbac_context is None:
        return access.may_act_on(request, permission, body, [])
    # A CEL condition reads the body as it is in `rbac_context`, with that
    # context as its rbac_context (condition_resource with every=True),
    # whether the write keeps the resource there, moves it in, or moves it
    # out; an operator gets the body as sent.
    if not access.policy_for(request).should_allow(
        permission,
        rbac_context,
        resource=condition_resource(body, rbac_context, every=True),
    ):
        return False
    # The narrow checks may refuse what the policy allows, never allow what
    # it refuses: here `body` is the request body, which the writer chose.
    # Grants they ask read it as the policy did just above.
    with _reading_every_context():
        return all(check(request, body, rbac_context) for check in narrow)
