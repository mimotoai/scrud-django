from dataclasses import dataclass


@dataclass
class TransitionCheck:
    """The workflow transition a write makes in one context, as the
    `WorkflowValidator` authorizes it: its name, or ``None`` where no workflow
    policy applies (there is no transition to authorize)."""

    transition: str | None


def authorized_transitions(resource, configuration):
    """The ``scrud_workflow`` operator: allows a `TransitionCheck` whose
    transition is in the configuration's ``authorized_transitions``, and
    denies every other one, including the ``None`` transition the
    `WorkflowValidator` checks where no workflow policy applies, so a grant
    conditioned on it allows no write there.

    Anything else (the request body of scoped-rbac's request check, the
    stored resource of its object check) is allowed: the transition is only
    known to the `WorkflowValidator`, which authorizes it in every context of
    the write; otherwise the workflow validation would run twice.

    To use this `scoped_rbac.policy.Operator`, define an operator condition that
    specifies the `authorized_transitions` property. Here's an example: ```json {
    "condition": {"operator": "scrud_workflow", "authorized_transitions": ["start"]} }
    ```

    The name for the initial transition is `__INITIALIZE__`, and for an update
    that stays in the same state `__SAME_STATE__`.
    """
    if isinstance(resource, TransitionCheck):
        return resource.transition is not None and resource.transition in (
            configuration.get("authorized_transitions") or []
        )
    return True
