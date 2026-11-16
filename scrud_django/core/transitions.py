"""The workflow transitions ``scrud_post_save`` reports, on plain mappings and
a type IRI: `ResourceMixin` (a ``Resource``) and an app's own models both
compute them here, so a resource and an own model report the same way.

An app's own model is governed by a workflow once it is recorded with
`govern` (``scrud_django.ninja.workflow_check`` does it): a save of it that
skips the workflow check, any save but the toolkit's (`checked_save`), is
logged as a resource's is (`log_skipped_check`), not refused.
"""

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import NamedTuple

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Model
from django.db.models.signals import post_save, pre_save

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


class Governed(NamedTuple):
    """A governed model's entry in the registry (`govern`)."""

    # The model's type IRI: its policies' ``resource_type``.
    iri: str
    # The content its workflow reads, of an instance.
    state_of: Callable[[Model], Mapping]
    # The contexts an instance is in.
    contexts_for: Callable[[Model], Sequence[str]]


# The governed models: model class -> `Governed`. The receivers `govern`
# connects read it at each save, so a model no longer in it is left alone.
_GOVERNED: dict[type[Model], Governed] = {}

# The models whose saves are the toolkit's own (`checked_save`) in this
# context (thread, task).
_CHECKED: ContextVar[frozenset[type[Model]]] = ContextVar(
    "scrud_django_checked_saves", default=frozenset()
)

# The attribute a governed instance's pre-save stashes ``(iri, Transitions)``
# under for its post-save.
_PENDING = "_scrud_governed_transitions"


def govern(
    model: type[Model],
    iri: str,
    *,
    state_of: Callable[[Model], Mapping],
    contexts_for: Callable[[Model], Sequence[str]],
) -> None:
    """Record `model` (an app's own model, not a resource) as governed by
    the workflow policies of the type `iri`: the content they read of an
    instance is `state_of` it, its contexts `contexts_for` it. A save of it
    outside `checked_save` (the admin, server code) skipped the workflow
    check: it is saved, and each context where it is invalid is logged as a
    WARNING on the ``scrud_django.workflow`` logger (`log_skipped_check`),
    as a resource's is. Its pre-save and post-save receivers are connected
    once per model.

    Governing the same model under the same `iri` again (a URLconf imported
    twice) replaces `state_of` and `contexts_for`; under another IRI it is
    ``ImproperlyConfigured``: a model has one type."""
    governed = _GOVERNED.get(model)
    if governed is not None and governed.iri != iri:
        raise ImproperlyConfigured(
            f"{model._meta.label} is governed as {governed.iri!r}; it cannot "
            f"also be governed as {iri!r}."
        )
    _GOVERNED[model] = Governed(iri, state_of, contexts_for)
    uid = f"scrud_django.governed.{model._meta.label}"
    pre_save.connect(_governed_pre_save, sender=model, dispatch_uid=uid)
    post_save.connect(_governed_post_save, sender=model, dispatch_uid=uid)


def governed_type_iris() -> frozenset[str]:
    """The type IRIs of the governed models (`govern`)."""
    return frozenset(governed.iri for governed in _GOVERNED.values())


@contextmanager
def checked_save(model: type[Model]) -> Iterator[None]:
    """While open, the saves of `model` (that class only: a save of another
    governed model it makes is still logged) are the toolkit's, which
    checked them against the workflow first: none is logged."""
    token = _CHECKED.set(_CHECKED.get() | {model})
    try:
        yield
    finally:
        _CHECKED.reset(token)


def _governed_pre_save(sender, instance, raw=False, using=None, **kwargs):
    """Before a save of a governed model that skipped the workflow check:
    stash its `Transitions` on the instance, from the stored row (re-read on
    the database the save writes to, `using`; a create or a row not there
    has no prior state and no contexts) to the instance. A raw save
    (``loaddata``) is not logged, as a resource's is not."""
    # One a save that raised before its post-save left behind is dropped.
    instance.__dict__.pop(_PENDING, None)
    governed = _GOVERNED.get(sender)
    if governed is None or raw or sender in _CHECKED.get():
        return
    stored = (
        None
        if instance._state.adding
        else sender._base_manager.using(using).filter(pk=instance.pk).first()
    )
    found = transitions_between(
        governed.iri,
        prior=None if stored is None else governed.state_of(stored),
        proposed=governed.state_of(instance),
        contexts_before=[] if stored is None else governed.contexts_for(stored),
        contexts_after=governed.contexts_for(instance),
    )
    setattr(instance, _PENDING, (governed.iri, found))


def _governed_post_save(sender, instance, **kwargs):
    """Once the row is written: log the workflow errors its pre-save found
    (`log_skipped_check`)."""
    pending = instance.__dict__.pop(_PENDING, None)
    if pending is None:
        return
    iri, found = pending
    log_skipped_check(
        found.errors, subject=f"{sender._meta.label} pk {instance.pk}", type_iri=iri
    )
