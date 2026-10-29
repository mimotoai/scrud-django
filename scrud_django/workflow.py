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
    WorkflowInvalidInitialStateError,
    WorkflowInvalidStateError,
    WorkflowTransitionError,
)

CEL_ENVIRONMENT = new_environment({})

WORKFLOW_POLICY_TYPE_URI = "http://api.openteams.com/json-ld/WorkflowPolicy"


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

    Constructing a policy compiles it: an invalid state condition, or a
    transition that is not an object with ``from`` and ``to`` naming defined
    states, raises ``ImproperlyConfigured``.
    """

    def __init__(self, policy):
        self.policy = policy
        self.initial_state_expressions = {}
        self.state_expressions = {}
        # from state -> to state -> transition name
        self.allowed_transitions = {}
        self.compile_state_expressions()
        self.compile_allowed_transitions()

    def validate(self, proposed_data, prior_data=None):
        if prior_data is None:
            self.validate_initial_state(proposed_data)
        else:
            self.validate_transition(proposed_data, prior_data)
        return proposed_data

    def validate_initial_state(self, data):
        proposed_state = self.determine_state(data, self.initial_state_expressions)
        if proposed_state is None:
            raise WorkflowInvalidInitialStateError(self.policy, proposed_state)

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
                raise WorkflowInvalidStateError(self.policy)
            else:
                if prior_state == proposed_state:
                    if proposed_state not in self.policy.get('restrict_update', []):
                        return TransitionData(
                            proposed_state, prior_state, "__SAME_STATE__"
                        )
                    raise WorkflowTransitionError(
                        self.policy,
                        prior_state,
                        proposed_state,
                        message='Update not allowed on this state',
                    )
                raise WorkflowTransitionError(self.policy, proposed_state, prior_state)
        elif (
            prior_state not in self.allowed_transitions
            or proposed_state not in self.allowed_transitions[prior_state]
        ):
            raise WorkflowTransitionError(self.policy, proposed_state, prior_state)

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
        match.
        """
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


def _type_uri(resource_type):
    return getattr(resource_type, "type_uri", resource_type)


def default_workflow_policy_for(resource_type):
    """The settings default policy for `resource_type` (a `ResourceType` or its
    type URI), from ``SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]``, or ``None``."""
    from scrud_django.conf import default_workflow_policies

    return default_workflow_policies().get(_type_uri(resource_type))


def workflow_policy_for(resource_type, context):
    """The `WorkflowPolicy` in effect for `resource_type` (a `ResourceType` or its
    type URI) in `context`, or ``None`` when the type has no workflow there.

    It is the stored ``WorkflowPolicy`` resource for the type whose own contexts
    (read with `rbac_contexts_of`, so a policy that names no ``rbac_context`` is
    in the default context ``""``) include `context`; when several do, the most
    recently modified one wins. Without one, it is the settings default for the
    type (`default_workflow_policy_for`). A policy stored in one context never
    applies in another.

    The one lookup of a policy: the `WorkflowValidator` and
    ``ResourceMixin.get_workflow_policy`` both use it.
    """
    from scrud_django.mixins.resource_mixin import rbac_contexts_of
    from scrud_django.models import Resource

    type_uri = _type_uri(resource_type)
    stored = Resource.objects.filter(
        resource_type__type_uri=WORKFLOW_POLICY_TYPE_URI,
        content__resource_type=type_uri,
    ).order_by("-modified_at", "-pk")
    for policy in stored:
        if context in rbac_contexts_of(policy.content):
            return WorkflowPolicy(policy.content)
    return default_workflow_policy_for(type_uri)
