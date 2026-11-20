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
from typing import NamedTuple

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Model
from django.db.models.signals import post_save, pre_save

from scrud_django.core.decisions import (
    ContextDecision,
    NullContent,
    WorkflowDecision,
    concerned_policies,
    context_transition,
    is_entering,
    snapshot_prior,
)
from scrud_django.workflow import (
    LEAVE,
    TransitionData,
    WorkflowAmbiguousStateError,
    WorkflowInvalidInitialStateError,
    WorkflowInvalidStateError,
    WorkflowTransitionError,
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


# Deprecated: the earlier name of `WorkflowDecision`, which
# `transitions_between` returns (no longer a ``NamedTuple``: it neither
# unpacks nor has ``_replace``). Kept importable for a later release.
Transitions = WorkflowDecision


def transition_in(
    policy, prior, proposed, *, entering: bool = False, leaving: bool = False
):
    """``(TransitionData or None, workflow error or None)``: a save of
    `proposed` under the `policy` (a workflow) in a context the content
    `entering` moves into, `leaving` moves out of, or else stays in, from
    `prior` (``None``: a create; `NULL_CONTENT`: stored content that is JSON
    null, in no state), as `scrud_django.core.decisions.context_transition`
    computes it. An invalid transition, a create (or move in) whose content
    is in no initial state (``WorkflowInvalidInitialStateError``) and content
    that meets the conditions of more than one state
    (``WorkflowAmbiguousStateError``) are reported as ``None`` with the
    error, never raised."""
    try:
        if leaving:
            prior_state = policy.state(prior)
            return TransitionData(None, prior_state, LEAVE), None
        return context_transition(policy, prior, proposed, entering=entering), None
    except WORKFLOW_ERRORS as error:
        # Workflow validation is the workflow check's: the error is
        # reported, not raised.
        return None, error


def transitions_between(
    type_iri: str,
    *,
    prior: Mapping | NullContent | None,
    proposed: Mapping,
    contexts_before: Sequence[str],
    contexts_after: Sequence[str | None],
    stored_pk: object | None = None,
    actor: object | None = None,
) -> WorkflowDecision:
    """The `WorkflowDecision` (``checked=False``) of a save of a `type_iri`
    that skipped the workflow check, which ``scrud_post_save`` reports, by
    `actor` (``None``: unknown) of the stored resource `stored_pk`
    (``None``: a create) from `prior` (the stored content; ``None``: a
    create, or a row not on the database written to; `NULL_CONTENT`: stored
    content that is JSON null, an update from no state) to `proposed`, in
    the contexts `contexts_before` (the stored content's) and
    `contexts_after` (the proposed content's; none is ``[None]``, the
    settings default):

    - ``transition_data``, and ``transition_error``, the workflow error that
      made it ``None`` (an invalid transition, or content that meets the
      conditions of more than one state, which a write that skipped the
      workflow check still saves), else ``None``;
    - ``transitions``, a read-only mapping (``MappingProxyType``): for each
      context the stored content or the proposed content is in (stored ones
      first, as the workflow check checks them) where a policy applies
      (`scrud_django.core.decisions.concerned_policies`), the write there.
      (A ``None`` context, which callers pass in `contexts_after` when the
      content is in none, is reported with the settings default, as the
      workflow check checks it: for a resource it is defensive; an own
      model's ``contexts_for`` may give none.) A context it stays in: the
      transition under its policy (``None`` if invalid); one it enters (or
      a create's): its ``__INITIALIZE__`` (``None`` if the content is in no
      initial state there); one it leaves: ``TransitionData(None, <the
      state it was in there>, "__LEAVE__")`` (`LEAVE`); ``None`` in any of
      them where the state is ambiguous. A context without a policy is
      left out;
    - ``errors``, context -> the workflow error that made its
      ``transitions`` entry ``None``.

    Each context is computed as the workflow check computes it
    (`transition_in`, `scrud_django.core.decisions.is_entering`), so that a
    checked and an unchecked write of the same content decide alike (but
    for a context it leaves, until then). Each context's policy is read
    once."""
    stored_contexts = list(contexts_before)
    contexts = list(contexts_after) or [None]
    entries = []
    for context, policy in concerned_policies(
        type_iri, contexts_before=stored_contexts, contexts_after=contexts
    ):
        entering = is_entering(context, prior=prior, contexts_before=stored_contexts)
        data, error = transition_in(
            policy,
            prior,
            proposed,
            entering=entering,
            leaving=context not in contexts,
        )
        entries.append(
            ContextDecision(
                context=context,
                policy=policy.reference,
                transition=data,
                error=error,
                entering=entering,
            )
        )
    return WorkflowDecision(
        type_iri=type_iri,
        stored_pk=stored_pk,
        actor=actor,
        checked=False,
        prior=snapshot_prior(prior),
        proposed=proposed,
        contexts_after=contexts,
        contexts=entries,
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

# The attribute a governed instance's pre-save stashes ``(iri,
# WorkflowDecision)`` under for its post-save.
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
    as a resource's is; an instance in no context (`contexts_for` gives
    none) is checked under the settings default, in context ``None``, as
    the workflow check applies it. Its pre-save and post-save receivers are
    connected once per model. `state_of` and `contexts_for` must not raise:
    an exception fails the save, as a resource's transition computation
    does.

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
    stash its unchecked `WorkflowDecision` (`transitions_between`) on the
    instance, from the stored row (re-read on the database the save writes
    to, `using`; a create or a row not there has no prior state and no
    contexts) to the instance. A raw save (``loaddata``) is not logged, as
    a resource's is not."""
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
        # In no context: the settings default under None, as the workflow
        # check applies it (as for a resource).
        contexts_after=list(governed.contexts_for(instance)) or [None],
        stored_pk=None if stored is None else stored.pk,
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
