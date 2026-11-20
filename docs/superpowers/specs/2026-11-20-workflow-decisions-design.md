# Workflow decisions: one decision, from check to save to actions

Epic #58 (milestone "Workflow decisions"): #37, then #36 with #22, then #31,
then #20. Written 2026-11-20, against `develop` at c012238 (0.17.0).

## Intent

Today a write's workflow decision is made twice and seen by no one whole.
The check (`scrud_django.core.workflow.workflow_check`) computes one
`TransitionData` per context, authorizes it, and throws it away;
`Resource.save()` recomputes it (`ResourceMixin.get_transitions` →
`core.transitions.transitions_between`), re-reading the stored row and every
policy, and reports something that can differ from what was authorized: a
context the write leaves is authorized as an ordinary transition but
reported as `__LEAVE__` (#36), and a policy edited between check and save
changes the report. Signals and actions get no actor and no prior content
(#20), and superusers cannot repair content the policy refuses (#31).

After this epic, the check computes one immutable **decision**, the save
sends that same decision, and every consumer (the `scrud_post_save`
signal, `SCRUD_WORKFLOW_ACTIONS`, the log) sees the transition actually
authorized in each context, whether that context is being left, who wrote,
and whether a superuser override applied. Resources and apps' own models
(the ninja toolkit) report identically, and ninja and DRF answer
identically.

## Goals

- #37: one immutable `WorkflowDecision` per checked write, carried from the
  check to the save and sent with `scrud_post_save`. Saves without a check
  compute one the same way, with no actor.
- #36 (owner's option (b)): a context the write leaves keeps today's
  authorization (an ordinary transition from the stored content, checked
  under that context's policy) and is reported as that transition with
  `leaving=True`. `__LEAVE__` is no longer reported anywhere.
- #22: `TransitionData` enforces each kind's field rules.
- #31: active superusers bypass the workflow policy where it would refuse;
  the bypass is logged at WARNING and reported as `__OVERRIDE__`.
- #20: `on_transition` actions keyed by transition name; `user` and
  `prior_content` in the signal kwargs and action payload; an opt-in
  transactional outbox shipped as an optional app.

## Non-goals

- Named regions (#40). The decision is shaped so they can be added
  (see "Regions later"), nothing more.
- Refusing unchecked saves (#56): they stay log-only.
- Workflow checks on DELETE (follow-up `delete-no-workflow-check`).
- Fixing `core-save-checked-mark-body` (the mark names the write, not the
  body). The decision's snapshot of the proposed content makes that fix
  easy later; note it in that follow-up.
- `workflow-grant-blocks-leaving` (#4): unchanged.

## Anti-goals (failure even if tests pass)

- Any outcome that depends on JSON object key order. PostgreSQL `jsonb` and
  MySQL JSON reorder keys; MariaDB stores text. Contexts come from the
  `rbac_context` **array** (order kept everywhere); anything keyed by a
  policy's object keys must be sorted.
- ninja and DRF diverging: `tests/test_front_end_parity.py` stays green.
- Domain logic in a view or adapter. It lives in `scrud_django/core/`,
  which imports nothing from DRF (`tests/test_core_imports.py`).
- A reported transition that differs from the one authorized for the same
  save.
- Editing scoped-rbac, scrudful or safetydance. Gaps go to
  `docs/follow-ups.md`.

## Constraints

- No version bump. Release notes under `## Unreleased` in
  `docs/release-notes.md` (create the heading above `## 0.17.0`). #36 and
  #31 go under **Breaking changes** (what signals and actions receive
  changes).
- SQLite, PostgreSQL, MySQL and MariaDB.
- One feature branch per step, off the previous step's branch, as stacked
  MRs (each MR targets the previous step's branch; GitLab retargets to
  `develop` as each merges): `feature/workflow-decision` (#37),
  `feature/workflow-leaving` (#36 + #22),
  `feature/workflow-superuser-override` (#31),
  `feature/workflow-transition-actions` (#20). Never merge without the
  owner.
- Test idiom per CLAUDE.md (safetydance `Given/When/Then`, steps only in
  `tests/steps.py`). Never weaken an assertion; a test whose behaviour
  changes gets explicit new assertions and the commit says why.

## Owner decisions

- #36 is option (b), as above.
- `LEAVE` is **retired**: never reported. Unchecked saves report a left
  context exactly as the check would compute it: the transition from the
  stored content to the new content under that context's policy, with
  `leaving=True`; invalid (including stored content in no state there) is
  `None` with the error, logged as today. The constant `LEAVE` stays
  importable and in `RESERVED_TRANSITIONS`, documented as no longer
  reported, for removal in a later release.
- The override kind is `__OVERRIDE__` (constant `OVERRIDE` in
  `scrud_django.workflow`, added to `RESERVED_TRANSITIONS`).
- A superuser's write is an override **only in the contexts where the
  policy would refuse it**. Where it is valid, it is reported as its real
  transition (so `on_transition["submit"]` fires) and nothing is logged.
- Overlapping states are a feature, to be supported deterministically.
- Unchecked saves stay log-only (#56).
- The opt-in outbox is **in scope** for #20.

## The design

### The decision object (#37)

In a new neutral module, `scrud_django/core/decisions.py` (imports nothing
from DRF; `scrud_django.workflow` re-exports nothing from it):

```python
@dataclass(frozen=True)
class ContextDecision:
    context: str | None          # None: the resource is in no context
    policy: PolicyReference      # StoredPolicyReference / DefaultPolicyReference
    transition: TransitionData | None   # None only for an invalid unchecked save
    error: WorkflowTransitionError | None  # what made `transition` None
    entering: bool               # the write moves the resource into it
    # leaving is carried by transition.leaving (one source of truth)

    @property
    def override(self) -> bool:  # transition.transition == OVERRIDE

@dataclass(frozen=True)
class WorkflowDecision:
    type_iri: str                # the type checked (the resource type's URI / own model IRI)
    stored_pk: object | None     # None for a create
    actor: object | None         # the user; None when unknown
    checked: bool                # True: made by workflow_check; False: computed for a save that skipped it
    prior: Mapping | None        # snapshot (deep copy) of the stored content; None for a create
    proposed: Mapping            # snapshot (deep copy) of the content checked
    contexts: tuple[ContextDecision, ...]
```

- **Order of `contexts`:** the stored content's contexts first, then the new
  content's, each in its `rbac_context` array order, duplicates dropped:
  the order the check already uses. Only contexts with a policy appear
  (a context without one has no transition).
- **Lookup:** `decision.get(context)` → `ContextDecision | None`. Internally
  entries are keyed by a key tuple built by one function, so #40 can add a
  region to the key (see "Regions later") without changing callers that
  look up by context.
- **Compatibility views**, so `WorkflowDecision` replaces the
  `Transitions` named tuple everywhere (`transitions_between` returns one):
  - `transitions`: read-only `MappingProxyType` of context → `TransitionData
    | None`, as today;
  - `transition_data` / `transition_error`: the first context the **new**
    content names that has a policy, as today;
  - `errors`: read-only mapping of context → error, as today;
  - `overrides`: the contexts whose transition is `__OVERRIDE__` (empty until
    #31).
  `Transitions` (re-exported from `mixins/resource_mixin.py`) stays
  importable as an alias of `WorkflowDecision`, documented as deprecated.
  It is no longer a `NamedTuple` (no unpacking, no `_replace`): listed under
  "Changed" in the release notes. `ResourceMixin.get_transitions` and
  `get_transition_data` remain, and are used only by saves with no matching
  decision: overriding them no longer changes what a checked save sends
  (also under "Changed").
- **Snapshots:** `prior` and `proposed` are deep copies taken when the
  decision is built, so a later in-place change to the body or the stored
  row cannot change what was decided. Receivers must not mutate them
  (documented; not enforced with a deep proxy).
- **Actor:** for a checked write, `access.request_user(request)` (the user
  scoped-rbac authorized; ninja's `resolve_user` and DRF both set
  `request.user`) when authenticated, else `None`. For an unchecked save,
  the user set with `acting_as` (below), else `None`.

### Producing it

- `workflow_check` (core/workflow.py) builds and **returns** the
  `WorkflowDecision` (today it returns a list of `(context, policy,
  TransitionData)`). `validate_write` returns it too. `ninja/workflow.py`,
  `drf/validators.py` and the tests that read the old list are updated.
  This is a change to a core API, noted under "Changed" in the release
  notes.
- `transitions_between` (core/transitions.py) builds a `WorkflowDecision`
  with `checked=False` from the same per-context computation, so a checked
  and an unchecked write of the same content produce equal `contexts`.
  The per-context computation is one function used by both (today
  `transition_for` in core/workflow.py and `transition_in` in
  core/transitions.py duplicate it; merge them).

### Carrying it to the save

- The workflow mark (`core/authz.py`, `_mark_workflow_authorized`) carries
  the decision: the mark's identity stays `(type_iri, stored_pk)` (the
  post-save check's type IRI, which may differ from `decision.type_iri`),
  and the decision rides with it.
- `core.writes.save_checked` reads the decision from the mark before the
  save, and runs the save inside a context variable,
  `core.decisions.applying(decision)`. It is cleared however the save ends.
- `Resource.save()` (`ResourceMixin.save`) takes the pending decision when
  it **matches this save**: `self.resource_type.type_uri ==
  decision.type_iri`, the pk matches (`None`/adding for a create), and
  `self.content == decision.proposed`. A match is consumed (a second save in
  the same block, such as an `after_write` hook re-saving, computes its
  own). No match, or no pending decision: compute as today
  (`transitions_between`, `checked=False`) and log its errors.
- Own models (builder's call, recorded): content equality cannot be the
  test, because a create's checked state has no primary key (`ArticleOut.id`
  is `None` when checked) and database-set fields differ after the save.
  `ModelWorkflow.save_checked` keeps the decision `check_write` returned
  and, after the save, recomputes the per-context transitions of
  `state(obj)` as today (own models already pay this second evaluation;
  this is no regression). If every context's `TransitionData` equals the
  decision's (a context the decision overrode counts as equal when the
  recompute errors there), it sends the checked decision. Otherwise it
  sends the recomputed one (`checked=False`) and logs its errors as today:
  what was saved is not what was authorized.
- The governed pre/post-save receivers (core/transitions.py) build
  `checked=False` decisions for logging only. An own model's save outside
  the toolkit still sends **no** signal (unchanged; builder's call: sending
  one would start running actions and outbox events for admin saves, which
  nobody asked for).
- A resource whose stored content is JSON `null` (only before 0.12.0) is
  decided as the check decides it today, by both paths: an update from no
  state in the default context (`transitions_between` stops treating it as
  a create). `prior` is `None` for it, as for a create; receivers tell them
  apart by `action`.
- `entering` is `True` for every context of a create (the resource enters
  each), in both paths, so checked and unchecked decisions are equal.

### Sending it

`scrud_post_save` gains the keyword argument `decision` (the
`WorkflowDecision`). `transition_data`, `transition_error` and `transitions`
are still sent, now taken from the decision. #20 adds `user` and
`prior_content` (below). Adding keyword arguments is not breaking
(receivers take `**kwargs`).

### Leaving a context (#36) and per-kind rules (#22)

- `TransitionData` gains `leaving: bool = False` (keyword-only, last field,
  so positional construction keeps working).
- In a context the stored content is in and the new content is not, the
  check and `transitions_between` both compute `determine_transition(new,
  stored)` and mark it `leaving=True`. `transition_in(..., leaving=True)`
  no longer returns `LEAVE`.
- `TransitionData.__post_init__` raises `ValueError` unless:
  - `transition` is a `str`;
  - `__INITIALIZE__`: `prior_state is None`, `proposed_state is not None`,
    not `leaving`;
  - `__SAME_STATE__`: `prior_state == proposed_state`, both not `None`;
  - `__OVERRIDE__`: any states (each `None` when it cannot be told);
  - `__LEAVE__` (retired, never produced): `proposed_state is None`, kept so
    third-party code constructing it still works;
  - any other `__`-prefixed name: refused (reserved);
  - a policy transition name: both states not `None`.
- README "Workflow actions": a left context is reported as the transition
  authorized there with `leaving`; `__LEAVE__` is no longer reported.

### Superuser override (#31)

- A superuser is `user.is_active and user.is_superuser`, decided by one
  neutral helper in core/authz.py (scoped-rbac decides without
  `is_active`; record that difference in `docs/follow-ups.md`).
- In `workflow_check`, per context with a policy: compute the transition.
  If that raises a workflow state error (`WorkflowInvalidInitialStateError`,
  `WorkflowInvalidStateError`, `WorkflowAmbiguousStateError`,
  `WorkflowTransitionError` from the policy, including `restrict_update`)
  and the user is a superuser, the context's transition is
  `TransitionData(proposed, prior, OVERRIDE, leaving=...)`, each state the
  one state the content is in, or `None` when it is in none or several.
  Entering, staying and leaving contexts are all covered.
- Authorization still runs: scoped-rbac already allows superusers, and the
  type's own hook checks (`narrow`) are still asked, with
  `current_transition` `"__OVERRIDE__"`. They are the type's rules, not the
  policy's (the owner may revisit this when reviewing the design).
- JSON Schema validation and RBAC context checks (request, object,
  post-save) are unchanged.
- When an override decision is applied by a save (`Resource.save`,
  `ModelWorkflow.save_checked`), each overridden context is logged at
  WARNING on `scrud_django.workflow`: type IRI, pk, context, policy
  reference, prior and proposed state, the overridden error's message, and
  matching states when ambiguous; before `scrud_post_save` is sent.
- Update the tests that pin superusers being refused (e.g.
  `test_transition_from_no_state_error_names_no_prior_state`, and
  `tests/test_workflow_contexts.py:510-516`): either use a non-superuser to
  keep pinning the refusal, or assert the override; both cases end up
  covered.

### Actions keyed by transition, actor and prior content (#20)

- `SCRUD_WORKFLOW_ACTIONS[type_uri]["on_transition"] = {"submit": fn, ...}`.
  On `scrud_post_save`, for each distinct transition name taken in the
  decision (any context, reserved kinds included, e.g. `"__OVERRIDE__"` or
  `"__INITIALIZE__"`), its action runs once, on commit, after `on_save`,
  in the decision's context order of first appearance. Its kwargs are the
  signal's plus `transition` (the name) and `contexts` (tuple of the
  contexts where it was taken). A `leaving` transition fires like any
  other; its `ContextDecision` is in `decision`.
- System checks: `E008` accepts `on_transition`; a value that is not a
  mapping of `str` to callable is a new error, `scrud_django.E012`, and so
  is a reserved key that can never be taken (any `__` name other than
  `__INITIALIZE__`, `__SAME_STATE__` and `__OVERRIDE__`; `__LEAVE__`
  included). At write time a bad entry runs nothing and is logged, as
  today.
- `scrud_post_save` gains `user` (`decision.actor`) and `prior_content`
  (`decision.prior`; `None` for a create). `scrud_post_delete` gains
  `user` (the acting user, below; `None` when unknown).
- `scrud_django.core.signals.acting_as(user)`: a context manager setting the
  acting user in a context variable. The front ends open it around their
  saves and deletes (`core.writes.save_checked`, `destroy`,
  `ModelWorkflow.save_checked` / `delete_checked`); server code may open it
  to name an actor for unchecked saves.

### Opt-in outbox (#20)

- A new optional app, `scrud_django.outbox` (opt in by adding it to
  `INSTALLED_APPS`), with its own migrations, so projects without it get no
  table.
- Model `OutboxEvent`: `type_uri`, `resource_pk` (text, for own models'
  non-integer keys), `action`, `checked`, `transitions` (a JSON **list**,
  in the decision's order, of `{context, transition, prior_state,
  proposed_state, leaving, error}`; not an object keyed by context, whose
  key order jsonb and MySQL drop, and whose `None` context has no JSON
  key), `user_id` (text, nullable), `content`, `prior_content` (JSON,
  nullable), `created_at`, `sent_at` (nullable).
- Its `AppConfig.ready()` connects a receiver to both signals (fixed
  `dispatch_uid`) that stores one event in the write's transaction on
  `using`, **only for the types listed** in `SCRUD_OUTBOX["TYPES"]` (a list
  of type URIs, or `"__all__"`; owner's decision). Unset or empty records
  nothing, and a new system check warns that the app is installed with no
  types (`scrud_django.W010`). A `TYPES` that is neither is `E013`. The
  suite's default settings install the app with no types, so existing
  query budgets do not move; the outbox tests list types with
  `override_settings`.
- Delivery: `scrud_django.outbox.send_events(deliver, batch=100)` and
  `manage.py scrud_outbox_send` reading `SCRUD_OUTBOX["DELIVER"]` (a dotted
  path to `deliver(event)`, which must be idempotent). Rows are claimed
  with `select_for_update(skip_locked=True)` where supported (PostgreSQL,
  MySQL 8, MariaDB 10.6+); SQLite ignores it. At-least-once delivery.
- The README's hand-written outbox recipe is replaced by documentation of
  the app (or kept short, pointing at it).

## Regions later (#40)

The entry key function returns `(context,)` today. A region would make it
`(context, region)`, with `region` sorted by name (regions come from a
policy's object keys, whose order is not kept). `decision.get(context)`
keeps meaning "the context's single entry" until regions exist;
`on_transition` would then key on `(region, transition)` or a qualified
name. Nothing else needs to change.

## Release notes (Unreleased)

- Breaking: a left context is reported as the transition authorized there
  with `leaving=True`, not `__LEAVE__` (#36); superusers' writes the policy
  would refuse are saved and reported as `__OVERRIDE__` (#31);
  `TransitionData` rejects inconsistent fields (#22).
- Added: `decision`, `user`, `prior_content` signal kwargs;
  `on_transition`; `acting_as`; `scrud_django.outbox` and
  `SCRUD_OUTBOX`; checks `E012`, `E013` and `W010`.
- Changed: `workflow_check` and `validate_write` return a
  `WorkflowDecision`; `transitions_between` returns one.

## Left to the builder

- Exact helper names inside core, the module split of `core/decisions.py`,
  and whether `ContextDecision` holds the policy reference or the compiled
  policy (it must stay immutable from the outside).
- Test placement, following the existing modules
  (`tests/test_core_transitions.py`, `test_workflow_contexts.py`,
  `test_workflow_policy.py`, `test_condition_contexts.py`, the own-model and
  parity suites).
- The outbox's admin registration and indexes.
