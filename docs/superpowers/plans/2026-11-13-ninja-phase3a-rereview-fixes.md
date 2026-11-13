# Ninja Phase 3A Re-review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix what MR !19's second five-agent review found after the first fix round (`docs/superpowers/plans/2026-11-13-ninja-phase3a-review-fixes.md`): the Basic-auth hook example, error bodies when uniform errors are configured off, the `TypeSpec` states that are still invalid, the tests that the harness or timing let pass, and stale docs.

**Architecture:** Fix-up work on `feature/ninja-front-end` (MR !19). Phase 3A's goal still holds: ninja answers byte-for-byte as DRF does.

**Tech Stack:** Python 3.11+, Django, DRF 3.18, django-ninja 1.7.x, scoped-rbac 0.12.x, scrudful 0.10.x, pytest, uv, ruff.

**Spec:** `docs/superpowers/specs/2026-11-11-ninja-front-end-design.md` (§3.1–3.3). The evidence is in the re-review reports at
`/private/tmp/claude-501/-Users-davidcharboneau-dev/2b56465c-b343-4839-a25f-ccc5e8fdae69/scratchpad/review-19b/`
(`code.md`, `tests.md`, `errors.md`, `comments.md`, `types.md`), with probes in `errtests/`, `typesprobe/`, `comments-probe/`, `probe/` and `probes/`. Read a finding's section before you fix it.

## Global Constraints

- Never edit upstream packages (scoped-rbac, scrudful, safetydance, safetydance-django). Record any upstream gap in `docs/follow-ups.md`.
- No version bump; this goes into the held 0.16.0.
- No change to DRF's behaviour that a user could see. The one exception is a task that says so, and that change goes in `docs/release-notes.md`.
- Supported databases are SQLite, PostgreSQL 14 and MySQL 8.4. A task that touches locking, transactions or the concurrency tests runs them on PostgreSQL 14 and MySQL 8.4, in throwaway docker containers that it removes afterwards.
- Every commit message ends with `Claude-Session: https://claude.ai/code/session_01SAZeWvu8MUgWeP2fTMBG43`.
- `uv run pytest -q`, `uv run ruff check .` and `uv run ruff format --check .` pass at the end of every task. So does `uv run --with mypy mypy scrud_django/core --ignore-missing-imports --follow-imports=silent`.
- Ruling A of the first fix plan stands. ninja authenticates with scoped-rbac's `resolve_user` and does not run DRF's authenticators.

## Review Focus

1. A user logged in by session must stay that user on ninja when the README's `NINJA_USER` hook is set: a GET returns 200, and a PUT without a CSRF token returns 403. Task 1 tests this.
2. A project built with `SCOPED_RBAC["UNIFORM_ERRORS"] = False` gets DRF's error bodies from ninja. Task 2 tests this.
3. `TypeSpec` must not be buildable, through any public route, as a spec whose authorization hooks are never asked, or whose slug can shadow another route. Task 3 tests this.
4. A two-writer test must fail when the lock order it guards is reversed. When the race doesn't happen, it must fail rather than pass without testing anything. Task 4 handles this.

---

### Task 1: The Basic-auth hook keeps session users

**Files:**
- Modify: `tests/basic_auth_hook.py`, `README.md` (the hook example and its prose, about lines 82-120, kept verbatim with the module by the existing test)
- Modify: `tests/test_ninja_auth.py`
- Modify: `docs/follow-ups.md`

**Interfaces:** none new.

- [ ] **Step 1: Write the failing tests** in `tests/test_ninja_auth.py`, with the hook set:
  - a user logged in by session, with no `Authorization` header, gets 200 on a ninja item GET;
  - the same user's PUT with no CSRF token gets 403, with `CSRF Failed` in the body;
  - a client that sends both a session cookie and that same user's Basic credentials, with no CSRF token, gets 403 on a PUT.
  
  The evidence is in `code.md` I1 and S1 and `errors.md` N2. The probes are `probe/test_probe_hook_session.py`, `errtests/test_hook_session.py` and `comments-probe/test_probe_hook_csrf.py`.
- [ ] **Step 2: Run them.** Expected: the session GET fails with 401.
- [ ] **Step 3: Change the hook so that with no Basic header it returns `getattr(request, "user", None)`.**
  - Correct the README:
    - The hook replaces the session lookup; it does not add to it. So the hook must return the session user itself.
    - A hook's user who is also the request's session user is checked for CSRF.
    - A Basic-only client with no session cookie needs no token.
    - Basic credentials belong only over TLS.
  - Correct the comment at `tests/test_ninja_auth.py:~66`.
  - Add follow-up `scoped-rbac-ninja-user-hook-csrf-doc`. It records that scoped-rbac's `conf.ninja_user_hook` docstring says a hook's user skips the CSRF check, which is not true for a session user. It is an upstream docs proposal.
- [ ] **Step 4: Run** `uv run pytest tests/test_ninja_auth.py -q`, then the full gates. Expected: all pass.
- [ ] **Step 5: Commit**: `fix(docs): the Basic-auth NINJA_USER hook keeps session users`.

### Task 2: ninja answers DRF's error bodies when uniform errors are configured off

**Files:**
- Modify: `scrud_django/ninja/errors.py` (`install`, around lines 39-68, and `_answering_natively` / `_native` if needed), `scrud_django/ninja/computed.py` (`_as_drf_answers`)
- Create: a URLconf module for an API built with uniform errors off, under `tests/` (for example `tests/ninja_off_urls.py`)
- Test: `tests/test_front_end_parity.py`, or a new `tests/test_ninja_uniform_off.py`
- Modify: `docs/follow-ups.md` (`ninja-uniform-errors-per-front-end`), `docs/release-notes.md`

**Interfaces:** none new. `scrud_django.ninja.api()` keeps its signature.

- [ ] **Step 1: Write the failing tests.**
  - DRF and a ninja API built while `SCOPED_RBAC["UNIFORM_ERRORS"]` is `False` must give the same bytes, with uniform errors also off for each request, for:
    - an anonymous GET of an item and of a computed resource (401);
    - a PATCH of a computed route (405);
    - a `compute` raising a bare `Http404()` (404);
    - every case in the existing uniform-errors corpus that can run against that API.
  - Also: `compute` raising `PermissionDenied("")` and `Http404("")`, and a message that is not a string (`errors.md` S1), compared with DRF with uniform errors both on and off.
  - The evidence is in `errors.md` N1 and S1, and the probe is `errtests/test_built_off.py` with `errtests/off_urls.py`.
  - Build the off API at import time of its own URLconf, under `override_settings`, or with whatever mechanism the existing `ninja_api.py` / `ninja_urls.py` pattern supports. Then route requests to it with `pytest.mark.urls` or the harness's mount mechanism.
- [ ] **Step 2: Run them.** Expected: the 401, 405 and bare-404 cases fail on the body text.
- [ ] **Step 3: Fix.**
  - `errors.install` always installs scoped-rbac's uniform handler set, whatever `UNIFORM_ERRORS` was when the API was built. Do this with a small local settings shim around `install_error_handlers(api)`, without importing from `django.test` in library code.
  - The existing per-request translation (`answer_natively_when_off`) then produces DRF's native bodies.
  - `_as_drf_answers` matches DRF for an empty message and for a message that is not a string.
- [ ] **Step 4: Run** the new tests, `tests/test_front_end_parity.py`, `tests/test_error_media_types.py` and then the full gates. Expected: all pass.
- [ ] **Step 5: Update the docs.**
  - Close follow-up `ninja-uniform-errors-per-front-end` (move it to Done, citing the commit).
  - Make the release notes' parity claim accurate.
- [ ] **Step 6: Commit**: `fix(ninja): DRF's error bodies when uniform errors are configured off`.

### Task 3: `TypeSpec` refuses its remaining invalid states

**Files:**
- Modify: `scrud_django/registration.py` (`TypeSpec.__post_init__`, `json_resource_type_spec`, `ResourceTypeRegistration.register_urls` around line 321)
- Test: `tests/test_type_specs.py`, `tests/test_ninja_api.py`

**Interfaces:**
- `TypeSpec.__post_init__` additionally raises `ImproperlyConfigured` in these cases:
  - `kind == "computed"` with any `TypeOptions` field other than `compute` set to a value different from its default;
  - `slug` is not a `str` matching `django.core.validators.slug_re`;
  - `resource_type_uri` is not a non-empty `str`;
  - a spec that is not a built-in uses the IRI or the slug of a built-in.
- `json_resource_type_spec` checks, when the spec is built, that the callables it stores are callable (`types.md` S-C).
- `ResourceTypeRegistration(build_urls=False).register_urls()` raises `TypeError` with a message, instead of an `AttributeError` (`types.md` S-F).

- [ ] **Step 1: Write the failing tests** for every case in Interfaces, built both through the public constructor and through `dataclasses.replace`.
  - Include the end-to-end case from `types.md` N2 / `errors.md` N3: `api(types=[json_resource_type_spec(uri, "")])` raises before any side effect. Use `_refused` in `tests/test_ninja_api.py`, which already checks for side effects.
  - Include `"{pk}"`, `"a/b"`, `None` and `""` as slugs.
  - Include a computed spec with `object_checks=(deny,)` (`types.md` N1; the probes are in `typesprobe/`).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** what the Interfaces block describes. Every existing spec in the repo, built-in or in tests, must stay valid.
- [ ] **Step 4: Run** the full gates. Expected: all pass, and mypy reports Success.
- [ ] **Step 5: Commit**: `fix(registration): refuse computed hooks, bad slugs and built-in identities in TypeSpec`.

### Task 4: Make the harness and the concurrency tests catch what they guard

**Files:**
- Modify: `tests/front_ends.py` (`IGNORED_HEADERS`, `parity`), `tests/test_front_end_parity.py`
- Modify: `tests/test_delete_locking.py` (the five older two-writer policy tests, around lines 493-893; `test_an_unlocked_delete_waits_for_a_locked_api_write`; `test_an_api_put_of_a_policy_holding_its_row_and_a_server_update_do_not_deadlock`)
- Modify: the preflight test `test_a_preflight_on_an_entry_router_does_not_resolve_the_user`, and `tests/test_ninja_imports.py`

**Interfaces:**
- `tests.front_ends.parity` compares `Vary` by default.
- A case can opt out with an explicit per-case flag whose name says why. Only two kinds of case use it: the harness self-test, and the failing-credentials cases (the `Vary: Cookie` difference from Ruling A). Each opt-out asserts the exact `Vary` difference.

- [ ] **Step 1: Make the harness compare `Vary`.** Remove `"vary"` from `IGNORED_HEADERS` and add the opt-out (`tests.md` S1). Run `uv run pytest tests/test_front_end_parity.py -q`. Expected: only the cases named above fail before the opt-out is applied. Any other failure is a parity defect: report it, or fix it in `scrud_django/ninja/` if the fix is small and obvious.
- [ ] **Step 2: Fix the two-writer tests (`tests.md` S2, S3).**
  - The five older tests: correct their comments, which describe DRF's lock order. Either make the API writer start first, so that their ninja cases tell the lock orders apart, or state in the comment that the API-first test is the one that guards ninja's order. Prefer making them discriminate, if that keeps each test's own purpose.
  - `test_an_unlocked_delete_waits_for_a_locked_api_write`: start the other writer on a `threading.Event` that is set once the API holds its lock, not after `delay=0.25`.
  - The new deadlock test: assert that the PUT had not finished when the server update started. If the race did not happen, the test fails instead of passing without testing anything.
- [ ] **Step 3: Small fixes (`tests.md` S4).**
  - The preflight test asserts the exact expected status, not `!= 401`.
  - `tests/test_ninja_imports.py` adds a `ModuleNotFoundError` for one of ninja's own dependencies, such as pydantic, and checks that it is re-raised unchanged.
- [ ] **Step 4: Run** `tests/test_delete_locking.py`, `tests/test_atomic_writes.py` and `tests/test_conditional_create.py` on SQLite, PostgreSQL 14 and MySQL 8.4. Then reverse the lock order in a scratch change to `core/resources.py`, run them again, and confirm that each two-writer test you changed now fails on ninja on PostgreSQL. Revert the scratch change and record the evidence in your report. Then run the full gates.
- [ ] **Step 5: Commit**: `test: compare Vary in the parity harness; two-writer tests discriminate lock order`.

### Task 5: Correct the remaining docs and comments

**Files:**
- Modify: `scrud_django/ninja/routers.py` (around lines 271, 350 and 360), `scrud_django/ninja/services.py` (around line 76), `scrud_django/ninja/api.py` (around lines 89 and 125), `scrud_django/registration.py` (the `computed_resource_type_spec` and `TypeSpec` docstrings), `README.md`, `CLAUDE.md`, `docs/release-notes.md`, `docs/follow-ups.md`

**Interfaces:** none.

- [ ] **Step 1: Apply `comments.md` N2, N3, and every Suggestion that is still true after Tasks 1–4.** In particular:
  - the four OPTIONS comments;
  - the claims in `api.py` that the OpenAPI document is valid (`types.md` baseline I2 partial);
  - follow-up `ninja-computed-link-none`, rewritten to its true premise: DRF's computed view passes `None`;
  - the exception named in `computed_resource_type_spec`'s docstring;
  - follow-ups that are fixed go under the file's Done section, not "# Decided";
  - `scoped-rbac-checks-before-405` goes back to open as an upstream proposal, if it is one;
  - the unclosed parenthesis at `CLAUDE.md:158-160`;
  - the release notes' wording on preflights and on `entry`'s `mode="operation"` refusal (it happens when the router is mounted).
- [ ] **Step 2: README.** A `compute` that raises DRF's own `APIException` gives a 500 on ninja (`errors.md` S2). Also describe `TypeSpec`'s new refusals from Task 3 where the README describes `TypeSpec`.
- [ ] **Step 3: Add follow-ups** for the items parked by the controller's Ruling D (below).
- [ ] **Step 4: Run** the full gates and the docs build (`.gitlab-ci.yml` `docs` job). Expected: all pass.
- [ ] **Step 5: Commit**: `docs: correct ninja OPTIONS, OpenAPI and follow-up notes after re-review`.

## Rulings made while writing the plan (controller)

- **Ruling D (parked):** these become follow-ups (Task 5, Step 3) and are not coded now:
  - `errors.md` S3: ninja's computed GET reads the type before `compute` and on a 304. This changes queries only, not answers.
  - `types.md` S-B: specs built by the helpers can't be copied, pickled or turned into dicts.
  - `types.md` S-D: the media-type set is declared twice.
  - `types.md` S-E: `RESERVED_SLUGS` is public, copies scoped-rbac's prefixes, and leaves out the docs routes.
  - `types.md` S-G: nits.

  **Why:** none of them changes an answer, and none is a fail-open. **Cost if wrong:** one later task each.
