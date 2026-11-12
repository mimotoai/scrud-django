# Core Hardening A: Fail-Closed Facade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the fail-open and silent-failure gaps the core facade still has, so that ninja phase 3 can call it without re-checking authorization itself.

**Architecture:** The core facade (`scrud_django/core/resources.py`, `lists.py`, `writes.py`) is not called by DRF. Each fix makes it refuse, or raise, where it now allows, and a new parity harness compares it with the DRF routes, so a change here cannot drift from DRF's answers unseen. Where a fix touches code DRF also runs (the workflow mark, the leaf trust rule), DRF's answers must stay as they are on `develop`.

**Tech Stack:** Python 3.13, Django, DRF, django-scoped-rbac, pytest with safetydance/safetydance-django.

**Spec:** `docs/superpowers/specs/2026-11-11-ninja-front-end-design.md` (phase 1 and §1.2), and these `docs/follow-ups.md` sections, which are this plan's requirements: `core-facade-parity-harness`, `core-leaf-self-declared-grants`, `core-list-grant-and-request-check`, `core-workflow-mark-not-bound`, `core-hooks-silently-absent`, `core-readable-queryset-stored-contexts`, `core-anyof-object-check-only`, `core-write-no-type-iri-recheck`, `core-small-semantics`.

**Stacking:** branch `feature/core-hardening`, cut from `feature/neutral-core` (MR !16). Its MR targets `feature/neutral-core`; GitLab retargets it to `develop` once !16 merges. Plan B (`2026-11-12-core-hardening-b-types.md`) stacks on this branch.

## Global Constraints

- Never edit scoped-rbac, scrudful, safetydance or safetydance-django; work around them here (CLAUDE.md).
- `scrud_django/core/` imports no DRF (`tests/test_core_imports.py`).
- DRF answers stay as on `develop`: no assertion of a test that exists on `develop` is edited or removed.
- No version bump: this is part of the held 0.16.0. Release-note entries go under 0.16.0.
- Each follow-up a task resolves is removed from `docs/follow-ups.md` in that task, and its behaviour noted in `docs/release-notes.md` (0.16.0) when it changes what an adopter can see.
- Test conventions are CLAUDE.md's: `@pytest.mark.django_db` then `@test_context`; steps only in `tests/steps.py`; tests pass in any order and alone.
- Every commit message ends with `Claude-Session: https://claude.ai/code/session_01SAZeWvu8MUgWeP2fTMBG43`.
- Verification per task: the task's tests, then `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`.

## Review Focus

1. A third-party leaf class (neither `GrantsLeaf`, `ViewLeaf` nor `HookLeaf`) in a requirement: the grants must still be asked beside it. Pinned in Task 2.
2. The same request, which passed `validate_write` once, writing twice or a different body: the second write must not reuse the workflow authorization. Pinned in Task 4.
3. A user with the item grant but not the list grant calling `readable_queryset`: refused, as the DRF list is. Pinned in Task 3.
4. A worker process that never imports the URLconf asking `requirement_for` for a hooked type: an error, not an empty requirement. Pinned in Task 5.
5. A hook returning a truthy non-bool (`"no"`, `1`, a dict): refused. Pinned in Task 7.

---

### Task 1: Facade parity harness

**Files:**
- Create: `tests/test_core_parity.py`
- Modify: `docs/follow-ups.md` (remove `core-facade-parity-harness`)

**Interfaces:**
- Consumes: `tests/test_core_resources.py::users()` (the user matrix), the facade (`load_for_read`, `allowed_methods`, `readable_queryset`, `validate_write`, `create`, `update`, `destroy`), `scrud_django.core.types.requirement_for`.
- Produces: `outcome_of(callable) -> int`, mapping a facade result to the status DRF would give (a value: 200; `Http404`: 404; `access.AccessDenied`: 403; a `ScrudException`: its `status_code`; `access.ReservedContextError` or other `access.AccessError`: its DRF status via `scrud_django.permissions.drf_error`). Later tasks add parity cases here.

- [ ] **Step 1: Write the harness**

Parametrize over the types `JSON_LD` (`tests/test_core_resources.py`'s type), `hooked-notes` (`tests/resource_types.py`), and the workflow type the suite uses in `tests/test_core_workflow.py`. Parametrize over every user in `users()` and over resources stored in "a", in "b", and in `["a", "b"]`. For each combination assert:

```python
def test_item_read_parity(type_slug, user_name, stored_in): ...
    # DRF GET of the detail URL: status S
    # outcome_of(lambda: load_for_read(req, rt, pk, requirement=requirement_for(rt, type_slug), method="GET")) == S

def test_item_delete_parity(type_slug, user_name, stored_in): ...
    # load_for_read(method="DELETE") outcome == DRF DELETE status, mapped 204 -> 200 (DRF deletes; run DRF last, in its own savepoint)

def test_allow_parity(type_slug, user_name, stored_in): ...
    # sorted(allowed_methods(...)) == sorted(DRF OPTIONS Allow header split on ", ")

def test_list_parity(type_slug, user_name): ...
    # DRF GET of the list: 403 -> facade raises AccessDenied; 200 -> {r.pk for r in readable_queryset(...)} == {item pks in the DRF body}

def test_write_parity(type_slug, user_name, body_context): ...
    # PUT of {"text": "x", "rbac_context": body_context} on a resource in "a": DRF status class (2xx/400/403/404/409)
    # == outcome class of validate_write(... requirement=...) then update(...), inside transaction.atomic + savepoint rollback
```

`test_list_parity` must be marked `@pytest.mark.xfail(strict=True, reason="core-list-grant-and-request-check: Task 3")` for the users whose DRF list is a 403 (split those out as their own parametrized test so the strict xfail is exact). Build facade requests with `RequestFactory` and `force_authenticate`-equivalent `request.user = user`, as `tests/test_core_resources.py` does.

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_core_parity.py -q`
Expected: all pass except the strict xfails, which xfail. Any other failure is a real divergence: stop and record it in the report; do not loosen the harness to hide it.

- [ ] **Step 3: Commit**

```bash
git add tests/test_core_parity.py docs/follow-ups.md
git commit -m "test(core): a parity harness comparing the facade with the DRF routes"
```

### Task 2: Trust only known grant-asking leaves

**Files:**
- Modify: `scrud_django/core/requirements.py` (`asks_grants`, `_grants_meet`, new registry), `scrud_django/core/resources.py:159` (`check_grants_in_each_context`), `scrud_django/core/writes.py` (`check_post_save`, where it skips the grants on `reads_each_context`), `scrud_django/drf_requirements.py` (register `ViewLeaf`)
- Test: `tests/test_core_guards.py`, `tests/test_core_parity.py`

**Interfaces:**
- Produces: in `scrud_django/core/requirements.py`:
  - `register_grant_asking_leaf(cls: type) -> type` (usable as a class decorator), applied to `GrantsLeaf`; `drf_requirements.py` applies it to `ViewLeaf`.
  - `asks_grants_itself(leaf) -> bool`: `isinstance(leaf, <registered classes>) and leaf.reads_each_context`.
  - `is_hook(leaf) -> bool`: `isinstance(leaf, scrud_django.core.types.HookLeaf)` (import lazily to avoid the cycle). Replaces every `getattr(leaf, "grants_nothing", False)` read.
- Rule: a leaf is trusted to ask the grants only by its class. `asks_grants` counts a leaf as asking only if it is an instance of a registered class (`AnyOf` keeps its every-option rule). The grants are asked beside the leaves (`check_grants_in_each_context`, the post-save check) unless every leaf is `asks_grants_itself` or `is_hook`. A leaf of any other class, whatever its `reads_each_context` or `grants_nothing` say, gets the grants asked beside it, and counts as asking none.

- [ ] **Step 1: Write the failing tests** in `tests/test_core_guards.py`

```python
class SelfTrusting:
    reads_each_context = True
    overrides_object_checks = False
    def contexts_for(self, obj): return rbac_contexts_of(obj)
    def has_permission(self, request, body): return True
    def has_object_permission(self, request, obj): return True
    def has_context_permission(self, request, obj, context): return True

def test_a_leaf_of_an_unknown_class_does_not_count_as_asking_grants():
    # require_grants([SelfTrusting()], "x") raises ImproperlyConfigured
    # require_grants([SelfTrusting(), GrantsLeaf(JSON_LD)], "x") passes

def test_the_grants_are_asked_beside_a_leaf_of_an_unknown_class():
    # anonymous / reader-b: load_for_read(..., requirement=[GrantsLeaf(JSON_LD), SelfTrusting()], method="DELETE") on a resource in "a"
    # raises Http404 (unreadable) or AccessDenied; a writer with DELETE in "a" loads it

def test_a_grants_nothing_flag_on_an_unknown_class_is_ignored():
    # class with grants_nothing = True and reads_each_context = True: behaves as SelfTrusting above
```

Add a `test_core_parity.py` case: `requirement_for` requirements answer as before (the harness from Task 1 passes unchanged).

- [ ] **Step 2: Run them; expect the first and third to fail** (`require_grants` passes; the load returns the resource).

- [ ] **Step 3: Implement the registry and rule** as the Interfaces block states. `HookLeaf` loses `grants_nothing` (keep the attribute only if a test on `develop` reads it; none should).

- [ ] **Step 4: Run** `uv run pytest tests/test_core_guards.py tests/test_core_parity.py tests/test_type_hooks.py tests/test_drf_parity.py -q`, then the full suite. Expected: PASS, no `develop` assertion touched.

- [ ] **Step 5: Docs and commit.** Update `CLAUDE.md`'s Architecture paragraph (one sentence: the core trusts only `GrantsLeaf` and the DRF `ViewLeaf` to ask the grants; register another with `register_grant_asking_leaf`), remove `core-leaf-self-declared-grants` from `docs/follow-ups.md`.

```bash
git commit -m "fix(core): trust only registered leaf classes to ask the grants"
```

### Task 3: The list grant and the request check in the facade

**Files:**
- Modify: `scrud_django/core/resources.py` (new `check_request`; call it in `load_for_read`), `scrud_django/core/lists.py:178` (`readable_queryset` calls it), `tests/test_core_parity.py` (drop the strict xfails)
- Test: `tests/test_core_lists.py`, `tests/test_core_resources.py`

**Interfaces:**
- Produces: `check_request(request, resource_type, requirement, *, list_route: bool, body=None) -> None` in `scrud_django/core/resources.py`, re-exported nowhere else. It raises `access.AccessDenied` unless:
  - `list_route=True` and the request is a GET or HEAD: `GrantsLeaf(collection_type_uri_for(resource_type.type_uri)).has_permission(as_get(request), None)` allows, and every entry of `requirement` allows `has_permission(as_get(request), None)` (any option of an `AnyOf`);
  - otherwise: every entry of `requirement` allows `has_permission(request, body)`.
  It calls `require_grants(requirement, "check_request")` first.
- `readable_queryset(request, resource_type, requirement, *, using=None)` calls `check_request(request, resource_type, requirement, list_route=True)` before it filters. `load_for_read` calls `check_request(_as_method(request, method), resource_type, requirement, list_route=False)` **before** the lookup, as DRF's `initial()` runs `check_permissions` before `get_object()`: a requester refused at request level gets 403 for a missing and an existing pk alike (no existence leak). Add a parity case: such a user, a missing pk, DRF GET status == facade outcome (403).

- [ ] **Step 1: Write the failing tests**

```python
def test_readable_queryset_asks_the_list_grant():
    # user granted http.GET on JSON_LD in "a" but not on JSON_LD_LIST
    # DRF GET of the list: 403; readable_queryset(...) raises access.AccessDenied

def test_readable_queryset_lists_for_the_lister():
    # "lister-a": returns the resource in "a" only

def test_load_for_read_asks_the_request_check():
    # a requirement [GrantsLeaf(JSON_LD), Refusing()] where Refusing.has_permission returns False
    # and has_object_permission returns True: load_for_read raises AccessDenied (as DRF's has_permission refusal: 403)
```

- [ ] **Step 2: Run them; expect FAIL** (the queryset returns the resource; the load succeeds).

- [ ] **Step 3: Implement `check_request` and the two calls.** Remove the docstring paragraph in `readable_queryset` that says it does not check the list grant.

- [ ] **Step 4: Remove the strict xfails from `tests/test_core_parity.py`; run** `uv run pytest tests/test_core_parity.py tests/test_core_lists.py tests/test_core_resources.py -q`. Expected: PASS.

- [ ] **Step 5: Commit** (remove `core-list-grant-and-request-check` from follow-ups).

```bash
git commit -m "fix(core): the facade asks the list grant and the request check"
```

### Task 4: Bind the workflow mark to one write

**Files:**
- Modify: `scrud_django/core/authz.py:127-135` (`_mark_workflow_authorized`, `_workflow_authorized`), `scrud_django/core/workflow.py:244` (where the check marks), `scrud_django/core/writes.py` (`save_checked` consumes the mark; facade `create`/`update` validate the body they save)
- Test: `tests/test_core_writes.py`

**Interfaces:**
- Produces:
  - `_mark_workflow_authorized(request, *, type_iri: str, stored_pk: int | None)` stores the tuple `(type_iri, stored_pk)` on the request (attribute `_scrud_workflow_authorized`).
  - `_workflow_authorized(request, *, type_iri: str, stored_pk: int | None) -> bool`: true only for that exact tuple.
  - `_consume_workflow_mark(request) -> None`: deletes the attribute. `save_checked` calls it in a `finally` around its transaction, so one validation authorizes at most one save, success or failure.
  - `authorizing_write(request, *, covered, authorized=False, type_iri=None, stored_pk=None)`: passes them to `_workflow_authorized`.
  - Facade `create(request, resource_type, body, *, requirement, using, covered=True)` and `update(request, resource_type, instance, body, *, requirement, using, covered=True)` call `validate_write(request, resource_type, body, stored=instance, readable=..., requirement=requirement)` themselves, on the same `body` they save, unless the request already carries the mark for this `(type_iri, stored_pk)`. `readable` for `update` is `lambda: object_readable(request, resource_type, instance, requirement)`; for `create`, `None`.
- DRF (`WorkflowValidator`, `ResourceViewMixin.save_checked`) must keep its answers: it validates then saves once, so the tuple matches and consumption after its one save is invisible. DRF's `WorkflowValidator` passes the serializer's `instance.pk` (`None` on a create) as `stored_pk` and the view's `resource_type_iri_for(...)` as `type_iri`, the same IRI its post-save check names.

- [ ] **Step 1: Write the failing tests** in `tests/test_core_writes.py` (user: `http.PUT` on `JSON_LD` conditioned on `scrud_workflow`, as `may_stay_json_ld` in `tests/test_core_workflow.py`):

```python
def test_one_validation_authorizes_one_write():
    # validate_write(body in "a") then update(...) succeeds; a second update(...) on the same request raises AccessDenied; row as after the first

def test_a_validation_does_not_authorize_another_resource():
    # validate_write for resource X, then update of resource Y on the same request: AccessDenied, Y unchanged

def test_update_validates_the_body_it_saves():
    # update(...) without validate_write, with a body whose transition the workflow refuses: raises the workflow's error (400) and saves nothing
```

- [ ] **Step 2: Run; expect FAIL** (the second write and the other resource save).

- [ ] **Step 3: Implement** as the Interfaces block states.

- [ ] **Step 4: Run** `uv run pytest tests/test_core_writes.py tests/test_core_workflow.py tests/test_workflow*.py tests/test_core_parity.py -q`, then the full suite. Expected: PASS, no `develop` assertion touched.

- [ ] **Step 5: Commit** (remove `core-workflow-mark-not-bound`; release-notes line: the facade's `create`/`update` validate the body themselves; one validation authorizes one write).

```bash
git commit -m "fix(core): one workflow validation authorizes one write of one resource"
```

### Task 5: Hooks cannot go missing silently

**Files:**
- Modify: `scrud_django/core/types.py` (`register_type_options`, `type_options`, `hook_leaf`, `requirement_for`), `scrud_django/registration.py` and `scrud_django/computed.py` (callers), `scrud_django/apps.py` (see Step 3)
- Test: `tests/test_type_hooks.py`

**Interfaces:**
- Produces:
  - `register_type_options(type_iri: str, options: TypeOptions, *, slug: str) -> None`: keyed by `type_iri`; keeps `slug -> type_iri` too. Re-registering the same `type_iri` with equal options is silent; with different hooks it logs a WARNING on logger `scrud_django.core.types` naming the type, then replaces.
  - `type_options(type_iri: str) -> TypeOptions` and `type_options_for_slug(slug: str) -> TypeOptions`.
  - `requirement_for(resource_type, slug: str | None = None) -> Requirement`: the slug defaults to `resource_type.slug`; a given one that differs raises `ImproperlyConfigured`. Options are found by `resource_type.type_uri`.
  - `hooks_loaded() -> bool` and `ensure_hooks_loaded() -> None`: the latter imports `settings.ROOT_URLCONF` once (thread-safe) when no registration has run, so a process that never served a request still has its hooks. `requirement_for` calls it.
- DRF's per-request `type_hooks()` read keeps working by slug (`type_options_for_slug`).

- [ ] **Step 1: Write the failing tests**

```python
def test_requirement_for_refuses_a_mismatched_slug():
    # requirement_for(get_resource_type_for("hooked-notes"), "todos") raises ImproperlyConfigured

def test_requirement_for_defaults_the_slug():
    # requirement_for(rt) for hooked-notes has a HookLeaf

def test_replacing_different_hooks_warns(caplog):
    # register twice with different checks: one WARNING on scrud_django.core.types; equal options: none

def test_hooks_load_without_the_urlconf_imported():
    # subprocess: settings.configure(**SETTINGS); django.setup(); never import tests.urls;
    # requirement_for(<hooked-notes ResourceType>) contains a HookLeaf
```

The subprocess test follows `tests/test_core_imports.py`'s pattern and needs a database with the hooked type; if registering the type requires the URLconf import (it does: `json_resource_type` registers it), assert instead that `type_options_for_slug("hooked-notes")` has `checks` after `ensure_hooks_loaded()`, with no database access.

- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.** `ensure_hooks_loaded` imports `ROOT_URLCONF` with `importlib.import_module`; guard re-entry (the URLconf's own import calls `register_type_options`).
- [ ] **Step 4: Run** `uv run pytest tests/test_type_hooks.py tests/test_core_parity.py -q`, then the full suite.
- [ ] **Step 5: Commit** (remove `core-hooks-silently-absent`; README hooks section: one sentence on keying and the warning; release-notes line).

```bash
git commit -m "fix(core): a type's hooks are keyed by its IRI and cannot go missing silently"
```

### Task 6: Lists refuse a leaf with its own contexts; AnyOf request checks

**Files:**
- Modify: `scrud_django/core/lists.py` (`readable_queryset`, `list_last_modified`), `scrud_django/core/resources.py` (`_object_permitted`)
- Test: `tests/test_core_lists.py`, `tests/test_core_requirements.py`

**Interfaces:**
- `readable_queryset` and `list_last_modified` raise `ImproperlyConfigured` when a leaf of `requirement` has a `contexts_for` other than its class's default for reading stored contexts. Decide "custom" as: the leaf is not a `GrantsLeaf`, `HookLeaf` or `ViewLeaf`, or it is a subclass that overrides `contexts_for`. (`type(leaf).contexts_for is not <base>.contexts_for`.)
- `_object_permitted` (and `meets_object_checks` where it serves the facade) asks, for each `AnyOf` option, `has_permission(request, None)` of every entry before its object check: an option whose request check refuses is not satisfied, as DRF's `|` answers. (The facade's `check_request` from Task 3 already asks the request checks of a whole requirement; this is the per-option rule inside an `AnyOf`.)

- [ ] **Step 1: Write the failing tests**

```python
def test_readable_queryset_refuses_a_leaf_with_its_own_contexts():
    # class OwnContexts(GrantsLeaf): def contexts_for(self, obj): return ["a"]
    # readable_queryset(..., [OwnContexts(JSON_LD)]) raises ImproperlyConfigured

def test_anyof_option_refused_at_request_level_is_not_satisfied():
    # option 1: leaf refusing has_permission, allowing has_object_permission; option 2: GrantsLeaf the user lacks
    # core check_object raises (AccessDenied/Http404), and DRF's `|` of the equivalent IsAuthorized subclasses gives the same status
```

- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_core_lists.py tests/test_core_requirements.py tests/test_core_parity.py -q`, then the full suite.
- [ ] **Step 5: Commit** (remove `core-readable-queryset-stored-contexts` and `core-anyof-object-check-only`).

```bash
git commit -m "fix(core): lists refuse leaves with their own contexts; AnyOf options ask their request checks"
```

### Task 7: The type IRI on facade writes, and the small semantics

**Files:**
- Modify: `scrud_django/core/writes.py` (`create`, `update` gain `type_iri=None`), `scrud_django/core/types.py` (`HookLeaf`), `scrud_django/exceptions.py` (`ErrorText`), `scrud_django/drf_requirements.py` (`requirement_of`'s `NotNeutral` detection), `scrud_django/core/resources.py` (`readable_content(grants=False)`)
- Test: `tests/test_core_writes.py`, `tests/test_type_hooks.py`, `tests/test_core_requirements.py`, `tests/test_core_imports.py`, `tests/test_core_resources.py`

**Interfaces and rules:**
- `create(..., type_iri: str | None = None)` and `update(..., type_iri: str | None = None)` pass `type_iri` to `save_checked` (default: the type's `type_uri`).
- `HookLeaf` asks each hook and allows only on `is True`; any other return refuses. Applies to `checks`, `object_checks` and `narrow`.
- `HookLeaf.has_permission` asks the checks in the default context `""` for a body whose `rbac_context` is `[]` (as `rbac_contexts_of` reads no context), never in no context.
- `HookLeaf._as_content` raises `TypeError` naming the object's type for an object that is neither a `Mapping` nor a `Resource`.
- Without DRF, `ErrorText.__eq__` compares `code` too (and `__hash__` stays `str.__hash__`), matching `ErrorDetail`.
- `requirement_of` detects `~` by identity: `NotNeutral` when a permission object in the view's `_rbac_permissions()` is not among the requirement's leaves' wrapped permissions.
- `readable_content`'s `grants` parameter is removed from the public function (keep it on `_readable_content`).

- [ ] **Step 1: Write the failing tests** — one per rule, using the test names in `docs/follow-ups.md` `core-small-semantics` and `core-write-no-type-iri-recheck` (e.g. `test_a_truthy_non_bool_hook_refuses`: a check returning `"no"`; POST to `hooked-notes` is 403).
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the touched modules, `tests/test_core_parity.py`, then the full suite.
- [ ] **Step 5: Commit** (remove `core-small-semantics` and `core-write-no-type-iri-recheck`; README hooks section: hooks must return `True` to allow; release-notes line).

```bash
git commit -m "fix(core): hooks allow only on True; facade writes take type_iri; small semantics"
```

### Task 8: Branch verification

- [ ] Run `uv run pytest -q`, `uv run pytest -q --reverse`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run python manage.py makemigrations scrud_django --check --dry-run`, `uv run --group docs sphinx-build -W -b html docs docs/_build`. Expected: all pass.
- [ ] Run the suite on PostgreSQL 14 and MySQL 8.4 in throwaway containers (CLAUDE.md's commands). Expected: pass.
- [ ] `git diff origin/develop -- tests/` shows no `develop` assertion edited or removed.
