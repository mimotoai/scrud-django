"""The workflow decision types and the per-context computation every
workflow check shares: which contexts a write concerns under which policies
(`concerned_policies`), whether it enters one (`is_entering`), and its
transition there (`context_transition`).

A `WorkflowDecision` is what a workflow check decided about one write, as
immutable data: it holds snapshots of the stored and proposed content, so that
it describes the write as checked however the caller changes them later. This
module imports only `scrud_django.workflow`, `scrud_django.exceptions` and the
standard library: `core.workflow` and `core.transitions` import it.
"""

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from scrud_django.exceptions import (
    DefaultPolicyReference,
    StoredPolicyReference,
    WorkflowTransitionError,
)
from scrud_django.workflow import (
    TransitionData,
    WorkflowPolicy,
    default_workflow_policy_for,
    workflow_policies_for,
)


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


def decision_key(context: str | None) -> tuple:
    """The key of `context` in a decision's entries. The one place a region
    will be added to it."""
    return (context,)


@dataclass(frozen=True)
class ContextDecision:
    """What a workflow check decided in one context: the `transition` under
    `policy` (`WorkflowPolicy.reference`), or the workflow `error` that made
    it invalid, never both. `entering` says the write moves the resource
    into the context (a create enters every context), so that `transition`
    is an ``__INITIALIZE__``."""

    context: str | None
    policy: StoredPolicyReference | DefaultPolicyReference
    transition: TransitionData | None
    error: WorkflowTransitionError | None
    entering: bool

    def __post_init__(self):
        if (self.transition is None) == (self.error is None):
            raise ValueError(
                "A ContextDecision has a transition or an error, exactly one: "
                f"got transition={self.transition!r}, error={self.error!r}."
            )


@dataclass(frozen=True)
class WorkflowDecision:
    """What a workflow check decided about a write of the type `type_iri`
    of the stored resource `stored_pk` (``None``: a create) by `actor`:
    `checked` says the workflow check ran. `prior` and `proposed` are
    snapshots (deep copies) of the stored and proposed content;
    `contexts_after` are the contexts the proposed content names, as its
    builder gives them (`normalized_contexts_after`: ``(None,)``, the
    settings default, for a write in no context at all), and `contexts` the
    decision in each context concerned."""

    type_iri: str
    stored_pk: object | None
    actor: object | None
    checked: bool
    prior: Mapping | None
    proposed: Mapping
    contexts_after: tuple[str | None, ...]
    contexts: tuple[ContextDecision, ...]

    def __post_init__(self):
        object.__setattr__(self, "prior", copy.deepcopy(self.prior))
        object.__setattr__(self, "proposed", copy.deepcopy(self.proposed))
        object.__setattr__(self, "contexts_after", tuple(self.contexts_after))
        object.__setattr__(self, "contexts", tuple(self.contexts))

    def get(self, context: str | None) -> ContextDecision | None:
        """The decision in `context`, ``None`` where none applies."""
        key = decision_key(context)
        for entry in self.contexts:
            if decision_key(entry.context) == key:
                return entry
        return None

    @property
    def transitions(self) -> Mapping[str | None, TransitionData | None]:
        """Context -> its transition (``None``: invalid there), for each
        context with a decision; a read-only view."""
        return MappingProxyType({e.context: e.transition for e in self.contexts})

    @property
    def errors(self) -> Mapping[str | None, WorkflowTransitionError]:
        """Context -> the workflow error that made it invalid, for each such
        context; a read-only view."""
        return MappingProxyType(
            {e.context: e.error for e in self.contexts if e.error is not None}
        )

    def _first_entry(self) -> ContextDecision | None:
        for context in self.contexts_after:
            entry = self.get(context)
            if entry is not None:
                return entry
        return None

    @property
    def transition_data(self) -> TransitionData | None:
        """The transition in the first of `contexts_after` that has a
        decision; ``None`` if there is none or it is invalid."""
        entry = self._first_entry()
        return None if entry is None else entry.transition

    @property
    def transition_error(self) -> WorkflowTransitionError | None:
        """The workflow error in the first of `contexts_after` that has a
        decision, ``None`` if there is none or it is valid."""
        entry = self._first_entry()
        return None if entry is None else entry.error


def normalized_contexts_after(
    contexts_before: Sequence[str | None], contexts_after: Sequence[str | None]
) -> tuple[str | None, ...]:
    """`contexts_after` as a decision holds them: ``(None,)``, the settings
    default, when the write is in no context at all (neither
    `contexts_before` nor `contexts_after` names one), as the workflow check
    decides it; else `contexts_after` itself, as a tuple (an update out of
    every context it was in concerns those contexts alone)."""
    after = tuple(contexts_after)
    if not after and not list(contexts_before):
        return (None,)
    return after


def concerned_policies(
    type_iri: str,
    *,
    contexts_before: Sequence[str],
    contexts_after: Sequence[str | None],
) -> list[tuple[str | None, WorkflowPolicy]]:
    """``(context, policy)`` for each context a write concerns that has a
    policy: `contexts_before` (the stored content's), then `contexts_after`
    (the proposed content's, `normalized_contexts_after`), duplicates
    dropped: a write in no context at all concerns ``None``, the settings
    default. The stored policies of the named contexts are read in one
    query."""
    after = normalized_contexts_after(contexts_before, contexts_after)
    concerned = list(dict.fromkeys([*contexts_before, *after]))
    named = [context for context in concerned if context is not None]
    found = dict(workflow_policies_for(type_iri, named)) if named else {}
    pairs = [
        (
            context,
            default_workflow_policy_for(type_iri)
            if context is None
            else found[context],
        )
        for context in concerned
    ]
    return [(context, policy) for context, policy in pairs if policy is not None]


def is_entering(
    context: str | None, *, prior: object, contexts_before: Sequence[str]
) -> bool:
    """Whether a write moves the resource into `context`: a create (no
    `prior`) enters every context, an update only a named one the stored
    content is not in."""
    return prior is None or (context is not None and context not in contexts_before)


def context_transition(
    policy: WorkflowPolicy,
    prior: Mapping | NullContent | None,
    proposed: Mapping,
    *,
    entering: bool,
) -> TransitionData:
    """The `TransitionData` of writing `proposed` under `policy`: a create's
    ``__INITIALIZE__`` when there is no `prior` or the write is `entering`
    the context, else the transition from `prior`. Raises the workflow
    errors; the caller decides what to do with them."""
    if prior is None or entering:
        return policy.initial_transition(proposed)
    return policy.determine_transition(proposed, prior)


def snapshot_prior(prior: Mapping | NullContent | None) -> Mapping | None:
    """`prior` as a decision's: ``None`` for ``None`` and `NULL_CONTENT`."""
    return None if prior is None or isinstance(prior, NullContent) else prior
