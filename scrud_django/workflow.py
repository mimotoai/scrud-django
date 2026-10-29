import logging
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

logger = logging.getLogger(__name__)

CEL_ENVIRONMENT = new_environment({})


@dataclass
class TransitionData:
    proposed_state: str
    prior_state: str
    transition: str


class WorkflowPolicy:
    """Currently assuming one workflow policy in effect per resource type per access
    control context. In this scheme... moving a resource to a new access control
    context will require the user to be authorized in both the source and target
    contexts and for the proposed state to be a valid state in the workflow policy
    associated with the target context.
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
            transition_from = as_list(transition_policy["from"])
            transition_to = as_list(transition_policy["to"])
            for source, target in product(transition_from, transition_to):
                source_transitions = self.allowed_transitions.setdefault(source, {})
                source_transitions[target] = transition_name
