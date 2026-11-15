# ninja phase 3A: an additive ninja front end — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve scrud-django's resource types through django-ninja, giving the
same answers as the DRF routes, as an addition that leaves `scrud_django.urls`
and every DRF answer unchanged.

**Architecture:** A package `scrud_django.ninja` builds one `NinjaAPI`
(`scrud_django.ninja.api(types=[...])`). It has one `ScrudfulRouter` per type
slug, whose handlers call the core facade (`scrud_django.core`) and nothing
else for domain logic. Types are described by a neutral `TypeSpec` that both
front ends register from. A parity harness mounts both front ends in one
URLconf (`/drf/`, `/ninja/`), replays the same exchanges on fresh data and
compares the responses byte for byte, apart from the spec's §3.3 list.

**Tech Stack:** Django 5.2, django-ninja 1.7.0–1.7.3 (through
`django-scoped-rbac[ninja]` and `scrudful[ninja]`), scrudful 0.10.1
(`scrudful.ninja`), django-scoped-rbac 0.12.1 (`scoped_rbac.ninja`), pytest
with safetydance.

**Spec:** `docs/superpowers/specs/2026-11-11-ninja-front-end-design.md`, §3.1–3.3
and "Testing" (phase 3). The follow-ups this plan closes are in
`docs/follow-ups.md`: `core-options-copy-entry`,
`core-allowed-methods-existence-leak`, and (as a constraint)
`core-save-checked-mark-body` / `core-write-no-recheck`.

**Branch:** `feature/ninja-front-end`, from `feature/core-types` (MR !18). Its
MR targets `feature/core-types`.

## What this plan leaves out (parts B and C, and phase 2)

- **Part B (own models):** `scrud_django.ninja.workflow_check`, signals and
  workflow actions moved to the core, and the `demo/` example (§3.4).
- **Part C (the switch), after phase 2:**
  - `scrud_django.urls` becomes ninja;
  - the `drf` extra and the `scrud_django.drf` module move;
  - the deprecated import aliases;
  - the CI job with base dependencies only.

  Phase 2 must come first, because until then the UI renders HTML on the DRF
  API's own routes.
- **Phase 2's types:** the UI's resource types and the studio's computed types.
  Their behaviour is DRF mixins until phase 2 moves it into core hooks, so the
  ninja API does not serve them yet (ruling R3).

## Rulings against the spec (owner may overrule)

- **R1. The list route is not `scrudful_list`.**
  - `scrudful_list` validates `page`/`page_size` with pydantic (400s). scrud's
    DRF paginator instead clamps `page_size` silently (`<1` or not an integer
    gives 100, `>1000` gives 1000) and answers 404 "Invalid page." for a bad
    `page`. It also has no `first`/`last`, and its list ETag hashes the page.
  - So the ninja list is a described plain GET operation that pages with
    `core.page_of` and validates with `core.list_etag` /
    `core.list_last_modified` / `core.evaluate_list_preconditions`, giving
    DRF's answers exactly.
  - As a result, two things in the spec are dropped:
    - the spec's list-envelope change (`first`/`last`, page 1's `previous`
      as `?page=1`), with its **Breaking** entry;
    - the scrudful `list_etag_func(request, queryset, page)` proposal.
  - Cost if wrong: if the owner wants `first`/`last`, it is one change to
    `paginations.py` and to the ninja list together, plus the corpus.
- **R2. Conditional create is evaluated by the core, not by
  `scrudful_create(list_load=...)`.** The ninja create holds
  `core.writes.conditional_create_lock` and evaluates
  `core.evaluate_list_preconditions` with the list validators of a GET copy,
  as the DRF `create` does.
- **R3. Not served on ninja in 3A:**
  - `ui_resource_type_urls()` types;
  - `studio_computed_type_urls()` types;
  - the `ui-css-files` / `ui-js-files` views.

  `scrud_django.ninja.api()` does not mount them. Phase 2 moves them onto core
  hooks (and closes `studio-computed-to-compute`).
- **R4. OPTIONS is scrud's own OPTIONS operation**, not scrudful's automatic
  one with an `allowed=` hook.
  - It runs the request-level check of each method before any lookup. This
    closes `core-allowed-methods-existence-leak`.
  - It answers exactly what the DRF route answers for missing, hidden and
    readable items.
- **R5. Links follow the serving API's namespace.** The ninja routes keep
  DRF's route names (`<slug>-list`, `<slug>-detail`, `services`,
  `collections-json-schema`, `collections-json-ld`), under the `NinjaAPI`'s
  `urls_namespace`. `core.urls.absolute_reverse` reverses
  `<request namespace>:<name>` first when the request was resolved under a
  namespace, and the plain name otherwise. This is how `href`, `Location`,
  schema/context `Link`s and `services/` point at the ninja routes.
- **R6. No new system check for `NinjaAPI(auth=...)` in 3A.** The factory
  owns the `NinjaAPI` and refuses `auth=`. The check matters once apps build
  their own APIs around scrud routers, which is part C.
- **R7. The slug registry (§3.1) waits for phase 2.**
  `core.types._SLUG_IRIS` already records every registered slug on either
  front end. The UI at `/` is its only consumer.

## Global Constraints

- **Dependencies.**
  - `django-ninja>=1.7.0,<1.7.4`, brought by `django-scoped-rbac[ninja]>=0.12.1,<0.13`
    and `scrudful[ninja]>=0.10.1,<0.11` in a new optional extra `ninja`.
  - The `dev` dependency group installs it.
  - The base dependencies do not change in 3A.
- **Every existing test passes unchanged.**
  - The DRF answers do not change (`uv run pytest -q`, also with `--reverse`).
  - A test changes only where this plan says so (the FRONT_ENDS
    parametrization), never by weakening an assertion.
- **The core stays neutral.** `scrud_django/core/` imports nothing from
  `ninja`, `pydantic`, `scoped_rbac.ninja` or `scrudful.ninja`. Add them to
  the deny list in `tests/test_core_imports.py` (Task 2).
- **No domain logic in `scrud_django/ninja/`.**
  - Reads, lists, writes, preconditions on lists, workflow, body checks, list
    ETags and URLs come from the core facade.
  - A handler that needs something the core lacks adds it to the core, with a
    core test.
- **Writes go through `core.writes.create` / `update` / `destroy` only.**
  Never call `save_checked` directly (`core-save-checked-mark-body`,
  `core-write-no-recheck`).
- **Handlers and hooks are sync `def`.**
- **No `NinjaAPI(auth=...)`.** The factory refuses an `auth` argument
  (`TypeError`).
- **Never edit upstream packages.** Do not touch `.venv` or the scoped-rbac,
  scrudful or safetydance repos. Gaps are worked around here and recorded in
  `docs/follow-ups.md`.
- **No version bump** (0.16.0 stays).
- **Commits:**
  - Every commit message ends with
    `Claude-Session: https://claude.ai/code/session_01SAZeWvu8MUgWeP2fTMBG43`.
  - Commit only the files the task touched.
- **Four databases.** Code must work on SQLite, PostgreSQL, MySQL and MariaDB
  (CLAUDE.md "Gotchas"). The parity harness's key handling must not assume
  sequences roll back (PostgreSQL's do not).
- **Run before every commit:**
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `uv run pytest -q`

## Review Focus

These are the failure modes most likely to bite a user, each pinned by a test
in the task named:

1. **Hidden == missing, before 412.**
   - Case: a PUT/DELETE with a stale `If-Match` on an item the requester
     cannot read.
   - Expected: 404, not 412, on both front ends.
   - Test: Task 5, corpus `put-hidden-stale-if-match`.
2. **OPTIONS must not reveal existence.**
   - Case: OPTIONS on a missing item and on an existing one, as a requester
     granted nothing.
   - Expected: the same answer as DRF for both (R4).
   - Test: Task 7, `test_options_missing_and_existing_answer_alike`.
3. **A rewritten OPTIONS is refused at entry.**
   - Case: an OPTIONS rewritten to PUT by middleware.
   - Expected: 403 before any core call.
   - Test: Task 4, `test_rewritten_options_is_refused_before_the_core`.
4. **The conditional create is serialized.**
   - Case: a POST with `If-None-Match: *` / `If-Match`.
   - Expected: it holds the type's conditional-create lock across check and
     save, on ninja as on DRF.
   - Test: Task 6, `test_conditional_create_holds_the_type_lock`.
5. **Anonymous and unauthenticated parity.**
   - Case: an anonymous request to a type whose `POLICY_FOR_UNAUTHENTICATED`
     grants GET, and one whose policy grants nothing.
   - Expected: the same 200/401/404 as DRF. The factory refuses `auth=`.
   - Test: Task 4 (`auth=`) and Task 5, corpus `anonymous-*`.

---

### Task 1: The `ninja` extra and the package skeleton

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- Create: `scrud_django/ninja/__init__.py`
- Test: `tests/test_ninja_imports.py`

**Interfaces:**
- Produces:
  - the `ninja` extra;
  - `scrud_django.ninja`, importable when django-ninja is installed;
  - without django-ninja, `import scrud_django.ninja` raises `ImportError`
    whose message names `pip install 'scrud-django[ninja]'`.

- [ ] **Step 1: Write the failing tests.**

  `tests/test_ninja_imports.py`:
  - `test_ninja_package_imports`: `importlib.import_module("scrud_django.ninja")`
    succeeds, and `ninja.__version__` satisfies `1.7.0 <= v < 1.7.4`.
  - `test_ninja_package_names_the_extra_without_ninja`: in a subprocess (as
    `tests/test_core_imports.py` blocks `rest_framework`), block `ninja` and
    import `scrud_django.ninja`. Assert `ImportError` with
    `"scrud-django[ninja]"` in the message.
  - `test_models_import_without_ninja`: in a subprocess with `ninja` blocked,
    `import scrud_django.models` succeeds.

- [ ] **Step 2: Run them.** Run `uv run pytest tests/test_ninja_imports.py`.
  Expected: FAIL (`ModuleNotFoundError: scrud_django.ninja`, and `ninja` not
  installed).

- [ ] **Step 3: Add the extra, then lock and sync.**
  - In `pyproject.toml` add
    `ninja = ["django-scoped-rbac[ninja]>=0.12.1,<0.13", "scrudful[ninja]>=0.10.1,<0.11"]`
    to `[project.optional-dependencies]`.
  - Change the `dev` group's `scrud-django[search,ui]` to
    `scrud-django[search,ui,ninja]`.
  - Run `uv lock`, then `uv sync`.
  - Create `scrud_django/ninja/__init__.py`. It imports `ninja` inside a
    `try`, re-raising `ImportError("scrud_django.ninja needs django-ninja: pip install 'scrud-django[ninja]'")`
    from the original.

- [ ] **Step 4: Run the tests.** Run `uv run pytest tests/test_ninja_imports.py`
  (PASS), then `uv run pytest -q` (all pass).

- [ ] **Step 5: Commit.**
  `git add pyproject.toml uv.lock scrud_django/ninja/__init__.py tests/test_ninja_imports.py`,
  with message `build(ninja): the ninja extra and the scrud_django.ninja package`.

### Task 2: `TypeSpec`, the neutral registration

**Files:**
- Modify: `scrud_django/registration.py`, `tests/resource_types.py`,
  `tests/test_core_imports.py`
- Test: `tests/test_type_specs.py`

**Interfaces:**
- Produces, in `scrud_django/registration.py`:

  ```python
  @dataclass(frozen=True)
  class TypeSpec:
      kind: Literal["json", "computed"]
      resource_type_uri: str
      slug: str
      options: core.types.TypeOptions
      document_media_type: str | None      # "application/schema+json" for json-schema,
                                           # "application/ld+json" for json-ld, else None
      registration_kwargs: Mapping[str, Any]   # what ResourceTypeRegistration takes (frozen copy)
      def register(self, *, urls_func: Callable[[str], list] | None = None) -> list: ...

  def json_resource_type_spec(resource_type_uri, slug, revision="1", schema_resource=None,
      schema_func=None, schema_uri=None, context_resource=None, context_func=None,
      context_uri=None, indexing_policy_resource=None, indexing_policy_func=None,
      uses_reversion=True, register_service=True, checks=(), object_checks=(),
      content_filter=False, document_media_type=None) -> TypeSpec
  def computed_resource_type_spec(resource_type_uri, slug, schema_func, context_func, *, compute) -> TypeSpec
  def built_in_type_specs() -> list[TypeSpec]   # json-schema, json-ld, indexing-policies, workflow-policies
  ```

  - `TypeSpec.register(urls_func=)` does what `json_resource_type` /
    `computed_resource_type` do today without building DRF views:
    `register_type_options(...)`, then `register_or_defer(...)`, returning
    `urls_func(slug)` (or `[]`).
  - `json_resource_type(...)`, `computed_resource_type(...)` and
    `built_in_resource_type_urls()` keep their signatures and return values.
    Each now builds a spec and calls `spec.register(urls_func=<today's DRF url builder>)`.
  - `tests/resource_types.py` gains `ninja_type_specs() -> list[TypeSpec]`:
    the types of `json_resource_types()` and `hooked_resource_types()` that
    register no `viewset_class`, built from the same arguments. Use one
    module-level argument table per type, shared with the DRF calls, so the
    two front ends serve identical types.

- [ ] **Step 1: Write the failing tests** (`tests/test_type_specs.py`;
  `django_db`, safetydance idiom):
  - `test_json_resource_type_returns_todays_paths`:
    `json_resource_type(uri, "spec-things")` returns two paths named
    `spec-things-list` / `spec-things-detail`, and its `ResourceType` row
    equals the one a `json_resource_type_spec(uri, "spec-things").register()`
    makes.
  - `test_spec_register_twice_is_idempotent`: two `register()` calls leave
    one `ResourceType` row, and no warning is logged.
  - `test_computed_spec_requires_compute`: `computed_resource_type_spec(..., compute=None)`
    raises `ImproperlyConfigured`.
  - `test_built_in_specs_name_document_media_types`: in
    `built_in_type_specs()`, `json-schema` has `"application/schema+json"`,
    `json-ld` has `"application/ld+json"`, and the other two have `None`.
  - In `tests/test_core_imports.py`, extend the deny list with `ninja`,
    `pydantic`, `scoped_rbac.ninja` and `scrudful.ninja`.

- [ ] **Step 2: Run them.** Run `uv run pytest tests/test_type_specs.py tests/test_core_imports.py`.
  Expected: FAIL, because the names do not exist.

- [ ] **Step 3: Implement** the interfaces above. `registration_kwargs` is
  frozen with `types.MappingProxyType`.

- [ ] **Step 4: Run.** Run `uv run pytest tests/test_type_specs.py tests/test_core_imports.py`
  (PASS), then `uv run pytest -q` (all pass, unchanged).

- [ ] **Step 5: Commit**, with message
  `feat(registration): TypeSpec, the neutral type registration both front ends use`.

### Task 3: Links follow the serving API's namespace (R5)

**Files:**
- Modify: `scrud_django/core/urls.py`
- Test: `tests/test_core_urls.py`, with a URLconf module `tests/namespaced_urls.py`

**Interfaces:**
- Produces: `absolute_reverse(name, *, args=(), kwargs=None, request=None)`.
  - When `request.resolver_match.namespace` is non-empty and `name` has no
    `:`, it first tries `f"{namespace}:{name}"`.
  - On `NoReverseMatch` it falls back to today's call.
  - A request without `resolver_match` (or with an empty namespace) behaves
    exactly as today.

- [ ] **Step 1: Write the failing tests.**
  - Fixture: `tests/namespaced_urls.py` includes `tests.urls` at `""` and,
    under `path("ns/", include((patterns, "ns"), namespace="ns"))`, a route
    named `partner-profiles-detail` (any view).
  - `test_namespaced_request_reverses_in_its_namespace`: a request resolved
    under `ns` gets `http://testserver/ns/...`.
  - `test_plain_request_reverses_globally`: a request resolved at the root
    gets today's URL.
  - `test_namespace_without_the_name_falls_back`: a request under `ns`
    asking for `services` gets the global URL.

- [ ] **Step 2: Run them.** Run `uv run pytest tests/test_core_urls.py`.
  Expected: the namespaced case FAILs.

- [ ] **Step 3: Implement** it in `core/urls.py`; `scrud_django.reverse` is
  untouched.

- [ ] **Step 4: Run.** Run `uv run pytest tests/test_core_urls.py`, then
  `uv run pytest -q`.

- [ ] **Step 5: Commit**, with message
  `feat(core): links reverse within the serving API's URL namespace`.

### Task 4: The API factory, errors, entry checks and the parity harness

**Files:**
- Create:
  - `scrud_django/ninja/api.py`
  - `scrud_django/ninja/errors.py`
  - `scrud_django/ninja/entry.py`
  - `tests/ninja_api.py`
  - `tests/parity_urls.py`
  - `tests/front_ends.py`
- Modify: `scrud_django/ninja/__init__.py` (exports `api`)
- Test: `tests/test_ninja_api.py`, `tests/test_front_end_parity.py`

**Interfaces:**
- Produces:

  ```python
  # scrud_django/ninja/api.py
  def api(*, types: Sequence[TypeSpec] = (), urls_namespace: str = "scrud",
          built_ins: bool = True, **ninja_kwargs) -> ninja.NinjaAPI
  ```

  - It raises `TypeError` if `"auth"` is in `ninja_kwargs`.
  - It builds `NinjaAPI(urls_namespace=..., default_router=ScrudfulRouter(), **ninja_kwargs)`.
  - It registers each spec (`spec.register()`) and mounts one router per slug.
    The routers come from Tasks 5–8; until they land, the factory mounts
    nothing for types.
  - It calls `errors.install(api)` before returning, and the caller may still
    `add_router` before reading `api.urls`.

  ```python
  # scrud_django/ninja/errors.py
  def install(api) -> None
  ```

  - It registers a `ScrudException` handler whose response equals
    `scrud_exception_handler`'s for the same exception on DRF: status,
    `Content-Type` (`application/problem+json`), body (bytes) and `Link`.
  - It then calls `scoped_rbac.ninja.install_error_handlers(api)`.

  ```python
  # scrud_django/ninja/entry.py
  def entry(view) -> view
  ```

  This is a `router.add_decorator(entry, mode="view")` decorator, applied to
  every scrud router. For every request except a real OPTIONS (or a CORS
  preflight), it does, in DRF's `dispatch`/`initial` order:
  1. `access.refuse_rewritten_options(request)` (403), closing
     `core-options-copy-entry`;
  2. `scoped_rbac.ninja.resolve_user(request)`;
  3. for PUT/PATCH/DELETE, and for a POST carrying `If-Match`,
     `If-None-Match` or `If-Unmodified-Since`, the whole operation runs inside
     `core.writes.write_transaction(write_db())`, which rolls back on a
     status `>= 400`.
- Test harness, in `tests/front_ends.py`:
  - `FRONT_ENDS = ("drf", "ninja")`.
  - `Exchange(status, headers, body)`.
  - `exchange(client, front_end, method, path, *, body=None, headers=None) -> Exchange`.
    `path` holds `{mount}`, which becomes `/drf/` or `/ninja/`.
  - The normalizations, and nothing else:
    - the mount prefix becomes `{mount}`;
    - `Date`, `Vary`, `WWW-Authenticate` and `Set-Cookie` are dropped;
    - the target of the `rel="https://w3id.org/semantic-http/ns#http-schema-resource-document"`
      link becomes `{hsr}`;
    - `Content-Length` is recomputed after the replacement;
    - each key a case's setup created becomes `{name}` in path segments only
      (setup returns `{name: pk}`), so PostgreSQL's sequences do not matter.
  - `parity(case)` runs `case.setup` and then the exchange once per front
    end, each in `transaction.atomic()` with `set_rollback(True)` and a
    frozen clock (patch whatever `Resource.etag` / `modified_at` derive from),
    and returns both `Exchange`s.
  - `tests/parity_urls.py`:
    - `path("drf/", include("tests.urls"))`, not namespaced (so DRF's global
      names resolve under `/drf/`);
    - `path("ninja/", tests.ninja_api.API.urls)`;
    - `path("", include(hsr_urls()))`.
  - `tests/ninja_api.py`: `API = scrud_django.ninja.api(types=ninja_type_specs(), urls_namespace="scrud-ninja")`.

- [ ] **Step 1: Write the failing tests.**

  `tests/test_ninja_api.py`:
  - `test_api_refuses_auth`: `api(auth=object())` raises `TypeError`.
  - `test_scrud_errors_render_as_on_drf`: for a `QueryParameterError`, a
    `RequestBodyTooDeepError` and a `ResourceInUseError`, raised from a probe
    route on a throwaway API and from a DRF probe view, the status,
    Content-Type, `Link` and body bytes are equal.
  - `test_rewritten_options_is_refused_before_the_core`:
    - Setup: a middleware (test-only, added with `override_settings`) rewrites
      an OPTIONS with `X-HTTP-Method-Override: PUT` to PUT. Patch
      `scrud_django.core.resources.check_request` to record calls.
    - Request: as a user who may PUT in context "b", send an OPTIONS to a
      ninja detail route with a body writing context "a".
    - Assert: 403, and `check_request` was never called.
  - `test_unsafe_methods_run_in_one_transaction`: on a ninja PUT that the
    handler fails with 400 after a write in the same request, the row is
    unchanged. The probe route is in the test module.

  `tests/test_front_end_parity.py`:
  - `test_services_match`: `GET {mount}services/` is equal on both. This
    passes once Task 8 lands; until then, mark it `xfail(strict=True)` with
    reason `"Task 8"`.
  - `test_unknown_route_is_404_on_both`: `GET {mount}no-such-type/` gives an
    equal status (404) on both.

- [ ] **Step 2: Run them.** Run
  `uv run pytest tests/test_ninja_api.py tests/test_front_end_parity.py`.
  Expected: FAIL.

- [ ] **Step 3: Implement** the interfaces above.

- [ ] **Step 4: Run** the two modules, then `uv run pytest -q`.

- [ ] **Step 5: Commit**, with message
  `feat(ninja): the API factory, error rendering, entry checks and the parity harness`.

### Task 5: Item routes: GET, HEAD, PUT, DELETE

**Files:**
- Create: `scrud_django/ninja/routers.py`
- Modify: `scrud_django/ninja/api.py` (mount them)
- Test: `tests/test_front_end_parity.py`

**Interfaces:**
- Consumes: Tasks 2–4. From the core:
  - `requirement_for`
  - `get_resource_type_for` (with `using=type_db()` for unsafe methods)
  - `check_body`
  - `load_for_read(..., method=, lock_using=, body=)`
  - `item_validators`, `item_links`
  - `writes.update`, `writes.destroy`
  - `write_db`
- Produces: `routers.type_router(spec: TypeSpec) -> ScrudfulRouter`, with
  detail path `"/{slug}/"` and url name `<slug>-detail`. The type is read per
  request by slug, so deferred registration works as on DRF.
  - **`before`** reads the body with `scoped_rbac.ninja.request_json` for
    PUT, then runs `check_body`, then `check_request` for the item route.
    This gives DRF's order: 400 body checks before 403.
  - **`load`** is `load_for_read(request, rtype, pk, requirement=..., method=request.method, body=..., lock_using=<write db for PUT/DELETE, not on an OPTIONS copy>)`.
    On the post-write call scrudful makes after a 2xx PUT, it returns the
    object the handler stored on the request (`request._scrud_ninja_written`),
    without checks.
  - **`etag_func` / `last_modified_func`** come from `item_validators`, with
    the ETag quoted as DRF's.
  - **schema/context links** come from `item_links`.
  - **`error` / `error_body_func`** give scrudful's own 400/412/428 bodies
    exactly as DRF's (problem details while `UNIFORM_ERRORS` is on, native
    `{"detail"}` otherwise).
  - **GET/HEAD** return the resource's content.
  - **PUT** returns `writes.update(...)`'s content (200).
  - **DELETE** calls `writes.destroy(...)` (204).
  - **A type with `document_media_type`** answers its GET/HEAD with that
    media type unless `Accept` prefers `application/json`, as
    `DocumentMediaTypeMixin` does.
  - **PATCH** is not routed (405, as DRF).

- [ ] **Step 1: Write the failing tests.** Add corpus cases to
  `tests/test_front_end_parity.py`, each asserting `drf == ninja` on the
  `partner-profiles` type and on `json-schema` (for the document media type):
  - GET as admin, reader-in-context, no-roles and anonymous;
  - a missing key, and a non-integer key;
  - a hidden item (another context);
  - HEAD;
  - `If-None-Match` giving a 304, and `If-Modified-Since`;
  - PUT:
    - without `If-Match` (428);
    - a stale `If-Match` (412);
    - a matching one (200, new ETag);
    - a body failing the schema (400);
    - a non-object body (400);
    - a body too deep (400);
    - a body writing a context the user may not write (403);
  - `put-hidden-stale-if-match` (404);
  - PUT under a workflow policy: allowed (200) and refused (403/400 as DRF);
  - DELETE with `If-Match` (204), DELETE in use (409), DELETE hidden (404);
  - PATCH (405);
  - `anonymous-*`: anonymous GET on a type whose
    `POLICY_FOR_UNAUTHENTICATED` grants GET, and on one whose policy grants
    nothing.

- [ ] **Step 2: Run them.** Run `uv run pytest tests/test_front_end_parity.py`.
  Expected: FAIL (ninja 404s).

- [ ] **Step 3: Implement** `type_router`'s item operations. Write each
  operation as `@router.api_operation([...], "/{slug}/", url_name=...)`
  directly above `@scrudful(...)` (scrudful's decorator order).

- [ ] **Step 4: Run.** Run the module until every case is equal, then
  `uv run pytest -q`. If a case cannot be made equal without changing a DRF
  answer, stop and report it as DONE_WITH_CONCERNS with the two exchanges.
  Do not change the DRF side.

- [ ] **Step 5: Commit**, with message
  `feat(ninja): item routes over the core, equal to DRF's`.

### Task 6: List and create

**Files:**
- Modify: `scrud_django/ninja/routers.py`
- Test: `tests/test_front_end_parity.py`, `tests/test_ninja_api.py`

**Interfaces:**
- Consumes: `readable_queryset` / `list_queryset(request, rtype, request.GET, requirement=, ignored=frozenset({"page", "page_size", "format"}))`, `page_of`, `list_etag`, `list_last_modified`, `evaluate_list_preconditions`, `list_validators_needed`, `writes.create`, `writes.conditional_create_lock`, `type_db`.
- Produces, on `type_router`:
  - **The list path `"/"`** (url name `<slug>-list`), GET/HEAD and POST, as
    `@described(Entity(...))` plain operations (R1), giving DRF's answers.
  - **`page` and `page_size`** are read from `request.GET` with DRF's
    `StandardResultsSetPagination` rules:
    - a `page_size` that is not a positive integer gives 100;
    - a `page_size` over 1000 gives 1000;
    - a `page` that is not a positive integer, or is past the last page,
      gives 404 "Invalid page." (`EmptyPage` from `page_of`);
    - page 1 of an empty list is 200.
  - **The envelope** is `{count, page_count, next, previous, content}`, with
    each entry `{href, last_modified, etag, link, content}`. `href`, `next`
    and `previous` are built through `absolute_reverse` /
    `request.build_absolute_uri`.
  - **The list ETag** is `list_etag(rtype, items=page, count=count, query=sorted(request.GET.lists()))`.
  - **Last-Modified** is DRF's: the latest `modified_at` of the unpaged list,
    and `readable_removal`.
  - **Preconditions** on GET are `evaluate_list_preconditions`, giving 304/412
    as DRF.
  - **The schema/context links** are `collections-json-schema` /
    `collections-json-ld` of the slug.
  - **POST**, in order:
    1. `check_body`;
    2. the request check with the body;
    3. for a conditional POST (`list_validators_needed`), inside the entry
       transaction: `conditional_create_lock(rtype, type_db())`, then
       `evaluate_list_preconditions` with the list validators of a GET copy;
    4. `writes.create(...)`, which answers 201 with `Location` (the new item's
       `<slug>-detail`), the ETag, Last-Modified, Link and the content.
  - **A computed type** has no list path.

- [ ] **Step 1: Write the failing tests.**
  - Corpus cases (`drf == ninja`):
    - list as admin, reader and anonymous;
    - a content filter (`filtered/partner-profiles`-style type with
      `content_filter=True`), plus integer keys and NUL in a filter
      parameter (400);
    - `page=2&page_size=1`, `page=0`, `page=x`, `page=99`;
    - `page_size=0`, `page_size=x`, `page_size=5000`;
    - an empty list;
    - `If-None-Match` with the list ETag (304);
    - HEAD;
    - POST: valid (201), a body failing the schema (400), a context it may
      not write (403);
    - `If-None-Match: *` against an existing collection (412);
    - `If-Match` matching (201) and stale (412);
    - a create under a workflow policy: allowed and refused.
  - In `tests/test_ninja_api.py`, `test_conditional_create_holds_the_type_lock`:
    patch `core.writes.conditional_create_lock` with a recording context
    manager. A ninja POST with `If-None-Match: *` enters it once, and the save
    happens while it is held. A plain POST does not enter it.

- [ ] **Step 2: Run them.** Expected: FAIL.

- [ ] **Step 3: Implement.** Any piece of list logic missing from the core,
  for example the list's `Last-Modified` as DRF computes it, moves into
  `core/lists.py` with a core test, and the DRF mixin calls it.

- [ ] **Step 4: Run.** Run the modules, then `uv run pytest -q`.

- [ ] **Step 5: Commit**, with message
  `feat(ninja): list and create routes equal to DRF's paging, ETags and conditional create`.

### Task 7: OPTIONS (R4)

**Files:**
- Modify: `scrud_django/core/resources.py` (a key-level `allowed_methods_for`),
  `scrud_django/ninja/routers.py`
- Test: `tests/test_core_resources.py`, `tests/test_front_end_parity.py`

**Interfaces:**
- Produces:

  ```python
  # scrud_django/core/resources.py
  def allowed_methods_for(request, resource_type, pk: str | None, requirement, *,
                          list_requirement=None, methods: Sequence[str] | None = None) -> list[str]
  ```

  - It runs each method's request-level check first, then looks the item up
    only if any method passed, then defers to `allowed_methods(...)` with the
    loaded object.
  - It answers exactly what the DRF route answers:
    - for `pk=None` (the list route);
    - for a missing key;
    - for a hidden item;
    - for a readable item.

  The test pins DRF's answers first.
- On the ninja routers, an explicit OPTIONS operation on `"/{slug}/"` and on
  `"/"` replaces scrudful's automatic one and answers 204 with the `Allow` of
  `allowed_methods_for`. A CORS preflight still gets scrudful's bare 204.

- [ ] **Step 1: Write the failing tests.**
  - `tests/test_core_resources.py::test_allowed_methods_for_matches_the_drf_route`:
    for each of {admin, reader in "a", granted nothing, anonymous} × {list,
    readable item, hidden item, missing key}, the `Allow` the DRF OPTIONS
    answers equals `allowed_methods_for(...)`.
  - `tests/test_front_end_parity.py::test_options_missing_and_existing_answer_alike`:
    as a user granted nothing, OPTIONS on a missing key and on an existing
    key is equal across the front ends, and equal between the two keys.
  - Corpus cases: OPTIONS of the list and of an item, for each user above
    (`drf == ninja`).

- [ ] **Step 2: Run them.** Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run** the modules, then `uv run pytest -q`. Then mark
  `core-allowed-methods-existence-leak` and `core-options-copy-entry` decided
  in `docs/follow-ups.md`: move each under `# Decided`, saying how they are
  met.

- [ ] **Step 5: Commit**, with message
  `feat(ninja): OPTIONS from the core, with no existence leak`.

### Task 8: Computed types, `services/`, collection documents, role routes

**Files:**
- Modify: `scrud_django/ninja/routers.py`, `scrud_django/ninja/api.py`; the
  core where the computed answer's logic lives today (move it, as below)
- Test: `tests/test_front_end_parity.py`

**Interfaces:**
- Produces:
  - **Computed types.** `routers.computed_router(spec)` serves
    `"/{slug}/"` GET/HEAD. Its answer equals `ComputedResourceViewSet.retrieve`'s:
    - the content from `type_options(...).compute(request, key)`;
    - a strong `computed_etag`;
    - `If-None-Match` giving 304;
    - the schema/context `Link`.

    The pieces of that answer move into `scrud_django/core/computed.py`
    (`computed_etag`, `computed_answer(request, rtype, key) -> (content, etag)`),
    and `ComputedResourceViewSet` calls them. Authentication matches DRF's
    `IsAuthenticated`: an anonymous request gets DRF's answer.
  - **`services/`**: GET/HEAD returning `services.catalog` with its
    `services.etag_for` ETag, through `absolute_reverse` (R5).
  - **`collections-json-schema/{slug}/` and `collections-json-ld/{slug}/`**
    give the same documents and media types as the DRF views. Move the
    document builders out of `views.py` into `scrud_django/core/collections.py`,
    and the DRF views call them.
  - **Role routes.** `api()` mounts `scoped_rbac.ninja.api.router()` at `""`
    (`role-assignments/`, `roles/`, `user-rbac-policy/`).
  - **Mount order.** The `ScrudfulRouter`s mount before any path that could
    shadow `/_hsr/` (scrudful gotcha 13).

- [ ] **Step 1: Write the failing tests.** Corpus cases (`drf == ninja`):
  - a computed type (`tests/resource_types.py`'s computed `compute` type)
    GET as admin and anonymous, with `If-None-Match` matching (304);
  - `services/` GET, and GET with `If-None-Match`;
  - both collection documents for `partner-profiles`, with `Accept: */*` and
    with `Accept: application/json`;
  - `roles/` GET as admin.

  Remove the `xfail` from `test_services_match`.

- [ ] **Step 2: Run them.** Expected: FAIL.

- [ ] **Step 3: Implement.**

- [ ] **Step 4: Run** the module, then `uv run pytest -q`.

- [ ] **Step 5: Commit**, with message
  `feat(ninja): computed types, services, collection documents and role routes`.

### Task 9: The FRONT_ENDS parametrization of the generic suites

**Files:**
- Create: `tests/ninja_urls.py`, which mounts `tests.ninja_api.API.urls` at
  `""` plus `include(hsr_urls())`
- Modify:
  - `tests/conftest.py` (a `front_end` fixture);
  - `tests/front_ends.py` (an `api_url(front_end, name, *args, **kwargs)`);
  - the suites `tests/test_rest.py`, `tests/test_conditional.py`,
    `tests/test_list_rbac.py`, `tests/test_rest_workflow.py`,
    `tests/test_error_media_types.py`, `tests/test_options.py`, and the
    HTTP-making tests of `tests/test_workflow_contexts.py`,
    `tests/test_workflow_edge_cases.py` and `tests/test_workflow_fail_closed.py`

**Interfaces:**
- **The `front_end` fixture** (`params=FRONT_ENDS`). For `"ninja"`, it sets
  `settings.ROOT_URLCONF = "tests.ninja_urls"` and clears the URL caches. A
  test opts in by taking the fixture.
- **`api_url(front_end, name, ...)`** reverses `name` for DRF and
  `"scrud-ninja:" + name` for ninja.
- **Authentication** moves from `force_authenticate` to `force_login` in the
  parametrized tests only. It works on both front ends: session
  authentication on DRF, `resolve_user` on ninja.
- **Tests that exercise DRF internals stay DRF-only**, unparametrized:
  - DRF viewset subclasses (`test_options`' rewriting tests,
    `test_conditional_create`'s custom viewsets);
  - `test_workflow_permission_class`;
  - routes only `tests/urls.py` has (`denying/`, `other-iri/`,
    `object-for-checks/`, `post-save-refusing/`, `filtered/`).

  Leave each with a one-line comment saying why it is DRF-only.

- [ ] **Step 1: Convert one suite** (`tests/test_rest.py`): take
  `front_end`, use `api_url`, and log in with `force_login`. The expected
  values stay the same. Literal paths like `/partner-profiles/` become
  `api_url(...)`.

- [ ] **Step 2: Run it.** Run `uv run pytest tests/test_rest.py`. Both
  parameters must pass. A ninja failure here is a front-end bug: fix it in
  `scrud_django/ninja/`, never in the assertion.

- [ ] **Step 3: Convert the remaining suites the same way**, one commit per
  suite. Each commit message names the suite and says the assertions are
  unchanged.

- [ ] **Step 4: Run** `uv run pytest -q` and `uv run pytest -q --reverse`.

- [ ] **Step 5: Commit per suite.** Use
  `test(<suite>): run on both front ends`.

### Task 10: Docs and follow-ups

**Files:**
- Modify: `README.md`, `docs/release-notes.md` (0.16.0), `docs/follow-ups.md`,
  `CLAUDE.md`, and the Sphinx page that documents registration if one exists
  (`docs/*.md`)

- [ ] **Step 1: README.** Add a section "django-ninja (preview)". It covers:
  - `pip install 'scrud-django[ninja]'`;
  - `scrud_django.ninja.api(types=[json_resource_type_spec(...)])` and
    `path("api/", api.urls)`;
  - not setting `auth=`;
  - distinct `urls_namespace`s for two APIs;
  - that the UI and studio types are not served on ninja yet;
  - that `scrud_django.urls` stays DRF until the release's switch.
- [ ] **Step 2: Release notes.** In 0.16.0, add the `ninja` extra, `TypeSpec`
  and the `*_spec` functions, and the namespace-aware links. Nothing breaking
  in 3A.
- [ ] **Step 3: `docs/follow-ups.md`.** Add the upstream proposals, each as a
  section with its test:
  - `scoped-rbac-list-iri`: a `list_iri=` parameter (§3.5 item 1);
  - `scoped-rbac-crud-router-checks`: extra checks (§3.5 item 2);
  - `scoped-rbac-options-denials`: a public `_options_denials` (§3.5 item 3);
  - `scoped-rbac-ninja-docs-pin`: the stale docs (§3.5 item 4);
  - `scoped-rbac-ninja-auth-digest`: the auth-loop digest knows only ninja
    1.7.0/1.7.1, so 1.7.2/1.7.3 CSRF-check header-credential users.

  Also record R1–R5 under `# Decided`, pointing at this plan.
- [ ] **Step 4: CLAUDE.md "Architecture".** Add a short paragraph saying:
  - `scrud_django/ninja/` is the second adapter;
  - its handlers call only the core facade;
  - parity is `tests/test_front_end_parity.py`.
- [ ] **Step 5: Verify and commit.**
  - Run `uv run --group docs sphinx-build -W -b html docs docs/_build`,
    `uv run ruff check .` and `uv run pytest -q`.
  - Commit with message `docs: the ninja front end (preview), proposals upstream`.
