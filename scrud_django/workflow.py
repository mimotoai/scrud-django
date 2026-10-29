from collections.abc import Mapping
from dataclasses import dataclass
from itertools import product

from django.core.exceptions import ImproperlyConfigured
from scoped_rbac.cel import (
    InvalidExpression,
    compile_expression,
    is_true,
    new_environment,
)

from scrud_django.exceptions import (
    DefaultPolicyReference,
    StoredPolicyReference,
    WorkflowInvalidInitialStateError,
    WorkflowInvalidStateError,
    WorkflowTransitionError,
)

CEL_ENVIRONMENT = new_environment({})

WORKFLOW_POLICY_TYPE_URI = "http://api.openteams.com/json-ld/WorkflowPolicy"

# The transitions scrud-django names itself: ``__INITIALIZE__`` (a create) and
# ``__SAME_STATE__`` (an update that stays in its state). A policy may not
# define a transition with this prefix.
RESERVED_TRANSITION_PREFIX = "__"


@dataclass
class TransitionData:
    proposed_state: str
    prior_state: str
    transition: str


class WorkflowPolicy:
    """One workflow policy is in effect per resource type per access control
    context (see `workflow_policy_for`). A write is checked under the policy of
    every context the resource is in and every context it is moved to: the new
    state must be valid, and the transition authorized, in each of them.

    Constructing a policy checks it and raises ``ImproperlyConfigured`` for a
    state condition that does not compile, a transition that is not an object
    with ``from`` and ``to`` naming defined states or whose name starts with
    ``__`` (reserved), or a ``restrict_update`` that is not a list of defined
    state names. These are the checks a
    ``WorkflowPolicy`` write must pass; they are not a proof that every write
    under the policy succeeds.
    """

    def __init__(self, policy, resource=None, default_for=None):
        """`policy` is the policy's content; `resource` the stored
        ``WorkflowPolicy`` resource it comes from, or ``None`` for a policy from
        settings, whose type URI is `default_for` (``None``: the content's
        ``resource_type``)."""
        self.policy = policy
        self.resource = resource
        self.default_for = default_for
        self.initial_state_expressions = {}
        self.state_expressions = {}
        # from state -> to state -> transition name
        self.allowed_transitions = {}
        self.compile_state_expressions()
        self.compile_allowed_transitions()
        self.restrict_update = self.compile_restrict_update()

    @property
    def reference(self):
        """What a workflow error names as its policy, never its content: a
        stored policy by its URL (`StoredPolicyReference`, which RBAC
        protects; the writer may not be allowed to read it), a policy from
        settings by the type it is the default for (`DefaultPolicyReference`).
        """
        if self.resource is not None:
            return StoredPolicyReference(self.resource)
        type_uri = self.default_for
        if type_uri is None and isinstance(self.policy, Mapping):
            type_uri = self.policy.get("resource_type")
        return DefaultPolicyReference(type_uri if isinstance(type_uri, str) else None)

    def validate(self, proposed_data, prior_data=None):
        if prior_data is None:
            self.validate_initial_state(proposed_data)
        else:
            self.validate_transition(proposed_data, prior_data)
        return proposed_data

    def validate_initial_state(self, data):
        """The initial state `data` is in; raises
        ``WorkflowInvalidInitialStateError`` if it is in none."""
        proposed_state = self.determine_state(data, self.initial_state_expressions)
        if proposed_state is None:
            raise WorkflowInvalidInitialStateError(self.reference, proposed_state)
        return proposed_state

    def initial_transition(self, data):
        """The ``__INITIALIZE__`` `TransitionData` of a create of `data`: no
        prior state, its initial state as the proposed one."""
        return TransitionData(self.validate_initial_state(data), None, "__INITIALIZE__")

    def validate_transition(self, proposed_data, prior_data):
        """Given a proposed state and a previous state determine if the proposed
        transition is allowed by this policy.
        """
        self.determine_transition(proposed_data, prior_data)

    def determine_transition(self, proposed_data, prior_data):
        # TODO add prior state to Resource instead
        prior_state = self.determine_state(prior_data, self.state_expressions)
        if prior_state is not None:
            state_expressions = dict(
                map(
                    lambda s: (s, self.state_expressions[s]),
                    self.allowed_transitions.get(prior_state, {}),
                )
            )
        else:
            state_expressions = self.state_expressions
        proposed_state = self.determine_state(proposed_data, state_expressions)

        if prior_state is not None and proposed_state is None:
            # fallback and determine if the proposed_data matches any defined state
            proposed_state = self.determine_state(proposed_data, self.state_expressions)
            if proposed_state is None:
                raise WorkflowInvalidStateError(self.reference)
            else:
                if prior_state == proposed_state:
                    if proposed_state not in self.restrict_update:
                        return TransitionData(
                            proposed_state, prior_state, "__SAME_STATE__"
                        )
                    raise WorkflowTransitionError(
                        self.reference,
                        prior_state,
                        proposed_state,
                        message='Update not allowed on this state',
                    )
                raise WorkflowTransitionError(
                    self.reference, prior_state, proposed_state
                )
        elif (
            prior_state not in self.allowed_transitions
            or proposed_state not in self.allowed_transitions[prior_state]
        ):
            raise WorkflowTransitionError(self.reference, prior_state, proposed_state)

        return TransitionData(
            proposed_state,
            prior_state,
            self.allowed_transitions[prior_state][proposed_state],
        )

    def determine_state(self, data, state_expressions):
        """Apply the `state_expressions` to identify the current state of the data.
        First match is returned. Later revisions of this class may enable partial
        orders based on state conditions referencing each other, meaning the "most"
        matching expression in the first matched partial order will be the ultimate
        match. Data that is not a mapping (content stored before 0.12.0 could be
        a list) is in no state: ``None``.
        """
        if not isinstance(data, Mapping):
            return None
        for name, condition in state_expressions.items():
            # Evaluation errors, such as a property missing from the provided data,
            # count as not matching.
            if is_true(condition, data):
                return name

        return None

    def compile_state_expressions(self):
        initial_states = self.policy.get("initial_states", {})
        for name, condition in initial_states.items():
            self.initial_state_expressions[name] = self.compile_expression(condition)
        self.state_expressions = dict(self.initial_state_expressions)
        states = self.policy.get("states", {})
        for name, condition in states.items():
            self.state_expressions[name] = self.compile_expression(condition)

    def compile_expression(self, state_condition):
        try:
            return compile_expression(state_condition, CEL_ENVIRONMENT)
        except InvalidExpression as e:
            raise ImproperlyConfigured(f"Invalid workflow state condition: {e}") from e

    def compile_allowed_transitions(self):
        transitions = self.policy.get("transitions", {})

        def as_list(x):
            return [x] if not isinstance(x, list) else x  # noqa: E731

        for transition_name, transition_policy in transitions.items():
            if not isinstance(transition_name, str) or transition_name.startswith(
                RESERVED_TRANSITION_PREFIX
            ):
                raise ImproperlyConfigured(
                    f"Invalid workflow transition {transition_name!r}: a name must "
                    f"be a string not starting with {RESERVED_TRANSITION_PREFIX!r}, "
                    "which is reserved (__INITIALIZE__, __SAME_STATE__)."
                )
            if not (
                isinstance(transition_policy, Mapping)
                and "from" in transition_policy
                and "to" in transition_policy
            ):
                raise ImproperlyConfigured(
                    f"Invalid workflow transition {transition_name!r}: it must be "
                    "an object with 'from' and 'to'."
                )
            transition_from = as_list(transition_policy["from"])
            transition_to = as_list(transition_policy["to"])
            for state in transition_from + transition_to:
                if not isinstance(state, str) or state not in self.state_expressions:
                    raise ImproperlyConfigured(
                        f"Invalid workflow transition {transition_name!r}: "
                        f"{state!r} is not a defined state."
                    )
            for source, target in product(transition_from, transition_to):
                source_transitions = self.allowed_transitions.setdefault(source, {})
                source_transitions[target] = transition_name

    def compile_restrict_update(self):
        """The states whose content may not change in place: ``restrict_update``,
        absent or a list of defined state names."""
        restrict_update = self.policy.get("restrict_update", [])
        if not isinstance(restrict_update, list) or not all(
            isinstance(state, str) and state in self.state_expressions
            for state in restrict_update
        ):
            raise ImproperlyConfigured(
                "Invalid workflow restrict_update: it must be a list of defined "
                f"state names, not {restrict_update!r}."
            )
        return frozenset(restrict_update)


def _type_uri(resource_type):
    return getattr(resource_type, "type_uri", resource_type)


def default_workflow_policy_for(resource_type):
    """The settings default policy for `resource_type` (a `ResourceType` or its
    type URI), from ``SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]``, or ``None``.

    The defaults are built and checked once (`scrud_django.conf`). An invalid
    default raises ``ImproperlyConfigured`` here, for its own type only; the
    ``scrud_django.E001`` system check reports it at ``manage.py check``."""
    from scrud_django.conf import default_workflow_policy_set

    return default_workflow_policy_set().policy_for(_type_uri(resource_type))


def stored_policies_for(resource_type, context):
    """The stored ``WorkflowPolicy`` resources for `resource_type` (a
    `ResourceType` or its type URI) whose own contexts (read with
    `rbac_contexts_of`, so a policy that names no ``rbac_context`` is in the
    default context ``""``) include `context`, oldest first. There is at most
    one, unless policies were stored past the API's check (or before it)."""
    from django.db import router

    from scrud_django.mixins.resource_mixin import rbac_contexts_of
    from scrud_django.models import Resource

    # The write database: a write's uniqueness re-check (and the policy a
    # write is checked under) must not read a stale replica.
    stored = (
        Resource.objects.using(router.db_for_write(Resource))
        .filter(
            resource_type__type_uri=WORKFLOW_POLICY_TYPE_URI,
            content__resource_type=_type_uri(resource_type),
        )
        .order_by("pk")
    )
    return [policy for policy in stored if context in rbac_contexts_of(policy.content)]


def lock_workflow_policy_type(using):
    """Lock the ``WorkflowPolicy`` resource type's rows (``select_for_update``)
    on database `using`, inside the caller's transaction: ``WorkflowPolicy``
    writes take it before they re-check uniqueness, so they run one at a
    time. (SQLite ignores the lock: one writer holds the whole database. Give
    a file database ``OPTIONS["transaction_mode"] = "IMMEDIATE"`` so that a
    second writer waits instead of failing with "database is locked".)"""
    from scrud_django.models import ResourceType

    list(
        ResourceType.objects.using(using)
        .select_for_update()
        .filter(type_uri=WORKFLOW_POLICY_TYPE_URI)
        .order_by("pk")
    )


def workflow_policy_for(resource_type, context):
    """The `WorkflowPolicy` in effect for `resource_type` (a `ResourceType` or its
    type URI) in `context`, or ``None`` when the type has no workflow there.

    It is the one stored ``WorkflowPolicy`` resource for the type in
    `context` (`stored_policies_for`); a ``WorkflowPolicy`` write that would
    make a second one is refused. Without one, it is the settings default for
    the type (`default_workflow_policy_for`). A policy stored in one context
    never applies in another. Several stored policies for the type in
    `context` (stored before policies were unique, or past the API) raise
    ``ImproperlyConfigured``: none of them is picked, so the writes they
    govern fail until all but one are deleted.

    The one lookup of a policy: the `WorkflowValidator` and
    ``ResourceMixin.get_workflow_policy`` both use it.
    """
    stored = stored_policies_for(resource_type, context)
    if len(stored) > 1:
        raise ImproperlyConfigured(
            f"{len(stored)} WorkflowPolicy resources (pks "
            f"{[policy.pk for policy in stored]}) apply to "
            f"{_type_uri(resource_type)!r} in context {context!r}; there must be "
            "at most one."
        )
    if stored:
        return WorkflowPolicy(stored[0].content, resource=stored[0])
    return default_workflow_policy_for(resource_type)
