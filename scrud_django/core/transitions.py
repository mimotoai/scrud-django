"""The workflow transitions ``scrud_post_save`` reports, on plain mappings and
a type IRI: `ResourceMixin` (a ``Resource``) and an app's own models both
compute them here, so a resource and an own model report the same way.
"""

import logging
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import NamedTuple

from scrud_django.workflow import (
    LEAVE,
    TransitionData,
    WorkflowAmbiguousStateError,
    WorkflowInvalidInitialStateError,
    WorkflowInvalidStateError,
    WorkflowTransitionError,
    default_workflow_policy_for,
    workflow_policies_for,
)

# The workflow errors of saves that skipped the ``WorkflowValidator``.
logger = logging.getLogger("scrud_django.workflow")

# The workflow errors a report of a transition catches: a write that skipped
# the ``WorkflowValidator`` is still saved.
WORKFLOW_ERRORS = (
    WorkflowAmbiguousStateError,
    WorkflowInvalidInitialStateError,
    WorkflowInvalidStateError,
    WorkflowTransitionError,
)


class Transitions(NamedTuple):
    """A save's workflow transitions (`transitions_between`):
    ``scrud_post_save`` sends the first three as its keyword arguments of
    the same names; the save logs `errors` (`log_skipped_check`). Its
    mappings are read-only views (``MappingProxyType``): the same one reaches
    every receiver, and none can change what the others are given."""

    # Under the policy of the first context the content names that has one;
    # ``None`` when no policy applies or the transition is invalid there.
    transition_data: TransitionData | None
    # The workflow error that made `transition_data` ``None``, else ``None``.
    transition_error: WorkflowTransitionError | None
    # Context -> `TransitionData` (``None``: invalid there), for every
    # context the stored resource or the content is in under a policy.
    transitions: Mapping[str | None, TransitionData | None]
    # Context -> the workflow error that made its `transitions` entry
    # ``None``, for each such context (not sent with the signal: the save
    # logs them).
    errors: Mapping[str | None, WorkflowTransitionError]


def transition_in(
    policy, prior, proposed, *, entering: bool = False, leaving: bool = False
):
    """``(TransitionData or None, workflow error or None)``: a save of
    `proposed` under the `policy` (a workflow) in a context the content
    `entering` moves into, `leaving` moves out of, or else stays in, from
    `prior` (``None``: a create). An invalid transition, a create (or move in)
    whose content is in no initial state (``WorkflowInvalidInitialStateError``)
    and content that meets the conditions of more than one state
    (``WorkflowAmbiguousStateError``) are reported as ``None`` with the
    error, never raised."""
    try:
        if leaving:
            prior_state = policy.state(prior)
            return TransitionData(None, prior_state, LEAVE), None
        if prior is None or entering:
            # A create has no prior state; its proposed state is the
            # initial state the content is in. Content in none is an
            # invalid create (``WorkflowInvalidInitialStateError``), as
            # the `WorkflowValidator` checks it.
            return policy.initial_transition(proposed), None
        return policy.determine_transition(proposed, prior), None
    except WORKFLOW_ERRORS as error:
        # Workflow validation is the WorkflowValidator's: the error is
        # reported, not raised.
        return None, error


def transitions_between(
    type_iri: str,
    *,
    prior: Mapping | None,
    proposed: Mapping,
    contexts_before: Sequence[str],
    contexts_after: Sequence[str | None],
) -> Transitions:
    """The `Transitions` ``scrud_post_save`` reports for a save of a `type_iri`
    from `prior` (the stored content; ``None``: a create, or a row not on the
    database written to) to `proposed`, in the contexts `contexts_before` (the
    stored content's) and `contexts_after` (the proposed content's):

    - ``transition_data``, and ``transition_error``, the workflow error that
      made it ``None`` (an invalid transition, or content that meets the
      conditions of more than one state, which a write that skipped the
      ``WorkflowValidator`` still saves), else ``None``;
    - ``transitions``, a read-only mapping (``MappingProxyType``): for each
      context the stored content or the proposed content is in (stored ones
      first, as the ``WorkflowValidator`` checks them) where a policy applies
      (`workflow_policies_for`), the write there. (A ``None`` context, which
      `contexts_after` has only when the content names none, is defensive: it
      is reported with the settings default, as the ``WorkflowValidator``
      checks it.) A context it stays in: the transition under its policy
      (``None`` if invalid); one it enters (or a create's): its
      ``__INITIALIZE__`` (``None`` if the content is in no initial state
      there); one it leaves: ``TransitionData(None, <the state it was in
      there>, "__LEAVE__")`` (`LEAVE`); ``None`` in any of them where the
      state is ambiguous. A context without a policy is left out.

    Each context's policy is read once."""
    stored_contexts = list(contexts_before)
    contexts = list(contexts_after)
    concerned = list(dict.fromkeys(stored_contexts + contexts))
    named = [context for context in concerned if context is not None]
    # The stored policies of every named context, read in one query.
    found = dict(workflow_policies_for(type_iri, named)) if named else {}
    policies = {
        context: (
            default_workflow_policy_for(type_iri) if context is None else found[context]
        )
        for context in concerned
    }
    reported = {
        context: transition_in(
            policy,
            prior,
            proposed,
            entering=context is not None and context not in stored_contexts,
            leaving=context not in contexts,
        )
        for context, policy in policies.items()
        if policy is not None
    }
    # transition_data's context: the first the content names with a policy.
    picked = next((c for c in contexts if policies[c] is not None), None)
    transition_data, transition_error = reported.get(picked, (None, None))
    return Transitions(
        transition_data=transition_data,
        transition_error=transition_error,
        transitions=MappingProxyType(
            {context: data for context, (data, _) in reported.items()}
        ),
        errors=MappingProxyType(
            {
                context: error
                for context, (_, error) in reported.items()
                if error is not None
            }
        ),
    )


def log_skipped_check(errors: Mapping, *, subject: str, type_iri: str) -> None:
    """Log each workflow error of `errors` (context -> error, from
    `transitions_between`) as a WARNING on the ``scrud_django.workflow``
    logger. An API write passed the ``WorkflowValidator`` and has none; a save
    that skipped it (the admin, server code) would otherwise be invalid
    silently in every context but the one ``transition_error`` reports.
    `subject` names what was saved ("resource pk 7")."""
    for context, error in errors.items():
        logger.warning(
            "Saving %s of %s skipped the workflow check, which "
            "it fails in context %r (policy %r): %s (prior state %r, "
            "proposed state %r%s).",
            subject,
            type_iri,
            context,
            error.policy,
            error.message,
            error.prior_state,
            error.proposed_state,
            (
                f", matching states {error.matching_states!r}"
                if isinstance(error, WorkflowAmbiguousStateError)
                else ""
            ),
        )
