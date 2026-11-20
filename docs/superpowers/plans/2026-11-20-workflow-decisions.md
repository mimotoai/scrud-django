# Workflow Decisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One immutable workflow decision per write, made by the check, carried to the save and sent to signals, actions and an opt-in outbox (epic #58: #37 → #36 + #22 → #31 → #20).

**Architecture:** A new neutral module, `scrud_django/core/decisions.py`, holds the decision types, the per-context computation shared by the check and by unchecked saves, and the context variable that hands a checked decision to the save. `workflow_check` returns the decision and stores it with the request's workflow mark. `core.writes.save_checked` opens it around the save, and `Resource.save()` and `ModelWorkflow.save_checked` send it. The later issues extend the same objects: a `leaving` flag, per-kind rules, `__OVERRIDE__`, the actor, `on_transition`, and the outbox app.

**Tech Stack:** Python 3.11-3.13, Django, django-ninja, DRF (the `drf` extra), scoped-rbac, scrudful, safetydance tests, pytest.

**Spec:** `docs/superpowers/specs/2026-11-20-workflow-decisions-design.md` (read it first; this plan argues from it).

## Global Constraints

- Domain logic in `scrud_django/core/`; no DRF imports there (`tests/test_core_imports.py`).
- ninja and DRF answer identically: `tests/test_front_end_parity.py` stays green; HTTP tests take the `front_end` fixture.
- Apps' own models (`core/transitions.py`, `ninja/workflow.py`) get the same reporting.
- Never edit scoped-rbac, scrudful, safetydance(-django); gaps go to `docs/follow-ups.md` (anchor + `## slug: title`).
- No version bump. Release notes go under `## Unreleased`, added above `## 0.17.0` in `docs/release-notes.md`, with sections `### Breaking changes`, `### Added`, `### Changed`. #36 and #31 go under Breaking changes.
- SQLite, PostgreSQL, MySQL and MariaDB. Never rely on JSON object key order; contexts follow the `rbac_context` array order.
- Test conventions per CLAUDE.md: `@pytest.mark.django_db` then `@test_context`; custom steps only in `tests/steps.py`; never weaken an assertion. When behaviour changes, add explicit new assertions and say why in the commit message.
- Never run two pytest suites at once. Never use `while pgrep -f` loops.
- Never run `manage.py` in `demo/` with the demo's settings.
- Branches (each off the previous one), with one stacked MR per branch, targeting the previous branch:
  1. `feature/workflow-decision` (#37)
  2. `feature/workflow-leaving` (#36 + #22)
  3. `feature/workflow-superuser-override` (#31)
  4. `feature/workflow-transition-actions` (#20)
- Never merge without the owner.
- Reserved names: `INITIALIZE = "__INITIALIZE__"`, `SAME_STATE = "__SAME_STATE__"`, `LEAVE = "__LEAVE__"` (retired, never reported), `OVERRIDE = "__OVERRIDE__"`.
- A superuser for the workflow is `user.is_active and user.is_superuser`.

## Review Focus

- A policy edited between the check and the save: the reported transition must be the authorized one. Task 3 asserts that a checked save does not recompute.
- A resource in several contexts, its stored and new lists in different orders: the decision lists the stored contexts first, then the new ones, each in array order, deterministically on every database. Task 1 tests the order.
- A hook that changes the body in place after the check, or re-saves the resource (`after_write`): the decision must not change, and the second save must not reuse it. Covered by Task 1 (the snapshots) and Task 3 (the second save).
- A superuser write in a context where the type's own hook (`narrow`) refuses unknown transitions: still refused. The hook sees `"__OVERRIDE__"` (Task 9).
- The same transition name taken in two contexts: its `on_transition` action runs once, with both contexts, after `on_save`. An action that raises fails neither the write nor the other actions (Task 13).

---

## Part A: #37, carry the decision (branch `feature/workflow-decision`, already created off `origin/develop`)

### Task 0: Commit the spec and plan (controller)

- [ ] `git add docs/superpowers/specs/2026-11-20-workflow-decisions-design.md docs/superpowers/plans/2026-11-20-workflow-decisions.md && git commit -m "docs: workflow decisions design and plan (#58)"`

### Task 1: The decision types and the shared per-context computation

**Files:**
- Create: `scrud_django/core/decisions.py`
- Modify: `scrud_django/core/workflow.py` (move `NullContent`/`NULL_CONTENT` to decisions, re-export them; `NULL_STORED_CONTENT` alias stays)
- Test: `tests/test_core_decisions.py` (plain pytest, like `tests/test_core_transitions.py`)

**Interfaces:**
- Produces (in `scrud_django.core.decisions`):
  - `NullContent`, `NULL_CONTENT` (moved; `core.workflow` re-exports both, so existing imports keep working).
  - `decision_key(context: str | None) -> tuple`: returns `(context,)`. It is the one place #40 will add a region.
  - `@dataclass(frozen=True) class ContextDecision`:
    - `context: str | None`
    - `policy: StoredPolicyReference | DefaultPolicyReference` (that is `WorkflowPolicy.reference`)
    - `transition: TransitionData | None`
    - `error: WorkflowTransitionError | None`
    - `entering: bool`
    - `__post_init__` raises `ValueError` unless exactly one of `transition` and `error` is `None`.
  - `@dataclass(frozen=True) class WorkflowDecision`:
    - fields: `type_iri: str`, `stored_pk: object | None`, `actor: object | None`, `checked: bool`, `prior: Mapping | None`, `proposed: Mapping`, `contexts_after: tuple[str | None, ...]`, `contexts: tuple[ContextDecision, ...]`
    - `__post_init__` stores `copy.deepcopy` of `prior` and `proposed` (via `object.__setattr__`) and converts list fields to tuples.
    - methods and properties:
      - `get(context) -> ContextDecision | None`
      - `transitions -> MappingProxyType[str | None, TransitionData | None]`
      - `errors -> MappingProxyType[str | None, WorkflowTransitionError]`
      - `transition_data -> TransitionData | None` and `transition_error -> WorkflowTransitionError | None`: those of the first context in `contexts_after` that has an entry.
  - `concerned_policies(type_iri: str, *, contexts_before: Sequence[str], contexts_after: Sequence[str | None]) -> list[tuple[str | None, WorkflowPolicy]]`:
    - The order is `contexts_before`, then `contexts_after`, duplicates dropped.
    - An empty `contexts_after` becomes `[None]`.
    - `None` gets `default_workflow_policy_for`; named contexts get `workflow_policies_for` in one query.
    - Contexts with no policy are dropped.
  - `context_transition(policy: WorkflowPolicy, prior: Mapping | NullContent | None, proposed: Mapping, *, entering: bool) -> TransitionData`:
    - `prior is None` (a create) or `entering`: `policy.initial_transition(proposed)`.
    - Otherwise: `policy.determine_transition(proposed, prior)`.
    - It raises workflow errors; callers decide what to do with them.
  - `is_entering(context, *, prior, contexts_before) -> bool`: `prior is None or (context is not None and context not in contexts_before)`. A create enters every context (spec).
  - `snapshot_prior(prior) -> Mapping | None`: `None` for `None` and for `NULL_CONTENT`.

- [ ] **Step 1: Write the failing tests** in `tests/test_core_decisions.py` (TYPE_IRI `"tests://ToDo"`, which uses the default policy in `tests/static/todo_flow/workflow.json`):

```python
def test_contexts_follow_stored_then_new_array_order():
    pairs = concerned_policies(TYPE_IRI, contexts_before=["b", "a"], contexts_after=["c", "a"])
    assert [c for c, _ in pairs] == ["b", "a", "c"]

def test_no_context_is_the_settings_default_under_none():
    assert [c for c, _ in concerned_policies(TYPE_IRI, contexts_before=[], contexts_after=[])] == [None]

def test_a_create_enters_every_context():
    assert is_entering("a", prior=None, contexts_before=[])
    assert is_entering(None, prior=None, contexts_before=[])

def test_snapshots_do_not_follow_later_changes():
    body = {"status": "not started"}
    decision = WorkflowDecision(type_iri=TYPE_IRI, stored_pk=None, actor=None, checked=True,
                                prior=None, proposed=body, contexts_after=("a",), contexts=())
    body["status"] = "done"
    assert decision.proposed == {"status": "not started"}

def test_transition_data_is_the_first_new_context_with_an_entry():
    ...  # two ContextDecisions "a" (stored only) and "b"; contexts_after=("x", "b")
    assert decision.transition_data == b_entry.transition
    assert decision.transitions == {"a": a_entry.transition, "b": b_entry.transition}
    with pytest.raises(TypeError):
        decision.transitions["a"] = None

def test_a_context_decision_has_a_transition_or_an_error():
    with pytest.raises(ValueError):
        ContextDecision(context="a", policy=REF, transition=None, error=None, entering=False)

def test_decisions_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.checked = False

def test_null_content_moved_and_re_exported():
    from scrud_django.core import workflow
    assert workflow.NULL_CONTENT is NULL_CONTENT and workflow.NULL_STORED_CONTENT is NULL_CONTENT
```

- [ ] **Step 2: Run** `uv run pytest tests/test_core_decisions.py`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `scrud_django/core/decisions.py` as specified.
  - It imports `scrud_django.workflow`, `scrud_django.exceptions` and the standard library only.
  - It must not import `core.workflow`, `core.transitions` or `mixins` (that would be an import cycle).
  - Move `NullContent`/`NULL_CONTENT` here.
  - `core/workflow.py` imports both back.
- [ ] **Step 4: Run** `uv run pytest tests/test_core_decisions.py tests/test_core_imports.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(core): workflow decision types and the shared per-context computation (#37)`.

### Task 2: Both paths build a `WorkflowDecision`

**Files:**
- Modify: `scrud_django/core/workflow.py`
  - `workflow_check` returns a `WorkflowDecision`.
  - Delete `transition_for` (its callers use `context_transition`).
  - Keep `authorize`, `allowed` and `authorize_without_policy` as they are.
- Modify: `scrud_django/core/transitions.py`
  - `transitions_between` returns a `WorkflowDecision` with `checked=False`.
  - `transition_in` becomes a thin wrapper over `context_transition` that returns `(TransitionData | None, error | None)`; keep its `leaving` parameter, whose behaviour is unchanged until Task 6.
  - `Transitions = WorkflowDecision` is kept as a deprecated alias, with a comment.
- Modify: `scrud_django/core/writes.py`: `validate_write` returns the decision.
- Modify: `scrud_django/mixins/resource_mixin.py`
  - `get_transitions` passes `prior_of(...)` semantics: a row whose content is not a Mapping is `NULL_CONTENT`, not a create.
  - It still re-exports `Transitions`.
- Modify: `scrud_django/ninja/workflow.py`: keep the returned decision (it is used in Task 4).
- Test: `tests/test_core_decisions.py`, `tests/test_core_workflow.py`, `tests/test_core_transitions.py`, `tests/test_core_writes.py` (update the readers of the old list).

**Interfaces:**
- Consumes: Task 1's `concerned_policies`, `context_transition`, `is_entering`, `snapshot_prior`, `WorkflowDecision`, `ContextDecision`.
- Produces:
  - `workflow_check(request, *, type_iri, write, narrow=(), post_save_type_iri=None) -> WorkflowDecision`
    - `checked=True`
    - `actor=access.request_user(request)` if that user `is_authenticated`, else `None`
    - `stored_pk` is the `Update`'s, or `None`
    - A request of `None` where no policy applies returns a decision with no contexts.
  - `validate_write(...) -> WorkflowDecision`.
  - `transitions_between(type_iri, *, prior, proposed, contexts_before, contexts_after, stored_pk=None, actor=None) -> WorkflowDecision`. `prior` may now be `NULL_CONTENT`.

The check keeps its order: compute every context's transition (errors hidden per context), then authorize each, then `authorize_without_policy` for the contexts without a policy, then the mark. Only the per-context function changes.

- [ ] **Step 1: Write the failing tests**:
  - `test_a_checked_and_an_unchecked_decision_agree` in `test_core_decisions.py`, for the same create and for the same update (contexts `["a"]` → `["a", "b"]`): `checked.contexts == unchecked.contexts`.
    - Use `workflow_check` with a `RequestFactory` request whose user is a superuser (allowed everywhere today).
    - Use `transitions_between` for the unchecked side.
  - `test_workflow_check_returns_the_decision_with_its_actor` (`decision.actor == user`, `decision.checked`, `decision.stored_pk == 7` for an `Update(..., stored_pk=7)`).
  - `test_null_stored_content_is_an_update_from_no_state` in `test_core_transitions.py`:
    - `transitions_between(TYPE_IRI, prior=NULL_CONTENT, proposed=NOT_STARTED, contexts_before=[""], contexts_after=[""])`
    - asserts `result.transitions[""] is None` and `type(result.errors[""]) is WorkflowTransitionError`.
    - The commit message says why: it was reported as `__INITIALIZE__` before.
- [ ] **Step 2: Run** those tests. Expected: FAIL.
- [ ] **Step 3: Implement** the changes listed under Files. Update every reader of the old return values (`grep -rn "workflow_check(\|validate_write(\|transitions_between(" scrud_django tests`). A test that read `[(context, policy, data), ...]` now reads `decision.contexts` / `decision.transitions`, with the same assertions.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit** `feat(core): workflow_check and transitions_between build one WorkflowDecision (#37)`.

### Task 3: Carry the checked decision into `Resource.save()` and send it

**Files:**
- Modify: `scrud_django/core/authz.py`
  - `_mark_workflow_authorized(request, *, type_iri, stored_pk, decision=None)` also stores `decision` under `_WORKFLOW_DECISION = "_scrud_workflow_decision"`.
  - New `_workflow_decision(request) -> WorkflowDecision | None` returns it only while the mark is set.
  - `_consume_workflow_mark` removes both attributes, on the request and on `_request`.
- Modify: `scrud_django/core/workflow.py`: pass the decision to the mark.
- Modify: `scrud_django/core/decisions.py`:
  - `_PENDING: ContextVar[WorkflowDecision | None]`.
  - `@contextmanager applying(decision: WorkflowDecision | None)`, reset however the block ends.
  - `take_pending(*, type_iri: str, pk, content) -> WorkflowDecision | None`. It matches when `decision.type_iri == type_iri and decision.stored_pk == pk and decision.proposed == content`. On a match it consumes the decision (sets `_PENDING` to `None`).
- Modify: `scrud_django/core/writes.py` `save_checked`: read `_workflow_decision(request)` before anything consumes the mark, and wrap `run()` in `applying(decision)`.
- Modify: `scrud_django/mixins/resource_mixin.py` `ResourceMixin.save`:
  - `decision = take_pending(type_iri=self.resource_type.type_uri, pk=None if self._state.adding else self.pk, content=self.content)`.
  - Fall back to `self.get_transitions(db)`.
  - Log `decision.errors` as today.
  - Send `decision=decision` with the existing kwargs (taken from the decision).
- Modify: `scrud_django/core/signals.py`: the kwargs comment names `decision`.
- Test: `tests/test_workflow_decisions.py` (new; safetydance and `front_end`, idioms of `tests/test_workflow_contexts.py`, reusing its `reported_saves`-style fixture locally).

**Interfaces:**
- Consumes: Task 2's `workflow_check` / `transitions_between`.
- Produces: the `scrud_post_save` kwarg `decision: WorkflowDecision`. It is `checked=True` for every API/UI write whose saved content is the checked one.

- [ ] **Step 1: Write the failing tests** (TODO type, the default policy, a writer as in `test_workflow_contexts.py`):
  - `test_an_api_write_sends_the_checked_decision(front_end, reported_saves)`: after a PUT of a ToDo, `reported["decision"].checked is True`, `reported["decision"].actor == user`, and `reported["transitions"] == reported["decision"].transitions`.
  - `test_a_checked_save_does_not_recompute(front_end, monkeypatch)`: monkeypatch `scrud_django.mixins.resource_mixin.transitions_between` to raise `AssertionError`; the PUT answers 200. This pins the policy-edit race (Review Focus).
  - `test_a_server_side_save_computes_an_unchecked_decision(reported_saves)`: `ResourceRegistration` update gives `checked is False` and `actor is None`.
  - `test_a_second_save_in_the_block_computes_its_own` (core level): `with applying(decision): resource.save(); resource.save()`. The second sent decision `is not` the first and has `checked is False`.
  - `test_a_save_of_other_content_does_not_take_the_decision` (core level): `applying(decision)` with `decision.proposed != resource.content` gives `checked is False`.
- [ ] **Step 2: Run** `uv run pytest tests/test_workflow_decisions.py`. Expected: FAIL.
- [ ] **Step 3: Implement** the changes listed under Files.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS, parity included.
- [ ] **Step 5: Commit** `feat: Resource.save sends the workflow check's decision instead of recomputing it (#37)`.

### Task 4: Own models send the checked decision

**Files:**
- Modify: `scrud_django/core/decisions.py`: `same_transitions(checked: WorkflowDecision, saved: WorkflowDecision) -> bool`. It is true when both have the same contexts and equal `TransitionData` in each. (Task 10 extends it for overrides.)
- Modify: `scrud_django/ninja/workflow.py` `ModelWorkflow.save_checked`:
  - Keep `decision = check_write(...)`.
  - After the save, `recomputed = transitions_between(... state(obj) ..., stored_pk=..., actor=decision.actor)`.
  - Send `decision` when `same_transitions(decision, recomputed)`. Otherwise send `recomputed` and `log_skipped_check(recomputed.errors, ...)` as today.
  - Send `decision=` in the signal.
- Modify: `scrud_django/core/transitions.py`: the governed pre-save stashes a `WorkflowDecision` (`checked=False`). It still sends no signal (spec).
- Test: `tests/test_ninja_own_models.py`.

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: `same_transitions` (Task 10 extends it).

- [ ] **Step 1: Write the failing tests**:
  - `test_an_own_model_create_sends_the_checked_decision`: POST an Article as a writer, then `reported["decision"].checked is True` even though `ArticleOut.id` was `None` when checked.
  - `test_an_own_model_save_that_wrote_another_state_sends_the_recomputed_decision`: a `save` that sets `state="archived"` gives `checked is False`, a WARNING on `scrud_django.workflow`, and `transitions` reporting the saved state.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the changes listed under Files.
- [ ] **Step 4: Run** `uv run pytest tests/test_ninja_own_models.py tests/test_readme_own_models.py -q`, then `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(ninja): own models send the workflow check's decision (#37)`.

### Task 5: Docs and the MR for #37

**Files:** `README.md` ("Workflow actions": the `decision` kwarg and what it holds; "Your own models": the decision rule), `docs/release-notes.md` (`## Unreleased`; Added: `decision`; Changed: the `workflow_check`/`validate_write`/`transitions_between` return type, `Transitions` is no longer a NamedTuple, overriding `get_transitions` affects only unchecked saves, null stored content is an update from no state), `docs/follow-ups.md` (`core-save-checked-mark-body`: note that `WorkflowDecision.proposed` is the fingerprint the fix needs), `CLAUDE.md` (Architecture: name `core/decisions.py`).

- [ ] **Step 1:** Write the docs. Run `uv run --group docs sphinx-build -W -b html docs docs/_build`. Expected: build succeeds.
- [ ] **Step 2: Verify (controller)**, one suite at a time: `uv run pytest -q`, `uv run pytest -q --reverse`, `SCRUD_TEST_NO_DRF=1 uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run python manage.py check`, `uv run python manage.py makemigrations scrud_django --check --dry-run`.
- [ ] **Step 3: Commit** `docs: the workflow decision (#37)`. Push with `git push -u origin feature/workflow-decision`. Open the MR with `glab mr create --target-branch develop --title "Carry the workflow check's decision into save() (#37)"`, using a description that links #58 and #37. Comment on #37 with the MR link. Watch CI; on `runner_system_failure`, run `docker desktop restart` and retry the pipeline.

## Part B: #36 + #22, leaving and per-kind rules (branch `feature/workflow-leaving` off `feature/workflow-decision`)

### Task 6: Report the authorized transition in a context being left

**Files:**
- Modify: `scrud_django/workflow.py`
  - `TransitionData` gains `leaving: bool = field(default=False, kw_only=True)`.
  - The `LEAVE` comment says it is retired and never reported (kept for one release).
  - The docstring is updated.
- Modify: `scrud_django/core/decisions.py`
  - `context_transition(..., leaving: bool = False)` returns `dataclasses.replace(data, leaving=True)` when `leaving`.
  - `ContextDecision` gains `leaving: bool = False` (last field); `__post_init__` also requires `transition is None or transition.leaving == leaving`.
  - `concerned_policies` yields whether the context is left, meaning it is in `contexts_before` and not in `contexts_after`.
- Modify: `scrud_django/core/transitions.py`: `transition_in` no longer has a `LEAVE` branch; it computes as the check does.
- Modify: `scrud_django/core/workflow.py`: the check passes `leaving`.
- Test: `tests/test_core_transitions.py`, `tests/test_workflow_contexts.py`, `tests/test_condition_contexts.py`.

**Interfaces:**
- Produces: `TransitionData(proposed_state, prior_state, transition, *, leaving=False)` and `ContextDecision.leaving`.

- [ ] **Step 1: Write and update the tests** (the commit message says why the old expectations changed: owner decision (b) on #36):
  - `tests/test_core_transitions.py`:
    - `test_leaving_a_context_with_a_policy_reports_leave` becomes `test_leaving_a_context_reports_the_transition_there`: `left == TransitionData("not started", "not started", "__SAME_STATE__", leaving=True)`.
    - The test at line ~108 is updated the same way.
    - New: `test_leaving_from_no_state_unchecked_is_an_error`: `transitions["a"] is None` and `errors["a"]` is a `WorkflowTransitionError`.
  - `tests/test_workflow_contexts.py:1541` expects `"a": TransitionData("not started", "not started", "__SAME_STATE__", leaving=True)`.
  - `tests/test_workflow_contexts.py:1638` expects the transition from `"done"` that the check computes, with `leaving=True`. Read the test to find the right states.
  - New in `tests/test_condition_contexts.py`, beside `test_leaving_a_context_is_checked_with_that_context`: `test_leaving_a_context_reports_the_transition_authorized_there(front_end, ...)`. A writer allowed `finish` in "a" moves a resource out of "a" while finishing it; the reported `transitions["a"] == TransitionData("done", "not started", "finish", leaving=True)`.
  - Add one assertion that nothing reports `"__LEAVE__"` in these tests.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the changes listed under Files.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat!: a left context reports the transition authorized there, flagged leaving (#36)`.

### Task 7: Per-kind rules in `TransitionData` (#22)

**Files:** Modify `scrud_django/workflow.py` (`TransitionData.__post_init__`). Test: `tests/test_workflow_policy.py`.

Rules (`ValueError` naming the kind and the offending field):
- `transition` must be a `str`.
- `INITIALIZE`: `prior_state is None`, `proposed_state is not None`, and not `leaving`.
- `SAME_STATE`: `prior_state == proposed_state`, both not `None`.
- `LEAVE`: `proposed_state is None`.
- Any other name starting with `RESERVED_TRANSITION_PREFIX`: refused. Task 9 adds `OVERRIDE`, which accepts any states.
- Any other name: both states not `None`.

- [ ] **Step 1: Write the failing tests**:
  - `test_transition_data_kind_rules`, parametrized over valid and invalid tuples. Valid: `("draft", None, INITIALIZE)`, `("a", "a", SAME_STATE)`, `(None, "a", LEAVE)`, `("b", "a", "submit")`, `("a", "a", SAME_STATE, leaving=True)`. Invalid: `("draft", "x", INITIALIZE)`, `(None, None, INITIALIZE)`, `("draft", None, INITIALIZE, leaving=True)`, `("a", "b", SAME_STATE)`, `("a", None, LEAVE)`, `("b", None, "submit")`, `("b", "a", "__mine")`, `("b", "a", 3)`. Each invalid case raises `ValueError`.
  - Also assert that every TransitionData the existing policy tests produce still constructs (the suite run covers this).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat!: TransitionData enforces each kind's field rules (#22)`.

### Task 8: Docs and the MR for #36 + #22

**Files:**
- `README.md` "Workflow actions": a left context reports the transition authorized there with `leaving`. `__LEAVE__` is no longer reported. Field rules as in #22.
- `docs/release-notes.md`: Breaking: leaving (#36), `TransitionData` field rules (#22).
- `scrud_django/core/signals.py` comment.

- [ ] **Step 1:** Write the docs and build them (sphinx command above).
- [ ] **Step 2:** Run the verification in Task 5, Step 2.
- [ ] **Step 3:** Commit `docs: leaving a context and TransitionData rules (#36, #22)`.
  - Push `feature/workflow-leaving`.
  - Open the MR targeting `feature/workflow-decision`, titled "Report the transition authorized in a context being left; TransitionData kind rules (#36, #22)".
  - Comment on #36 and #22 with the link.

## Part C: #31, superuser override (branch `feature/workflow-superuser-override` off `feature/workflow-leaving`)

### Task 9: Superusers override the policy where it would refuse

**Files:**
- Modify: `scrud_django/workflow.py`: `OVERRIDE = "__OVERRIDE__"`, added to `RESERVED_TRANSITIONS`. `TransitionData` accepts any states for it (Task 7's rules).
- Modify: `scrud_django/core/authz.py`: `is_workflow_superuser(request) -> bool` (`access.request_user`, then `is_active and is_superuser`).
- Modify: `scrud_django/core/decisions.py`:
  - `ContextDecision.override` property.
  - `WorkflowDecision.overrides -> tuple[str | None, ...]`.
  - `override_transition(policy, prior, proposed, *, entering, leaving) -> TransitionData`: each state is the single state the content is in, or `None` (no state, or ambiguous: use `policy.matching_states` on the stored and new content; `prior` is `None` when entering or creating).
- Modify: `scrud_django/core/workflow.py` `workflow_check`, per context:
  - If `context_transition` raises one of `WORKFLOW_ERRORS` (from `core.transitions`; move the tuple to `core/decisions.py` and re-export it) and `is_workflow_superuser(request)`, use `override_transition`.
  - Keep the overridden error on the `ContextDecision`. Add the field `overridden: WorkflowTransitionError | None = None`, used in Task 10's log.
  - Authorization then runs as usual: `narrow` sees `"__OVERRIDE__"`.
- Test: `tests/test_workflow_override.py` (new; safetydance, `front_end`, `admin_login`).

**Interfaces:**
- Produces: `OVERRIDE`, `is_workflow_superuser`, `ContextDecision.override` / `.overridden`, `WorkflowDecision.overrides`.

- [ ] **Step 1: Write the failing tests**:
  - Content in no state:
    - Store a ToDo server-side as `{"status": "nonsense"}` in "a".
    - The superuser PUTs `NOT_STARTED`: 200.
    - The reported `transitions["a"] == TransitionData("not started", None, "__OVERRIDE__")`.
    - `decision.overrides == ("a",)`.
  - Ambiguous stored content (as in `test_a_resource_stored_in_more_than_one_state_cannot_be_updated`):
    - The superuser's PUT answers 200, reported as `__OVERRIDE__` with `prior_state is None`.
  - A `restrict_update` state edited in place: 200, reported as `__OVERRIDE__`.
  - Leaving a context from no state: 200, `transitions["a"].leaving is True` and the transition is `__OVERRIDE__`.
  - A valid superuser write (`not started` → `in progress`): reported `"start"`, not overridden, and no WARNING on `scrud_django.workflow` (`caplog`).
  - A body failing the JSON Schema is still 400 for the superuser.
  - The type's hook check still applies (Review Focus):
    - Register a ToDo hook `checks` that refuses `current_transition() == "__OVERRIDE__"` (follow the existing hook tests; grep `HookLeaf` in tests).
    - The superuser's no-state PUT is refused with the same status a non-superuser's hook refusal gets.
  - `is_workflow_superuser` is false for an inactive superuser (`RequestFactory` with `user.is_active = False`).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the changes listed under Files.
- [ ] **Step 4: Run** `uv run pytest tests/test_workflow_override.py`. Expected: PASS.
- [ ] **Step 5: Commit** `feat!: superusers override the workflow policy where it would refuse (#31)`.

### Task 10: Log applied overrides; own models report them

**Files:**
- Modify: `scrud_django/core/transitions.py`: `log_overrides(decision: WorkflowDecision, *, subject: str) -> None`.
  - One WARNING per overridden context on `scrud_django.workflow`.
  - The message names the subject, type IRI, context, `policy`, prior and proposed state, `overridden.message`, and `matching_states` when the overridden error is ambiguous.
- Call it in `ResourceMixin.save` (after the row is written, before the signal, beside `log_workflow_errors`) and in `ModelWorkflow.save_checked`.
- Modify: `same_transitions` (Task 4): a context the checked decision overrode counts as equal when the recomputed decision has an error there.
- Test: `tests/test_workflow_override.py`, `tests/test_ninja_own_models.py`.

- [ ] **Step 1: Write the failing tests**:
  - The resource no-state override from Task 9 logs exactly one WARNING containing `"tests://ToDo"`, `"'a'"` and the overridden message.
  - An own-model override: an Article stored as `state="bogus"`, PUT by a superuser to `draft`. Expect 200, `reported["decision"].checked is True`, `transitions["a"].transition == "__OVERRIDE__"`, and one WARNING (not the "skipped the workflow check" one).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the changes listed under Files.
- [ ] **Step 4: Run** `uv run pytest tests/test_workflow_override.py tests/test_ninja_own_models.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: log workflow overrides; own models report them (#31)`.

### Task 11: Existing superuser tests, docs and the MR for #31

- [ ] **Step 1:** Run `uv run pytest -q`.
  - For each failure where a superuser (`admin_login`, `is_superuser=True`) expected a workflow refusal, such as `test_transition_from_no_state_error_names_no_prior_state` and `tests/test_workflow_contexts.py:510-516`:
    - Switch that test to a non-superuser writer that holds the same grants (`regular_login` / `user_with(...)`), so it keeps pinning the refusal unchanged.
    - Make sure Task 9 covers the superuser side of the same case.
  - Never relax an assertion. Any other kind of failure is a bug to fix (`superpowers:systematic-debugging`).
  - Re-run until green.
- [ ] **Step 2:** Docs:
  - README:
    - "Workflow policies": superusers override the policy where it would refuse, with what still applies.
    - "Workflow actions": `__OVERRIDE__`, `ContextDecision.override`, the WARNING.
  - `docs/release-notes.md`: Breaking: #31.
  - `docs/follow-ups.md`: `superuser-is-active`. scoped-rbac decides superusers without `is_active`, while the workflow requires it. Describe the test that would show a divergence: an inactive superuser authenticated by a custom backend.
- [ ] **Step 3:** Run the verification in Task 5, Step 2.
- [ ] **Step 4:** Commit `test: pin workflow refusals with non-superusers; docs for #31`.
  - Explain the switched tests in the commit message.
  - Push `feature/workflow-superuser-override`.
  - Open the MR targeting `feature/workflow-leaving`, titled "Superusers bypass workflow authorization (#31)".
  - Comment on #31.

## Part D: #20, actions keyed by transition, actor, prior content, outbox (branch `feature/workflow-transition-actions` off `feature/workflow-superuser-override`)

### Task 12: The actor and prior content in signals

**Files:**
- Modify: `scrud_django/core/signals.py`:
  - `_ACTING: ContextVar`
  - `@contextmanager acting_as(user)`
  - `acting_user() -> object | None`
- Modify: `scrud_django/core/transitions.py` and `mixins/resource_mixin.py`: `transitions_between`'s `actor=None` means `acting_user()`, so unchecked decisions carry the acting user.
- Modify: `ResourceMixin.save` and `ModelWorkflow.save_checked` send `user=decision.actor` and `prior_content=decision.prior`. `ResourceMixin.delete` and `ModelWorkflow.delete_checked` send `user=acting_user()`.
- Modify: `core.writes.save_checked` and `core.writes.destroy`: wrap the save or delete in `acting_as(<the request's authenticated user or None>)`, using the same helper as the decision's actor. Factor it out as `core.authz.request_actor(request)` and use it in `workflow_check` too.
- Modify: `ModelWorkflow.delete_checked`: the same wrapping.
- Test: `tests/test_workflow_decisions.py`, `tests/test_ninja_own_models.py`.

**Interfaces:**
- Produces:
  - `acting_as`, `acting_user`, `request_actor`.
  - `scrud_post_save` kwargs `user` and `prior_content`.
  - `scrud_post_delete` kwarg `user`.

- [ ] **Step 1: Write the failing tests**:
  - `test_a_put_sends_its_user_and_prior_content(front_end, reported_saves)`: `user` is the writer, and `prior_content` equals the stored content before the write.
  - `test_a_create_sends_no_prior_content`.
  - `test_a_delete_sends_its_user(front_end)`.
  - `test_server_code_names_its_actor`: `with acting_as(user): ResourceRegistration.update(...)` sends `user == user` with `decision.checked is False`.
  - The same user and prior-content assertions for an own-model PUT and DELETE.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the changes listed under Files.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: scrud signals carry the acting user and prior content (#20)`.

### Task 13: `on_transition` actions

**Files:**
- Modify: `scrud_django/core/signals.py` `ScrudSignalProcessor`:
  - `post_transition_literal = "on_transition"`.
  - After the `on_save` action, `post_save_trigger` reads `kwargs.get("decision")`. For each distinct transition name, in order of first appearance over `decision.contexts` with a non-`None` transition, it looks up `actions["on_transition"].get(name)` and calls `run_on_commit(fn, sender, {**kwargs, "transition": name, "contexts": (...)}, f"on_transition[{name}]")`.
  - A bad entry is logged and runs nothing, as `determine_action` does today (E012).
- Modify: `scrud_django/checks.py` `check_workflow_actions`:
  - E008 accepts `on_transition`.
  - New `scrud_django.E012`: an `on_transition` that is not a mapping, a key that is not a `str`, a value neither callable nor `None`, or a `__`-prefixed key other than `__INITIALIZE__`, `__SAME_STATE__` or `__OVERRIDE__`.
- Modify: `tests/workflow_actions.py` with recorders for the tests.
- Test: `tests/test_workflow_actions_setting.py` (the checks) and `tests/test_transition_actions.py` (new; dispatch, both front ends).

- [ ] **Step 1: Write the failing tests**:
  - `test_on_transition_runs_once_when_taken(front_end)`: ToDo `start`. The `"start"` action is called once on commit, with `transition="start"`, `contexts=("a",)`, and `user`. It runs after `on_save`: record the call order.
  - `test_on_transition_does_not_run_for_other_transitions`.
  - `test_on_transition_does_not_run_on_rollback`: a post-save 403.
  - `test_a_transition_taken_in_two_contexts_runs_once_with_both`: a server-side save in `["a", "b"]` under `acting_as`.
  - `test_a_leaving_transition_fires_with_its_flag`: the `decision.get("a").leaving` seen by the action is `True`.
  - `test_an_override_fires_on_transition_override`: a superuser no-state PUT, keyed `"__OVERRIDE__"`.
  - `test_a_failing_transition_action_fails_nothing`: the write is 200, `on_save` ran, and the error is logged on `scrud_django.workflow_actions` naming `on_transition[start]`.
  - E012 checks for each bad shape listed under Files, and E008 is silent for `on_transition`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the changes listed under Files.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: SCRUD_WORKFLOW_ACTIONS on_transition, keyed by transition (#20)`.

### Task 14: The opt-in outbox app

**Files:**
- Create the app package `scrud_django/outbox/`:
  - `__init__.py`
  - `apps.py`: `OutboxConfig`, `name = "scrud_django.outbox"`, `label = "scrud_django_outbox"`. `ready()` connects `store_event` with `dispatch_uid="scrud_django.outbox"` and imports `checks`.
  - `models.py`: `OutboxEvent`:
    - `type_uri = TextField()`
    - `resource_pk = CharField(max_length=255)`
    - `action = CharField(max_length=6)`
    - `checked = BooleanField(null=True)` (`None` for a delete)
    - `transitions = JSONField(default=list)`
    - `user_id = CharField(max_length=255, null=True)`
    - `content = JSONField(null=True)`
    - `prior_content = JSONField(null=True)`
    - `created_at = DateTimeField(auto_now_add=True)`
    - `sent_at = DateTimeField(null=True)`
    - an index on `("sent_at", "id")`
  - `migrations/0001_initial.py` (generated)
  - `receivers.py`: `store_event(sender, **kwargs)`. It records the event only when `recorded_type(kwargs["resource_type_uri"])` holds. It writes on `using`, in the write's transaction. `transitions` is a list in decision order of `{"context", "transition", "prior_state", "proposed_state", "leaving", "error"}`; `error` is the message string or `None`.
  - `delivery.py`: `send_events(deliver, batch=100) -> int`. It claims rows with `select_for_update(skip_locked=True)` on `router.db_for_write(OutboxEvent)`, ordered by `pk`, and marks `sent_at`. It returns the count.
  - `conf.py`: `outbox_settings()` reads `SCRUD_OUTBOX`. `recorded_type(type_uri) -> bool` is true for `TYPES == "__all__"` or a type URI in the list.
  - `checks.py`: `scrud_django.W010` when the app is installed and `TYPES` is unset or empty; `scrud_django.E013` when `TYPES` is neither `"__all__"` nor a list or tuple of `str`.
  - `management/commands/scrud_outbox_send.py`: `--batch` (default 100). It imports `SCRUD_OUTBOX["DELIVER"]` and fails with `CommandError` when unset. It prints the count sent.
- Modify: `tests/settings.py` `SETTINGS`: add `"scrud_django.outbox"` to `INSTALLED_APPS`, with no `SCRUD_OUTBOX`. If W010 then fails the `manage.py check` command, silence it in the test settings with `SILENCED_SYSTEM_CHECKS`, and assert W010 itself in a test.
- Modify: `.gitlab-ci.yml:53` and `Makefile:18`: `makemigrations scrud_django scrud_django_outbox --check --dry-run`.
- Test: `tests/test_outbox.py` (new). Use `override_settings(SCRUD_OUTBOX={"TYPES": ["tests://ToDo"]})`.

The outbox must not import DRF: the no-DRF suite covers it.

- [ ] **Step 1: Write the failing tests**:
  - An API PUT records one event: `transitions == [{"context": "a", "transition": "start", "prior_state": "not started", "proposed_state": "in progress", "leaving": False, "error": None}]`, plus `user_id == str(user.pk)` and `prior_content`.
  - A rolled-back write records none.
  - An unlisted type records none, and so does an empty `TYPES`.
  - A delete records `action == "DELETE"` with `checked is None`.
  - An own-model write records the IRI and `str(pk)`.
  - `send_events` delivers in `pk` order and marks `sent_at`. A `deliver` that raises leaves the rest unsent.
  - The command fails without `DELIVER`.
  - W010 and E013 each fire on their case.
  - Context order: a resource in `["b", "a"]` records `b` before `a`. This runs on PG and MySQL in CI.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the app. Generate its migration with `uv run python manage.py makemigrations scrud_django_outbox`.
- [ ] **Step 4: Run** `uv run pytest tests/test_outbox.py`, then `uv run pytest -q`. Expected: PASS, with no query-budget test changed.
- [ ] **Step 5: Commit** `feat: opt-in transactional outbox app, scrud_django.outbox (#20)`.

### Task 15: Docs, final verification and the MR for #20

**Files:**
- `README.md` "Workflow actions": `on_transition` (once per name, after `on_save`, with `transition` and `contexts`), the `user` and `prior_content` kwargs, and `acting_as`. Replace the hand-written outbox recipe with the app's documentation: install it, `SCRUD_OUTBOX` `TYPES` and `DELIVER`, `scrud_outbox_send`, at-least-once delivery, an idempotent `deliver`.
- `docs/release-notes.md` Added: these items, plus E012, E013 and W010.
- `CLAUDE.md`: the outbox app and its migrations check.

- [ ] **Step 1:** Write the docs and build them.
- [ ] **Step 2:** Run the verification in Task 5, Step 2, with the migrations check over both apps. Then, one at a time, run the PostgreSQL and MySQL suites locally in throwaway containers named for the task (commands in CLAUDE.md), or rely on CI's `test-postgres` and `test-mysql` and report their results.
- [ ] **Step 3:** Commit `docs: on_transition, actor, prior content and the outbox (#20)`.
  - Push `feature/workflow-transition-actions`.
  - Open the MR targeting `feature/workflow-superuser-override`, titled "Workflow actions keyed by transition, actor and prior content, opt-in outbox (#20)".
  - Comment on #20 and on #58 with the four MR links, in merge order.
  - Ask the owner before any merge.
