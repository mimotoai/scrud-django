# Core Hardening B: Core Types, Method Sets and Paging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the core the types and functions ninja phase 3 builds on (a validated requirement, distinct create and update inputs to the workflow check, typed hooks, route method sets, the list query with key-first paging) while only tests call the facade.

**Architecture:** Each type replaces a convention a caller had to know with one the constructor enforces. The DRF adapters convert at their edge (`scrud_django/drf_requirements.py`, `scrud_django/validators.py`, `scrud_django/paginations.py`), so DRF's answers do not change. Plan A's parity harness (`tests/test_core_parity.py`) must stay green after every task.

**Tech Stack:** Python 3.13, Django, DRF, django-scoped-rbac, pytest with safetydance/safetydance-django; mypy run ad hoc (`uv run --with mypy`), not added as a dependency.

**Spec:** `docs/superpowers/specs/2026-11-11-ninja-front-end-design.md` (§1.2's `list_queryset` row and the key-first paging note), and these `docs/follow-ups.md` sections: `core-type-redesign`, `core-method-sets-hard-coded`, `core-paging-key-first`.

**Stacking:** branch `feature/core-types`, cut from `feature/core-hardening` once Plan A (`2026-11-12-core-hardening-a-fail-closed.md`) is complete. Its MR targets `feature/core-hardening`.

## Global Constraints

- Plan A's Global Constraints apply unchanged (no upstream edits, no DRF in `core/`, `develop`'s assertions untouched, no version bump, follow-ups removed as resolved, CLAUDE.md test conventions, the commit trailer, per-task verification).
- The facade's signatures may change freely: only tests call it. Every change is listed in the 0.16.0 release notes' "Framework-neutral core" section, not under Upgrading (nothing released used them).
- `tests/test_core_parity.py` passes after every task.

## Review Focus

1. A `Requirement` built from an `AnyOf` whose option is a bare callable or a nested list (scoped-rbac allows both): normalised, not rejected. Pinned in Task 1.
2. A DRF view with no `IsAuthorized` (a requirement asking no grants) still served: the adapter's unchecked constructor. Pinned in Task 1.
3. An update over a resource whose stored content is JSON `null`: checked as an update, never a create. Pinned in Task 2.
4. OPTIONS on a computed type's item for a user granted every method: `Allow` lists only GET, HEAD, OPTIONS. Pinned in Task 4.
5. A MySQL list page holding a row over `sort_buffer_size` through the core's own paging: no error 1038. Pinned in Task 5.

---

### Task 1: A validated, frozen `Requirement`

**Files:**
- Modify: `scrud_django/core/requirements.py`, `scrud_django/core/types.py` (`requirement_for`), `scrud_django/core/resources.py`, `scrud_django/core/lists.py`, `scrud_django/core/writes.py` (entry points), `scrud_django/drf_requirements.py` (`requirement_of`)
- Test: `tests/test_core_requirements.py`, `tests/test_core_guards.py`

**Interfaces:**
- Produces, in `scrud_django/core/requirements.py`:
  - `@dataclass(frozen=True) class AnyOf: options: tuple[tuple[Leaf, ...], ...]`. `AnyOf.of(*options)` normalises each option (a leaf, a list of leaves, or scoped-rbac's forms `_entries` accepts) to a tuple; an empty `options` or an empty option raises `ValueError`.
  - `@dataclass(frozen=True) class Requirement: entries: tuple[Leaf | AnyOf, ...]`, iterable over `entries`.
    - `Requirement.of(entries) -> Requirement`: converts scoped-rbac `access.AnyOf` to the core's `AnyOf`, then raises `ImproperlyConfigured` unless it asks the grants (Plan A's `asks_grants`). Passing a `Requirement` returns it unchanged.
    - `Requirement.unchecked(entries) -> Requirement`: the same conversion, no grants check. Used only by `drf_requirements.requirement_of` (a DRF view with no `IsAuthorized` asks no grants and is still served).
  - `to_scoped_rbac(any_of: AnyOf) -> access.AnyOf` for any place a scoped-rbac call needs its own type.
- Every facade entry point (`load_for_read`, `check_object`, `allowed_methods`, `readable_queryset`, `list_last_modified`, `check_request`, `validate_write`, `create`, `update`, `destroy`, `save_checked` without `recheck`) takes `requirement: Requirement | Sequence` and starts with `requirement = Requirement.of(requirement)`, replacing its `require_grants` call. `require_grants` stays as `Requirement.of`'s check.
- `requirement_for(resource_type, slug=None) -> Requirement`.

- [ ] **Step 1: Write the failing tests**

```python
def test_requirement_of_normalises_scoped_rbac_anyof():
    # Requirement.of([access.AnyOf([GrantsLeaf(A)], [[GrantsLeaf(B), hook]])]).entries[0] == AnyOf(((GrantsLeaf(A),), (GrantsLeaf(B), hook)))  (compare by option shape and leaf identity)

def test_requirement_of_refuses_no_grants():
    # Requirement.of([]) and Requirement.of([hook]) raise ImproperlyConfigured

def test_anyof_refuses_empty():
    # AnyOf.of() and AnyOf.of([]) raise ValueError

def test_requirement_is_frozen():
    # dataclasses.FrozenInstanceError on assignment to .entries

def test_drf_view_without_isauthorized_still_served():
    # an existing tests/urls.py view with no IsAuthorized answers as before (pick one from tests/test_authz.py's AllowAny cases)
```

- [ ] **Step 2: Run; expect FAIL** (`Requirement` not defined).
- [ ] **Step 3: Implement.** Internal helpers (`requirement_leaves`, `_grants_meet`, `meets_object_checks`, `_object_permitted`, `_request_checks_pass`) iterate a `Requirement` and the core's `AnyOf`; `_entries` stays only inside `AnyOf.of`.
- [ ] **Step 4: Run** `uv run pytest tests/test_core_*.py tests/test_drf_parity.py tests/test_type_hooks.py -q`, then the full suite.
- [ ] **Step 5: Commit** (release notes: the facade takes a `Requirement`).

```bash
git commit -m "refactor(core): a validated, frozen Requirement with the core's own AnyOf"
```

### Task 2: Distinct create and update inputs to the workflow check

**Files:**
- Modify: `scrud_django/core/workflow.py` (`workflow_check`, `NULL_STORED_CONTENT`, `prior_of`), `scrud_django/core/writes.py` (`validate_write`), `scrud_django/validators.py` (DRF's `WorkflowValidator`)
- Test: `tests/test_core_workflow.py`

**Interfaces:**
- Produces, in `scrud_django/core/workflow.py`:
  - `class NullContent` with the single instance `NULL_CONTENT` (falsy, `repr` `"NULL_CONTENT"`), replacing `NULL_STORED_CONTENT = ()`. Keep `NULL_STORED_CONTENT` as an alias of `NULL_CONTENT` (it is imported by `validators.py`).
  - `@dataclass(frozen=True) class Create: proposed: Mapping; contexts_after: Sequence[str] | None = None`.
  - `@dataclass(frozen=True) class Update: prior: Mapping | NullContent; proposed: Mapping; readable: Callable[[], bool]; contexts_before: Sequence[str] | None = None; contexts_after: Sequence[str] | None = None`. `readable` is required (no fail-closed default needed any more).
  - `workflow_check(request, *, type_iri: str, write: Create | Update, narrow: Sequence[Callable] = ()) -> list[tuple[str | None, WorkflowPolicy, TransitionData]]`. Any other `write` raises `TypeError`. A delete stays out of scope (follow-up `delete-no-workflow-check`).
  - `prior_from(value: object) -> Mapping | NullContent`: `None` gives `NULL_CONTENT`, a `Mapping` itself, anything else `TypeError`. `prior_of(instance: Resource)` is `prior_from(instance.content)`. Ninja phase 3's own models use `prior_from`.
- `validate_write(..., stored, readable, requirement, narrow=None)` builds `Create` when `stored is None`, else `Update(prior_of(stored), body, readable, ...)`; with a `stored` and `readable=None` it raises `TypeError`.

- [ ] **Step 1: Write the failing tests**

```python
def test_create_is_checked_as_a_create():
    # workflow_check(write=Create(proposed=...)) authorizes the __INITIALIZE__ transition as today

def test_update_over_null_content_is_an_update():
    # workflow_check(write=Update(prior=NULL_CONTENT, proposed=..., readable=lambda: True)) asks the from-state check (refuses where a create would be allowed)

def test_workflow_check_refuses_other_inputs():
    # write=None and write={"proposed": ...} raise TypeError

def test_prior_from():
    # prior_from(None) is NULL_CONTENT; prior_from({"a": 1}) == {"a": 1}; prior_from([]) raises TypeError
```

Rewrite the existing `tests/test_core_workflow.py` calls (new on this branch, not on `develop`) to the new signature; keep every assertion's expected outcome.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement**, DRF's `WorkflowValidator` included.
- [ ] **Step 4: Run** `uv run pytest tests/test_core_workflow.py tests/test_workflow*.py tests/test_core_parity.py -q`, then the full suite.
- [ ] **Step 5: Commit.**

```bash
git commit -m "refactor(core): the workflow check takes a Create or an Update"
```

### Task 3: Typed hooks, exception attributes and `ViewLeaf`'s IRI

**Files:**
- Modify: `scrud_django/core/types.py` (`TypeOptions`), `scrud_django/exceptions.py` (`ScrudException`), `scrud_django/drf_requirements.py` (`ViewLeaf`)
- Test: `tests/test_type_hooks.py`, `tests/test_exceptions.py` (add only), `tests/test_drf_parity.py`

**Interfaces:**
- In `scrud_django/core/types.py`: `class Check(Protocol): def __call__(self, request, resource_type, body: Mapping, context: str) -> bool`; `class ObjectCheck(Protocol)` `(request, resource_type, obj: Mapping) -> bool`; `class Compute(Protocol)` `(request, key: str) -> Mapping`. `TypeOptions.checks: tuple[Check, ...]`, `object_checks: tuple[ObjectCheck, ...]`, `compute: Compute | None`. `__post_init__` converts any sequence given for `checks`/`object_checks` to a tuple (`object.__setattr__`) and raises `TypeError` for a non-callable entry.
- `ScrudException` declares `type: str | None = None`, `title: str | None = None`, `instance: str | None = None` as class attributes; `response_data` reads them directly (no `hasattr`), leaving out a member that is `None`. The body of every existing error is unchanged (`tests/test_exceptions.py` on `develop` is the oracle).
- `ViewLeaf.__init__` reads `type_iri` once, under `as_detail_route`, and stores it; it no longer reads `view.resource_type_iri` on each use.

- [ ] **Step 1: Write the failing tests**: `TypeOptions(checks=[f]).checks == (f,)`; `TypeOptions(checks=[1])` raises `TypeError`; a bare `ScrudException` subclass with no `type` set renders a body with no `type` key; a `ViewLeaf` built on a list view keeps its item IRI after `view.action` changes.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the touched modules, `tests/test_core_parity.py`, then the full suite.
- [ ] **Step 5: Commit.**

```bash
git commit -m "refactor(core): typed hooks, declared problem fields, ViewLeaf's IRI read once"
```

### Task 4: The methods a route serves

**Files:**
- Modify: `scrud_django/core/resources.py` (`ITEM_METHODS`, `LIST_METHODS`, `allowed_methods`)
- Test: `tests/test_core_resources.py`, `tests/test_core_parity.py`

**Interfaces:**
- Produces: `served_methods(resource_type, *, item: bool) -> tuple[str, ...]` in `scrud_django/core/resources.py`: a computed type (its `TypeOptions.compute` is set, by type IRI) serves `("GET",)`; any other type `ITEM_METHODS` or `LIST_METHODS`. `allowed_methods(request, resource_type, obj, requirement, *, list_requirement=None, methods: Sequence[str] | None = None)`: `methods` defaults to `served_methods(resource_type, item=obj is not None)`; HEAD and OPTIONS are added as today.

- [ ] **Step 1: Write the failing test**

```python
def test_allowed_methods_of_a_computed_item():
    # superuser; a computed type registered with compute= (tests/resource_types.py or tests/urls.py has one)
    # allowed_methods(...) == ["GET", "OPTIONS", "HEAD"]; and equals the DRF OPTIONS Allow for that route
```

- [ ] **Step 2: Run; expect FAIL** (PUT and DELETE listed).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_core_resources.py tests/test_core_parity.py -q`, then the full suite.
- [ ] **Step 5: Commit** (remove `core-method-sets-hard-coded`).

```bash
git commit -m "feat(core): allowed_methods takes the methods its route serves"
```

### Task 5: `list_queryset` and key-first paging in the core

**Files:**
- Modify: `scrud_django/core/lists.py`, `scrud_django/paginations.py` (`StandardResultsSetPagination.paginate_queryset`)
- Test: `tests/test_core_lists.py`, `tests/test_ui_branch_large_rows.py` (read only, for how it builds a large row)

**Interfaces:**
- Produces, in `scrud_django/core/lists.py`:
  - `list_queryset(request, resource_type, params: QueryDict, *, requirement, ignored: frozenset[str] = frozenset(), using: str | None = None) -> QuerySet[Resource]`: `readable_queryset`, then `content_filtered` when the type's `TypeOptions.content_filter` is set, ordered by `Resource.Meta.ordering` (spec §1.2).
  - `rows_by_keys(queryset, keys: Sequence) -> list[Resource]`: the rows of `keys` in `keys`' order, read by key (`unsorted` as in `paginations.py`).
  - `page_of(queryset, *, number: int, size: int) -> tuple[list[Resource], int]`: the page's rows and the total count. On MySQL it pages `queryset.values_list("pk", flat=True)` and reads the rows with `rows_by_keys`; elsewhere it slices the queryset. A `number` past the last page raises `django.core.paginator.EmptyPage`.
- `StandardResultsSetPagination.paginate_queryset` keeps its own DRF page computation and calls `rows_by_keys` for the MySQL branch (its answers are unchanged).

- [ ] **Step 1: Write the failing tests**

```python
def test_list_queryset_orders_and_filters():
    # three resources; content_filter type; params {"text": "x"}: only matching, in Resource.Meta.ordering

def test_page_of_returns_key_order():
    # 5 resources, size 2: page 1 == first two in order, count 5; page 4 raises EmptyPage

@pytest.mark.skipif(not on_mysql, reason="MySQL's sort_buffer_size")
def test_page_of_a_large_row_on_mysql():
    # a row over sort_buffer_size, built as tests/test_ui_branch_large_rows.py builds one: page_of returns it, no OperationalError 1038
```

- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_core_lists.py tests/test_pagination*.py tests/test_core_parity.py -q`, then the full suite; then `tests/test_core_lists.py` and `tests/test_ui_branch_large_rows.py` on MySQL 8.4 (CLAUDE.md's container command).
- [ ] **Step 5: Commit** (remove `core-paging-key-first`).

```bash
git commit -m "feat(core): list_queryset and key-first paging in the core"
```

### Task 6: Type-check the core and verify the branch

- [ ] Run `uv run --with mypy mypy scrud_django/core --ignore-missing-imports --follow-imports=silent`. Fix every error in `scrud_django/core/` (none outside it). Record in `docs/follow-ups.md` (new section `core-type-check-ci`) that mypy is not yet in CI, with this command as the test. Remove `core-type-redesign`.
- [ ] Run `uv run pytest -q`, `uv run pytest -q --reverse`, ruff check and format, the migrations check and the sphinx `-W` build; the suite on PostgreSQL 14 and MySQL 8.4. Expected: all pass.
- [ ] `git diff origin/develop -- tests/` shows no `develop` assertion edited or removed.
- [ ] Commit: `git commit -m "chore(core): type-check the core; follow-up for CI"`.
