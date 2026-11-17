# Ninja Default (phase 3, part C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make django-ninja scrud-django's default front end: `scrud_django.urls`
serves ninja, DRF becomes the optional `drf` extra under `scrud_django.drf`, the
UI app and studio work on either front end, and a project without DRF installed
runs the API, the UI and the studio.

**Architecture:** First remove the stray DRF reach from neutral code and make the
UI find a type's API routes in any namespace (a core route registry both front
ends fill). Then serve the UI and studio types on ninja through the core hooks
phase 2 added, with parity against DRF. Then move the DRF adapter into
`scrud_django.drf` behind deprecated aliases, split the packaging, flip
`scrud_django.urls`, and prove a no-DRF install in CI.

**Tech Stack:** Django 5, django-ninja 1.7.x (pinned `<1.7.4` upstream),
scoped-rbac 0.12.1, scrudful 0.10.1, DRF 3.15+ (now optional), safetydance tests.

**Spec:** `docs/superpowers/specs/2026-11-11-ninja-front-end-design.md` §3
(Phase 3), "Testing" (Phase 3), "Breaking changes". Fact-finding report
(read-only reference, line numbers as of b9f5bc4):
`/private/tmp/claude-501/-Users-davidcharboneau-dev/2b56465c-b343-4839-a25f-ccc5e8fdae69/scratchpad/partC-facts.md`.

## Owner decisions (2026-11-17) and rulings this plan carries

- **(owner) DRF becomes the `drf` extra in 0.17.0; ninja is a base dependency.**
  Base: `django-scoped-rbac[ninja]`, `scrudful[ninja]` (bringing
  `django-ninja>=1.7.0,<1.7.4`). `drf` extra: `django-scoped-rbac[drf]`,
  `scrudful[drf]`, `djangorestframework>=3.15`, `django-filter>=25.1`.
- **(owner) An opt-in DRF-authentication hook ships:** a `NINJA_USER` hook in
  `scrud_django.drf` that runs the project's DRF authenticators, enabled with one
  setting; the migration notes name it.
- **(owner) Every README-documented DRF import path stays one release as an
  alias raising `DeprecationWarning`** (`scrud_django.views`,
  `scrud_django.exceptions.scrud_exception_handler`, `scrud_django.computed`,
  `scrud_django.permissions`, `scrud_django.filters`,
  `scrud_django.mixins.view_mixins`, `scrud_django.paginations`,
  `scrud_django.serializers`, `scrud_django.renderers`, `scrud_django.validators`
  DRF parts, `scrud_django.drf_requirements`, `scrud_django.mixins.serializer_mixins`,
  `scrud_django.ui.views`, `scrud_django.ui.branches.views`).
- **(owner) The demo serves ninja at `/api/` and signs in with Django's own login
  view; it needs no DRF.**
- **(owner, 2026-11-17)** `/` stays main's UI; per-prefix branch JSON stays gone
  (migration notes point to `/api/ui-branch-items/<branch>/`).
- **Ruling: switch only after the UI and studio types are served on ninja**
  (otherwise the default install's UI has nothing to serve).
- **Ruling: R1 of the 3A plan stands** (no `first`/`last` envelope change, no
  page-1 `?page=1`): strike them from spec §3.2 and its Breaking list.
- **Ruling: no `NinjaAPI(auth=)` system check** beyond `api()`'s `TypeError`
  (R6); `scrud_django.urls`'s API keeps the default namespace `"scrud"`, and the
  docs say an app building its own `api()` replaces the include (two scrud APIs
  need distinct namespaces).
- **Ruling: ninja's renderer behaviour is unchanged** without DRF (an HTML
  Accept answers JSON, as today; follow-up `ninja-renderer-settings` notes it).

## Global Constraints

- Never edit upstream packages (scoped-rbac, scrudful, safetydance,
  safetydance-django, django-ninja). Record gaps in `docs/follow-ups.md`.
- No version bump; release notes under `## Unreleased` (0.17.0). `develop` is
  released only by the owner through a `develop` → `main` merge request.
- `scrud_django/core/` imports no DRF and nothing from `scrud_django.ui`
  (`tests/test_core_imports.py`). After Task 5, no module outside
  `scrud_django/drf/` (and the deprecated alias shims) imports DRF at module level.
- The DRF front end keeps answering exactly as today (the parity suites are the
  oracle); the ninja front end answers as DRF does except the spec's §3.3
  tolerated differences.
- Every task leaves `uv run pytest -q` and `uv run pytest -q --reverse` green
  (one process at a time: the Whoosh index is shared), ruff clean,
  `uv run python manage.py check` no new issues, and the docs build
  `uv run --group docs sphinx-build -W -b html docs docs/_build` clean when docs change.
- Code works on SQLite, PostgreSQL, MySQL, MariaDB.
- Test conventions from CLAUDE.md; never weaken an assertion; changed behaviour
  gets explicit new assertions and a commit-message reason.
- `demo/db.sqlite3` stays byte-identical (scratch database for demo checks).
- Commit messages end with
  `Claude-Session: https://claude.ai/code/session_01SAZeWvu8MUgWeP2fTMBG43`.

## Review Focus

1. **A project with DRF not installed** runs `manage.py check`, the ninja API,
   the UI pages, embeds, prefixes and the studio without an ImportError. Pinned
   in Tasks 1 and 9.
2. **The UI on a ninja-only project** serves exactly the types the API serves,
   with the same refusals (§1.4 marks, hidden == missing), correct `in_mount`
   hrefs, branch cookie path and `Location`. Pinned in Tasks 3 and 4.
3. **A UI-type write over ninja** (component, branch with pull, push applied,
   branch-version refused) answers what DRF answers. Pinned in Task 4.
4. **An adopter upgrading** with `include("scrud_django.urls")` and DRF
   settings gets ninja and a clear path back (`scrud_django.drf.urls`), old
   import paths warn instead of breaking, and DRF Basic/Token clients can be
   kept with the hook. Pinned in Tasks 5 and 8.
5. **An app with its own `api()` plus `include("scrud_django.urls")`** is
   told about the namespace collision instead of silently reversing to the
   wrong API. Pinned in Task 8.

---

### Task 1: No stray DRF in neutral code

**Files:** `scrud_django/ui/layers.py` (~L393), `scrud_django/checks.py`
(~L633, `check_list_filters`), `scrud_django/ui/branches/middleware.py`
(`is_resource_view` ~L161), `scrud_django/ui/embed.py` (`api_url` ~L149-176),
`tests/test_core_imports.py`.

**Interfaces:** Produces `tests/no_drf.py` (a subprocess harness: settings
without `rest_framework*`, an import blocker for `rest_framework`,
`django_filters`, `scrudful.rest_framework` and scoped-rbac's DRF modules, a
URLconf of `scrud_django.ninja.api(types=[]).urls` at `api/` +
`scoped_rbac.semantic_urls` + `scrud_django.ui.urls` last) reused by Tasks 8-9.

- [ ] **Step 1: Failing test** `tests/test_core_imports.py::test_a_project_without_drf_runs`:
  in the harness subprocess, `django.setup()`, `call_command("check")` exits 0,
  and a test `Client` gets 200 from `/api/services/`, `/ui-css-files/<name>/<etag>.css`
  for a packaged stylesheet (resolve its URL with `file_urls`), and
  `/studio/` (session user). Run; expect failures at `checks.py`, `layers.py`.
- [ ] **Step 2: Fix** `layers.py` to import `core.writes.compile_json_schema`;
  `check_list_filters` returns `[]` when DRF is not importable (guarded lazy
  import); `is_resource_view`/`embed.api_url` decide by `core.types.route_authorization`
  marks (a scrud-built view) instead of `ResourceViewMixin`.
- [ ] **Step 3:** test passes; full suite and `--reverse`.
- [ ] **Step 4: Commit** `fix: neutral code needs no DRF at runtime`.

### Task 2: One copy of the write orchestration and of DRF-identical JSON

Follow-ups `core-write-orchestration` and `core-json-response` (read both in
`docs/follow-ups.md`). Do this before Task 4 adds UI types to ninja.

**Files:** `scrud_django/core/writes.py`, new `scrud_django/core/json.py`,
`scrud_django/ui/pages.py` (`_create`, `_update`, `_destroy`),
`scrud_django/ui/studio/answers.py` (`_write`, `json_response`),
`scrud_django/ninja/routers.py` (`create`, item writes),
`scrud_django/ninja/negotiation.py` (~L258), `scrud_django/ui/branches/file_views.py`.

**Interfaces (produces):**
- `core.writes.create_answer(request, resource_type, body, *, requirement, using) -> WriteAnswer`
- `core.writes.item_write_answer(request, resource_type, pk, method, body, *, requirement, using) -> WriteAnswer`
- `WriteAnswer(status: int, content: Mapping | None, etag: str | None, last_modified, location: str | None, problem: tuple[int, body, headers] | None)` — each front end only renders it.
- `core.json.dumps_like_drf(data) -> bytes` (compact separators, `ensure_ascii=False`, U+2028/2029 escaped, `allow_nan=False`) used by the studio, the file views and ninja negotiation.

- [ ] **Step 1: Tests** `tests/test_core_writes.py::test_create_answer_*` /
  `test_item_write_answer_*`: a create, a conditional create (stale `If-Match`
  412, `If-None-Match: *` on an existing list item 412), a PUT, a stale PUT, a
  DELETE in use (409), a refusal (403/404), each equal (status, validators,
  problem body) to the DRF route's answer for the same request; and
  `tests/test_core_json.py` byte equality with DRF's `JSONRenderer` for content
  with U+2028 and NaN refused.
- [ ] **Step 2: Implement** by moving the sequence out of `pages.py` (the most
  complete copy) into the core; pages, studio `answers` and ninja call it
  (move-then-delegate; delete the copies). `tests/test_front_end_parity.py`,
  `tests/test_ui_app.py`, `tests/test_studio_writes.py` stay green unchanged.
- [ ] **Step 3:** close both follow-ups (move to Done with the commit).
- [ ] **Step 4: Commit** `refactor(core): one write orchestration and one DRF-identical JSON writer`.

### Task 3: The UI finds a type's API routes in any namespace

Follow-up `ui-ninja-routes`; the facts report §3 "The UI serving ninja-routed
types" lists every call site (~25).

**Files:** `scrud_django/core/urls.py` (registry and resolver),
`scrud_django/registration.py` (`resource_type_urls`, `computed_urls` record),
`scrud_django/ninja/api.py` (records its namespace and routes),
`scrud_django/ui/pages.py` (`route_views`, `Location`),
`scrud_django/ui/embed.py` (`api_url`, `_resolve`, `_api_path`),
`scrud_django/ui/branches/selection.py` (`api_base`, `in_mount`, `cookie_path`),
`scrud_django/ui/branches/middleware.py`, `scrud_django/ui/studio/*` (the
reverses the report lists), `scrud_django/ui/checks.py` (W003/W004),
`demo/.../seed_todo_demo.py` (~L451).

**Interfaces (produces):**
- `core.urls.note_api_routes(slugs: Iterable[str], *, namespace: str | None) -> None` — called by DRF registration (namespace `None`) and `ninja.api()` (its `urls_namespace`).
- `core.urls.api_route_name(slug: str, kind: Literal["list", "detail"]) -> str | None` — the first of the bare name, then each noted namespace's `"<ns>:<slug>-<kind>"`, that reverses in the current URLconf; `None` if none.
- `core.urls.api_reverse(slug, kind, *, args=(), kwargs=None) -> str` (raises `NoReverseMatch` like `reverse`).
- `core.urls.api_namespaces() -> tuple[str | None, ...]`.
This is also the spec §3.1 slug registry; `ninja/api.py`'s hand list
`RESERVED_SLUGS` stays (it lists API routes, not types).

- [ ] **Step 1: Tests** on `tests/ninja_ui_urls.py` (ninja API + UI, no DRF
  routes): flip `tests/test_ui_routes.py::test_a_type_routed_only_in_a_namespace_is_named`
  into "served" (explicit new assertions; commit message says why); add
  `tests/test_ui_on_ninja.py`: a page GET, a page PUT (201/200 + `Location`
  under the UI), an embed of an API path, `in_mount` of an API href, the branch
  cookie's path, a published prefix page, a studio preview, W003/W004 silent —
  each on the ninja-only URLconf, with the same status the DRF URLconf gives the
  same user (parametrize over both URLconfs where the assertion is shared).
- [ ] **Step 2: Implement** the registry and port every call site to it;
  `route_views` resolves the reversed path as today (marks, §1.4, shadowing
  unchanged).
- [ ] **Step 3:** close `ui-ninja-routes`; full suite and `--reverse`.
- [ ] **Step 4: Commit** `feat(ui): the UI serves types routed by either front end`.

### Task 4: The UI and studio types on ninja

**Files:** `scrud_django/registration.py` (a public way for a spec to carry
`TypeOptions`), `scrud_django/ui/types.py` (`ui_type_specs()`; DRF
`ui_resource_type_urls()` builds from the same specs), 
`scrud_django/ui/studio/computed.py` (`studio_computed_type_specs()`),
`scrud_django/ninja/api.py` (include them when `scrud_django.ui` /
`scrud_django.ui.studio` are installed), `tests/conftest.py` (session setup via
specs), `tests/test_front_end_parity.py` (+ cases), `tests/front_ends.py`.

**Interfaces (produces):**
- `registration.json_resource_type_spec(..., options: TypeOptions | None = None)` — merged with the hook kwargs (a conflict between `options` and `checks=`/`object_checks=`/`content_filter=` is a `TypeError`).
- `ui.types.ui_type_specs() -> list[TypeSpec]`, `ui.studio.computed.studio_computed_type_specs() -> list[TypeSpec]`.
- `ninja.api()` serves them by default when the apps are installed (lifting 3A's R3); a `ui_types=False` keyword opts out.

- [ ] **Step 1: Tests** parity cases in `tests/test_front_end_parity.py` (both
  mounted, fresh data, exact compare): ui-components CRUD incl. a taken name
  (409) and a template that fails to compile (400); ui-stylesheets with a bad
  selector (400 naming `rules[i].select`); ui-branches create, pull (`pull: true`
  records a version), delete of `main` (400) and of a branch with children (409);
  ui-pushes create applied (answer replaced by the result) and delete of an
  applied push (409); ui-branch-versions GET narrowed per reader and POST/PUT/
  DELETE (405 — if DRF's 405 precedes authorization and the core's follows it,
  record the difference and rule: align ninja to DRF's observable answer for
  the same user, or add it to §3.3 with the owner-visible reason); each studio
  computed type GET; OPTIONS `Allow` for each UI type.
- [ ] **Step 2: Implement.** The module-level caches in `ui/types.py`
  (`UI_*_RESOURCE_TYPE_`/`_REGISTRATION_`) are filled from the specs path too, or
  retired (update `tests/conftest.py`'s reset).
- [ ] **Step 3:** full suite and `--reverse`; update README "django-ninja" and
  CLAUDE.md's "It serves no UI or studio types yet".
- [ ] **Step 4: Commit** `feat(ninja): the UI and studio types on ninja`.

### Task 5: The `scrud_django.drf` package, deprecated aliases, the auth hook

**Files:** create `scrud_django/drf/` (`__init__.py` raising "needs
scrud-django[drf]" when DRF is absent, like `ninja/__init__.py`; `views.py`,
`computed.py`, `filters.py`, `paginations.py`, `permissions.py`, `renderers.py`,
`serializers.py`, `requirements.py` (from `drf_requirements.py`), `validators.py`
(the DRF parts), `mixins.py` (view + serializer mixins), `ui.py` (from
`ui/views.py` + `ui/branches/views.py`), `exceptions.py` (handler, `as_drf`,
`drf_errors`), `auth.py`, `urls.py` (today's `scrud_django/urls.py`)); the old
modules become alias shims; `scrud_django/registration.py` lazy imports point
at `scrud_django.drf`.

**Interfaces (produces):**
- Each old path in the owner's list re-exports its public names and emits one
  `DeprecationWarning` at import (`stacklevel` pointing at the importer), naming
  the new path. `scrud_django.urls` still re-exports `scrud_django.drf.urls`'s
  `urlpatterns` in this task (no switch yet).
- `scrud_django.drf.auth.drf_user(request) -> User | None` — a `NINJA_USER`
  hook (`SCOPED_RBAC["NINJA_USER"] = "scrud_django.drf.auth.drf_user"`) that runs
  `api_settings.DEFAULT_AUTHENTICATION_CLASSES` (or `SCRUD_DRF_AUTHENTICATION`
  if set) on the request and returns the first authenticated user, enforcing
  CSRF for session authentication as DRF does; read scoped-rbac's hook contract
  (`resolve_user`, follow-up `scoped-rbac-ninja-user-hook-csrf-doc`) first.

- [ ] **Step 1: Tests:** `tests/test_drf_package.py` — every new path imports;
  every alias imports with exactly one `DeprecationWarning` naming the new path
  and exposes the same objects (`is`); `tests/test_ninja_auth.py` additions —
  with the hook, a Basic-auth and a Token-auth client reach the ninja API as
  their user (same answer as the DRF route), a session POST without CSRF is
  refused 403, an invalid Basic credential answers what DRF answers.
- [ ] **Step 2: Implement** (move-then-delegate; update `tests/test_drf_parity.py`
  ~L241-263 symbol homes and `tests/test_type_specs.py` ~L107 with explicit
  new assertions; the CI `package` job's wheel check imports
  `scrud_django.drf.views`). Internal code and tests import the new paths (no
  warnings from scrud's own imports: run the suite with
  `-W error::DeprecationWarning:scrud_django` scoped to scrud's modules and fix).
- [ ] **Step 3:** full suite and `--reverse`.
- [ ] **Step 4: Commit** `refactor!: the DRF adapter moves to scrud_django.drf`.

### Task 6: Tests run on either front end

**Files:** `tests/steps.py` (a client variant), `tests/conftest.py`, the
generic suites using `rest_framework.status`/`force_authenticate` where the test
is front-end-neutral, `pyproject.toml` (`[tool.pytest.ini_options]` marker `drf`).

**Interfaces (produces):** `Given.http` steps backed by `django.test.Client`
(CSRF off, `force_login`, JSON bodies by `content_type="application/json"`) when
DRF is absent or a module opts in; `pytest.mark.drf` on modules that test the
DRF adapter itself (skipped when DRF is not importable), applied in
`conftest.py` by module (a list, not 118 edits) where possible.

- [ ] **Step 1:** add the client variant and the marker with a test of the
  variant (`tests/test_steps_client.py`).
- [ ] **Step 2:** mark the DRF-specific modules; convert neutral modules'
  `rest_framework.status` to `http.HTTPStatus` and `force_authenticate` to
  `force_login` only where the module is otherwise neutral (no assertion
  changes).
- [ ] **Step 3:** full suite and `--reverse` (with DRF installed nothing is
  skipped: assert the skip count is 61 as before).
- [ ] **Step 4: Commit** `test: steps and markers for either front end`.

### Task 7: Packaging

**Files:** `pyproject.toml`, `uv.lock`, `.gitlab-ci.yml` (`package` job's wheel
check), `scrud_django/ninja/__init__.py` (the "needs [ninja]" guard becomes
unnecessary: keep an import-time check only if ninja can still be absent).

- [ ] **Step 1:** base deps `django-scoped-rbac[ninja]>=0.12.1,<0.13`,
  `scrudful[ninja]>=0.10.1,<0.11` (they bring `django-ninja>=1.7.0,<1.7.4`); a
  `drf` extra `django-scoped-rbac[drf]`, `scrudful[drf]`,
  `djangorestframework>=3.15`, `django-filter>=25.1`; keep `ninja` as an empty
  extra for 0.16 users' requirement files; the dev group installs
  `scrud-django[search,ui,drf]`. `uv lock`.
- [ ] **Step 2:** `tests/test_packaging.py` reads `pyproject.toml` and asserts
  the base and `drf` sets; the `package` CI job installs the wheel without
  extras and imports `scrud_django.ninja` and `scrud_django.ui.urls` (not yet
  `scrud_django.urls`, which still re-exports DRF until Task 8; Task 9 extends
  the check).
- [ ] **Step 3:** full suite and `--reverse`; `uv run python manage.py check`.
- [ ] **Step 4: Commit** `build!: ninja in the base install; DRF as the drf extra`.

### Task 8: The switch

**Files:** `scrud_django/urls.py`, `tests/urls.py` (~L37) and every test
URLconf that includes `scrud_django.urls` meaning DRF (the report lists them),
`scrud_django/checks.py` (namespace collision check), README (Configuration,
"django-ninja", "Migrating to 0.17.0"), `docs/ui.md` (~L65),
`docs/release-notes.md`, CLAUDE.md.

**Interfaces (produces):** `scrud_django.urls.urlpatterns` =
`[path("", scrud_django.ninja.api(types=[]).urls), path("semantic/", include("scoped_rbac.semantic_urls"))]`
(closes follow-up `ninja-semantic-urls-include`); a system check Warning when
two included URLconfs register scrud APIs under the same namespace (the
collision of Review Focus 5).

- [ ] **Step 1: Tests:** `tests/test_default_urls.py` — with
  `ROOT_URLCONF` including `scrud_django.urls` under `api/`: services,
  built-ins, UI types (Task 4), roles and semantic URLs answer as ninja (a
  ninja-only header or route marker), `scrud_django.drf.urls` answers as DRF;
  the collision check fires for `api()` + `include("scrud_django.urls")` with
  the default namespace and not with distinct ones.
- [ ] **Step 2: Implement;** switch the DRF test URLconfs to
  `include("scrud_django.drf.urls")` so every DRF test still tests DRF (grep
  every `include("scrud_django.urls")` in `tests/`; list each in the commit).
- [ ] **Step 3:** docs: migration section (the switch, `scrud_django.drf.urls`
  + `[drf]` to stay on DRF, the deprecated aliases, the auth hook, the namespace
  note, `/api/ui-branch-items/<branch>/` for branch JSON), release notes
  Breaking, CLAUDE.md (Architecture: ninja default, DRF adapter in
  `scrud_django.drf`; Commands unchanged), spec §3.2 strike of R1's
  `first`/`last` and the Breaking-list entry.
- [ ] **Step 4:** full suite and `--reverse`; docs build.
- [ ] **Step 5: Commit** `feat!: scrud_django.urls serves ninja`.

### Task 9: The suite without DRF, and its CI job

**Files:** `tests/settings.py` (an env flag `SCRUD_TEST_NO_DRF=1`: no
`rest_framework*` apps, no `REST_FRAMEWORK`, `ROOT_URLCONF` a ninja test
URLconf), `tests/conftest.py` (session setup via specs when DRF is absent; skip
`drf`-marked modules), `.gitlab-ci.yml` (job `test-no-drf`), CLAUDE.md Commands.

- [ ] **Step 1:** the job: `uv sync --locked`, then remove DRF and
  django-filter from the environment (safetydance-django requires DRF, so use
  an import blocker via `tests/no_drf.py`'s mechanism in `conftest.py` when the
  flag is set, rather than uninstalling), then
  `SCRUD_TEST_NO_DRF=1 uv run --no-sync pytest -q`.
- [ ] **Step 2:** run it locally until green; every skip is a `drf`-marked
  module (assert in `conftest.py` that nothing else is skipped by the flag).
- [ ] **Step 3:** the `package` job installs the base wheel (no extras) and
  imports `scrud_django.urls` and serves `/api/services/` with DRF absent.
- [ ] **Step 4: Commit** `ci: the suite without DRF`.

### Task 10: The demo on ninja

**Files:** `demo/demo/urls.py`, `demo/demo/settings.py`, `demo/demo/views.py`
(delete: unused), `demo/demo/templates/registration/login.html` (new),
`demo/.../seed_todo_demo.py`, CLAUDE.md demo paragraph, README demo section.

- [ ] **Step 1:** one `api(types=[json_resource_type_spec(partner-profiles…),
  json_resource_type_spec(todo-lists…)])` at `api/` with the `articles` router
  added (`/ninja/` goes); `semantic/`; `accounts/` = `django.contrib.auth.urls`
  with a minimal login template; UI last; no DRF apps or settings.
- [ ] **Step 2:** seed prints the new URLs (`/accounts/login/`).
- [ ] **Step 3: Verify on a scratch database** (memory recipe; `cp` backup and
  `cmp` of `demo/db.sqlite3`): migrate, seed, log in through `/accounts/login/`
  with the test `Client`, GET a UI page, `/api/todo-lists/<pk>/` JSON, the
  articles API, the workbench URL; all 200.
- [ ] **Step 4: Commit** `docs(demo): the demo on ninja with Django's login`.
