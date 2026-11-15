# Ninja Phase 3A Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the findings of MR !19's five-agent review: one Critical (the WorkflowPolicy lock order on ninja, which deadlocks on PostgreSQL), the Important findings, and the cheap suggestions that are parity defects or traps in public API that hasn't shipped yet.

**Architecture:** This is fix-up work on `feature/ninja-front-end` (MR !19, stacked on !18). The goal from phase 3A still holds: ninja answers byte-for-byte as DRF does, and the core holds the rules both front ends share. Each fix goes where the rule lives: lock order in the core, parity in `scrud_django/ninja/`, and checks on public types where those types are built.

**Tech Stack:** Python 3.11+, Django, DRF 3.18, django-ninja 1.7.x, scoped-rbac 0.12.x, scrudful 0.10.x, pytest, uv, ruff.

**Spec:** `docs/superpowers/specs/2026-11-11-ninja-front-end-design.md` (§3.1–3.3). The findings this plan answers are in the review reports under
`/private/tmp/claude-501/-Users-davidcharboneau-dev/2b56465c-b343-4839-a25f-ccc5e8fdae69/scratchpad/review-19/`
(`code.md`, `tests.md`, `errors.md`, `comments.md`, `types.md`). Their repro tests are in `probes/`, `probe/`, `typesrepro/` and `errtests/` there. Read a finding's section before fixing it. The report is the evidence; this plan is the decision.

## Global Constraints

- Never edit upstream packages (scoped-rbac, scrudful, safetydance, safetydance-django). Work around gaps in this repo and record them in `docs/follow-ups.md`.
- No version bump; this all goes into the held 0.16.0.
- No DRF-visible behaviour change, except where a task says so. Any such change goes in `docs/release-notes.md`.
- Supported databases: SQLite, PostgreSQL 14, MySQL 8.4. A task that touches locking or transactions runs its tests on PostgreSQL 14 and MySQL 8.4 in throwaway docker containers, and removes them afterwards.
- Every commit message ends with `Claude-Session: https://claude.ai/code/session_01SAZeWvu8MUgWeP2fTMBG43`.
- `uv run pytest -q`, `uv run ruff check .` and `uv run ruff format --check .` pass at the end of every task.
- `uv run --with mypy mypy scrud_django/core --ignore-missing-imports --follow-imports=silent` is clean at the end of Task 4 and stays clean after it.

## Review Focus

1. A ninja write of a WorkflowPolicy running at the same time as a server-side write of the same policy (`ResourceRegistration.update`, a bulk delete, or a sync repair) must not deadlock on PostgreSQL. The expected result is the same as DRF's. Task 1 adds the regression test.
2. A `TypeSpec` derived with `dataclasses.replace` must not register with the hooks of one type and the stored row of another. Task 4 makes that unrepresentable.
3. A ninja API given a slug that is already taken must refuse before it writes anything process-wide. Task 5 tests this.
4. A test parametrized over `front_end` must really run the request on ninja for the ninja case: no `reverse()` of DRF route names, and no `force_authenticate`. Tasks 1–3 port or add such tests, and each task's review checks this.
5. Ninja clients that authenticate with HTTP Basic or a token get the documented answer: anonymous, unless a `NINJA_USER` hook maps them. Task 6 pins this.

## Rulings made while writing the plan (controller)

- **Ruling A (auth):** ninja keeps spec §3.2's authentication: scoped-rbac's `resolve_user`, meaning the `NINJA_USER` hook, then `request.auth`, then the session. It does not run DRF's `DEFAULT_AUTHENTICATION_CLASSES`. Task 6:
  - documents this in the README;
  - pins it with tests;
  - adds a tested README example of a `NINJA_USER` hook for Basic credentials;
  - records two items in follow-ups: the §3.3 omission, and an optional hook scrud could provide that runs DRF's authenticators, with part C as the place to decide it.

  **Why:** the spec defines ninja auth this way, and making ninja run DRF's authenticators ties it to DRF just as part C moves DRF into an extra. **Cost if wrong:** a follow-up that adds the optional hook.
- **Ruling B (`TypeSpec` shape):** before 0.16.0 ships, `TypeSpec` becomes keyword-only. Its IRI and slug have a single source of truth, and the arguments it stores for registration become private. **Why:** this API hasn't shipped, so fixing it now is free and fixing it later is a breaking change. **Cost if wrong:** tests and README examples that construct `TypeSpec` positionally have to be changed. There are none outside this repo.
- **Ruling C (left out):** these review suggestions get follow-ups, not code:
  - `ATOMIC_REQUESTS` rollback in ninja's error handlers (errors S6; not reachable through scrud's routes);
  - logging when a link falls back across namespaces (errors S4);
  - `computed_link(None)` (types S13);
  - `entry`'s write database for part B's own models (types S10, part B note).

  **Why:** none of them is reachable through scrud's routes today, or each belongs to part B. **Cost if wrong:** one later task each.

---

### Task 1: Take the WorkflowPolicy type lock before the row lock on every front end (Critical)

**Files:**
- Modify: `scrud_django/core/resources.py` (`load_for_read`, around 340-360)
- Modify: `tests/test_atomic_writes.py` (`recorded_write_order` ~475, `test_a_policy_write_locks_the_type_before_it_reads_the_row` ~514, `test_a_write_reads_the_row_inside_its_transaction`)
- Modify: `tests/test_delete_locking.py`. These tests become parametrized over `front_end`: `test_a_bulk_delete_of_a_policy_and_its_api_put_do_not_deadlock` (~492), `test_a_sync_repair_of_a_policy_and_its_api_put_do_not_deadlock` (~667), `test_a_delete_of_a_policy_holding_its_row_and_an_api_put_do_not_deadlock` (~716), `test_a_save_moving_a_policy_and_its_api_put_do_not_deadlock` (~763), `test_the_documented_server_side_policy_write_and_an_api_put_do_not_deadlock` (~885), and `test_an_unlocked_delete_waits_for_a_locked_api_write` (~456).

**Interfaces:**
- Consumes:
  - `scrud_django.registration.lock_if_workflow_policy(resource_type, using)`;
  - the `front_end` fixture and `api_url` helpers in `tests/conftest.py` and `tests/front_ends.py`.
- Produces: `core.resources.load_for_read(..., lock_using=db)` takes `lock_if_workflow_policy(resource_type, db)` before its `select_for_update()`. Its signature is unchanged.

- [ ] **Step 1: Make the lock-order tests run on both front ends, and see them fail on ninja.**
  - Parametrize the two `test_atomic_writes.py` tests listed above, and the `test_delete_locking.py` tests listed above, over `front_end`. Use `api_url`-style paths and `force_login` instead of `reverse()` and `force_authenticate`, the way the other suites ported in phase 3A do.
  - `recorded_write_order` must patch every reference to `lock_workflow_policy_type`: the one in `scrud_django.registration`, the one in `scrud_django.core.writes` (which imports its own reference) and the one in `scrud_django.workflow`. Otherwise the ninja run records no type lock and fails for the wrong reason (`tests.md` C1).
  - Expected for ninja PUT and DELETE: events start with `("lock", True)` before the first `("select", True)`.
- [ ] **Step 2: Run them.**
  - Run: `uv run pytest tests/test_atomic_writes.py -q -k "locks_the_type or reads_the_row"`.
  - Expected: the ninja cases FAIL with `select` before `lock`, and the DRF cases PASS.
- [ ] **Step 3: Take the lock in `load_for_read`.**
  - When `lock_using` is not `None`, call `lock_if_workflow_policy(resource_type, lock_using)` immediately before the `select_for_update()` query.
  - Import it the way `core/writes.py` already imports from `scrud_django.registration`.
  - DRF's path takes the same lock earlier, in `mixins/view_mixins.py:1106`. Taking the lock twice inside one transaction is harmless, as DRF's own trace shows (`tests.md` C1, last note).
- [ ] **Step 4: Run the tests on SQLite, PostgreSQL 14 and MySQL 8.4.**
  - Run: `uv run pytest tests/test_atomic_writes.py tests/test_delete_locking.py -q` on each database, using the settings the repo's CI uses for PostgreSQL and MySQL (see `.gitlab-ci.yml`).
  - Expected: all pass. The `test_delete_locking.py` two-writer tests skip on SQLite, as they do today.
  - On PostgreSQL, the scenario in `probes/test_probe_pg_deadlock.py` must give the DRF-equal result on ninja. Make that scenario a permanent test in `tests/test_delete_locking.py` if no existing test already reproduces it: a ninja PUT paused after its row lookup while `ResourceRegistration.update` writes the same policy.
- [ ] **Step 5: Commit**: `fix(core): take the WorkflowPolicy type lock before the row lock on every front end`.

### Task 2: Run the write-path invariant suites on both front ends

**Files:**
- Modify: `tests/test_atomic_writes.py`, `tests/test_post_commit.py`, `tests/test_delete_locking.py`. Only the HTTP-level tests change. Tests that patch DRF view internals (`LockedWriteMixin`, viewset methods) stay DRF-only, with a one-line comment saying why.

**Interfaces:**
- Consumes: the `front_end` fixture; the ports already working in the probes `probes/test_probe_atomic.py` and `probes/test_probe_post_commit.py` under the review directory.
- Produces: nothing new.

- [ ] **Step 1: Parametrize over `front_end`** every HTTP-level test in the three modules that Task 1 has not already ported. That covers:
  - a stale `If-Match` under a concurrent write gives 412;
  - authorization against the stored context gives 404;
  - no `Resource` or `ResourceType` read goes through the read router;
  - a write refused after its save runs no workflow action or indexing;
  - side effects run once, after commit.
- [ ] **Step 2: Run** `uv run pytest tests/test_atomic_writes.py tests/test_post_commit.py tests/test_delete_locking.py -q` on SQLite, PostgreSQL 14 and MySQL 8.4. Expected: all pass. A ninja failure is a real defect: fix it in `scrud_django/ninja/` or the core, not in the test.
- [ ] **Step 3: Commit**: `test: run the write-path invariant suites on both front ends`.

### Task 3: Close the parity gaps the harness hid

**Files:**
- Modify: `scrud_django/ninja/computed.py` (~78-81), `scrud_django/ninja/routers.py` (`vary_on_accept` ~601-635), `scrud_django/ninja/negotiation.py` (`select` ~102-124), `scrud_django/ninja/errors.py` (`_native` ~155-159)
- Modify: `tests/front_ends.py` (`IGNORED_HEADERS` ~58, its docstring and its self-test), `tests/test_front_end_parity.py`

**Interfaces:**
- Consumes: the harness in `tests/front_ends.py`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing parity cases**, each running with uniform errors on and off:
  - `computed-compute-403` and `computed-compute-404`, with and without a message: a computed type whose `compute` raises `PermissionDenied("nope")` or `Http404("nope")`. DRF answers `"detail":"nope"` (`errors.md` I1).
  - Preflights of an item, a list and `services/`: their `Vary` is compared directly. DRF sends no `Vary` (`tests.md` S2).
  - `Accept: application/json; indent=4` on an item, a list, `services/` and `collections-json-schema/`: the bodies are equal (`code.md` S1).
  - `_read_cases` for the `json-ld` document type, as is already done for `json-schema` (`tests.md` S5).
- [ ] **Step 2: Run** `uv run pytest tests/test_front_end_parity.py -q`. Expected: the new cases FAIL on the body or on `Vary`.
- [ ] **Step 3: Fix:**
  - In the computed operation, re-raise `Http404` as `HttpError(404, str(exc))` when it has a message, the way `routers.py:332-336` already does for collection documents. Re-raise `PermissionDenied` with its message as DRF's handler answers it. The goal is equal bytes, so check both with and without a message.
  - `vary_on_accept` leaves preflights alone (`is_preflight`).
  - Negotiation keeps the accepted media type's parameters, and rendering passes `indent` through the way DRF's `JSONRenderer.get_indent` does.
  - `_native` keeps a link that has no `>` instead of raising (`link.find`).
- [ ] **Step 4: Tighten the harness:**
  - Remove `WWW-Authenticate` and `Set-Cookie` from `IGNORED_HEADERS`, its docstring and its self-test (`tests.md` S1: never needed).
  - `test_failing_credentials_are_refused_alike` states why its `Vary` differs, or compares `Vary` explicitly (`tests.md` S3).
  - In `parity`, assert that both runs stored the same list keys, so the MySQL `AUTO_INCREMENT` drift fails clearly (`tests.md` S4).
  - Tighten `test_options_refuses_a_requester_failing_authentication`'s precondition so a 401 doesn't pass (`tests.md` S5).
- [ ] **Step 5: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 6: Commit**: `fix(ninja): answer computed errors, preflight Vary and Accept indent as DRF does`.

### Task 4: Make invalid `TypeSpec`s unrepresentable, and get the core type-clean

**Files:**
- Modify: `scrud_django/registration.py` (`TypeSpec` ~826-900, `json_resource_type_spec` ~904-930, `computed_resource_type_spec` ~950-970, `built_in_type_specs` ~1143-1180, `ResourceTypeRegistration.__init__` ~305-313, `ROLE_LIST_ROUTES` ~1143)
- Modify: `scrud_django/core/types.py` (`TypeOptions.__post_init__` ~84), `scrud_django/core/lists.py` (`requested_page` ~483, `ListPage` ~447, `list_queryset` ~311, `create_list_validators` ~618), `scrud_django/core/urls.py` (`first_absolute_reverse` ~42), `scrud_django/core/resources.py` (`allowed_methods_for` ~680-690)
- Modify: `scrud_django/ninja/negotiation.py` (`DOCUMENT_RENDERERS` is keyed from the new constant)
- Test: `tests/test_type_specs.py`, `tests/test_core_lists.py`, `tests/test_core_urls.py`, `tests/test_core_resources.py`

**Interfaces:**
- Produces, used by Task 5:
  - `TypeSpec` is `@dataclass(frozen=True, kw_only=True)` with these public fields: `kind: Literal["json", "computed"]`, `resource_type_uri: str`, `slug: str`, `options: TypeOptions`, `document_media_type: DocumentMediaType | None`.
  - The arguments stored for registration are private and not passed to `__init__`. They are built by `json_resource_type_spec`, `computed_resource_type_spec` and `built_in_type_specs` through one private constructor.
  - `TypeSpec` is hashable.
  - `TypeSpec.__post_init__` raises `ImproperlyConfigured` when:
    - `kind` is not one of the two values;
    - `kind == "computed"` without `options.compute`, or `kind == "json"` with one;
    - `document_media_type` is not in `DOCUMENT_MEDIA_TYPES`;
    - a built-in spec has non-empty `options`.
  - `DOCUMENT_MEDIA_TYPES: frozenset[str] = frozenset({"application/schema+json", "application/ld+json"})` is defined in `scrud_django/registration.py`, and `negotiation.DOCUMENT_RENDERERS` is keyed from it.
  - `dataclasses.replace(spec, resource_type_uri=X)` gives a spec whose stored row and hooks both use `X`, or it raises. It never splits them.

- [ ] **Step 1: Write the failing tests** in `tests/test_type_specs.py`. Port the repros in `typesrepro/` (`types.md` I1, I3, I4, I6, S1, S2, S4):
  - `replace` of the URI keeps the hooks with the stored row;
  - `replace` of the slug stores and routes the same slug;
  - positional construction raises `TypeError`;
  - a bad `kind` raises;
  - `kind`/`compute` mismatches raise;
  - `document_media_type="text/csv"` raises;
  - `replace(built_in, options=...)` raises;
  - `hash(spec)` works;
  - `computed_resource_type_spec(..., compute="nope")` raises;
  - `json_resource_type_spec` no longer takes `indexing_policy_resource`, and its configuration arguments are keyword-only.
- [ ] **Step 2: Write the failing core tests:**
  - `ListPage(size=0, ...)`, `number=0` and `count=-1` raise `ValueError`, and `items` is stored as a tuple (`types.md` S6);
  - `list_queryset` and `create_list_validators` default to ignoring `ignored_list_parameters()` (`types.md` S7);
  - `first_absolute_reverse("role-list")` raises `TypeError` (`types.md` S8);
  - `allowed_methods_for` calls a `checks` hook once per method (`types.md` S9);
  - `ResourceTypeRegistration(build_urls=False, viewset_class=X)` raises `TypeError` (`types.md` S5).
- [ ] **Step 3: Run** these tests. Expected: FAIL.
- [ ] **Step 4: Implement** to the Interfaces block. Other changes:
  - `requested_page` gives `ListPage` an `int` size, by overloading `page_size_of` or using `or DEFAULT_PAGE_SIZE`. This is no behaviour change.
  - `TypeOptions.__post_init__` checks that `compute` is callable.
  - `ROLE_LIST_ROUTES` becomes a `MappingProxyType`.
  - `allowed_methods_for` passes the `allowed` it has already computed into a private helper.
  - Every existing caller and test that builds `TypeSpec` changes to the keyword form.
- [ ] **Step 5: Run** `uv run pytest -q` and the mypy command from the Global Constraints. Expected: all pass, and mypy reports `Success`.
- [ ] **Step 6: Commit**: `fix(registration): make invalid TypeSpecs unrepresentable; core type-clean`.

### Task 5: `api()` refuses what it can't serve, before it registers anything

**Files:**
- Modify: `scrud_django/ninja/api.py` (~81-118), `scrud_django/ninja/entry.py` (~30-90), `scrud_django/ninja/__init__.py` (~3-8)
- Test: `tests/test_ninja_api.py`, `tests/test_ninja_imports.py`

**Interfaces:**
- Consumes: Task 4's `TypeSpec` and its public fields.
- Produces:
  - `api(types=...)` checks every element and every slug before any `register()` call;
  - `entry` checks at decoration time that it wraps an `Operation.run`.

- [ ] **Step 1: Write the failing tests:**
  - `api(types=[json_resource_type_spec(URI, slug="json-schema")])` raises `ImproperlyConfigured` naming both IRIs. Afterwards, no new `ResourceType` row exists and `core.types._SLUG_IRIS["json-schema"]` is unchanged.
  - The same applies to two user specs with one slug, and to a slug equal to a fixed prefix: `services`, scoped-rbac's `roles`, `role-assignments` and `user-rbac-policy`, and the `routers.COLLECTION_ROUTES` prefixes (`types.md` I2).
  - An element that isn't a `TypeSpec` raises `TypeError` pointing at `json_resource_type_spec` (`types.md` S11).
  - `router.add_decorator(entry, mode="operation")` raises `TypeError` saying to use `mode="view"` (`types.md` S10, `errors.md` S2).
  - An OPTIONS that isn't a CORS preflight, on an app router decorated with `entry`, resolves the user, so a refusing `NINJA_USER` hook gets 401 (`errors.md` S3).
  - A django-ninja that is installed but fails to import re-raises its own error, not "needs django-ninja" (`errors.md` S1).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement:**
  - `api()` collects `[*built_in_type_specs(), *types]`, checks them and their slugs, then registers.
  - Route dispatch ends in `else: raise ImproperlyConfigured(...)`. This can't be reached after Task 4, but it stays as a guard.
  - `entry` uses `resolve_user` for a real OPTIONS. `_api_of` resolves outside the `except`.
  - `__init__` follows `exceptions._drf_is_absent`'s pattern.
- [ ] **Step 4: Run** `uv run pytest -q`. Expected: all pass.
- [ ] **Step 5: Commit**: `fix(ninja): api() and entry refuse what they cannot serve, before any side effect`.

### Task 6: Pin and document ninja's authentication; correct the docs

**Files:**
- Modify: `README.md` (django-ninja section, ~60-110), `CLAUDE.md` (Architecture paragraph on the ninja adapter, line ~154), `docs/follow-ups.md`, `docs/release-notes.md`, and the docstrings named in `comments.md`
- Test: `tests/test_ninja_api.py` (or a new `tests/test_ninja_auth.py`)

**Interfaces:**
- Consumes: Ruling A and the final code of Tasks 1–5.
- Produces: nothing new.

- [ ] **Step 1: Write the auth tests** (`tests.md` I3, `code.md` I1):
  - On ninja, valid Basic credentials alone are the anonymous user: `services/` gives 200 with the anonymous user's grants, and an item GET gives 401.
  - With the README's example `NINJA_USER` hook set (it decodes Basic credentials and returns the active user, else `None`), the same item GET gives 200, and a PUT with no CSRF token gives 200.
  - The test imports the hook from one place, a module under `tests/`, whose text the README shows verbatim. Wrong credentials under the hook give 401 when the hook raises `AuthenticationError`.
- [ ] **Step 2: Run** them. Expected: PASS. This is documentation of current behaviour; if a test fails, report it rather than changing the code.
- [ ] **Step 3: Correct the docs**, one item per finding in `comments.md`:
  - **I1:** `entry` runs writes in one transaction only for PUT, PATCH and DELETE, and for a conditional POST.
  - **I2:** operation ids are unique except for each GET and its HEAD. Point to the follow-up `ninja-openapi-head-operation-id`.
  - **I3, I4 and I5:** move those three follow-ups to Decided/Done, each with the commit that fixed it.
  - Make every comments-report Suggestion that is still true after Tasks 1–5.
  - Add to the README's ninja section:
    - the authentication rule (Ruling A) with the hook example;
    - `compute` receives a plain Django `HttpRequest` on ninja (`code.md` S2).
  - Widen CLAUDE.md's list of core modules (`code.md` S3).
  - In CLAUDE.md, say that the demo's "Basic auth" applies to DRF.
  - Update follow-up `core-type-check-ci` to the state after Task 4.
  - Add follow-ups: `ninja-drf-authenticators` (Ruling A, including the §3.3 omission for the owner), `ninja-atomic-requests-rollback`, `ninja-namespace-fallback-logging` and `ninja-entry-write-db` (Ruling C).
  - Add release notes for the `TypeSpec` shape (Ruling B) and for the lock-order fix.
- [ ] **Step 4: Run** `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`. If the repo builds docs, build them too. Expected: all pass.
- [ ] **Step 5: Commit**: `docs: ninja authentication, entry transactions and review follow-ups`.
