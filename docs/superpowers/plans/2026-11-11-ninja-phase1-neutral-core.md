# Neutral Core (ninja phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract a framework-neutral core, `scrud_django.core`, out of the DRF viewsets. Reads, lists, writes, workflow and per-type authorization hooks become callable without DRF. The DRF viewsets become thin adapters over the core, and every DRF answer stays byte-for-byte the same.

**Architecture:**
- **Move, then delegate.** Each task moves one slice of logic from `mixins/view_mixins.py`, `filters.py`, `validators.py` or `permissions.py` into a `scrud_django/core/` module. Its inputs become a Django `HttpRequest` (with `.user`), the `ResourceType`, plain mappings, and a neutral **requirement** (Task 2) in place of the DRF view. The DRF method that held the logic then calls the core function.
- **The suite is the oracle.** The existing suite is not edited, apart from tests that pin moved internals (`tests/test_upstream_drift.py`, import paths). New unit tests call the core with `RequestFactory` requests, never a DRF `Request`.

**Tech Stack:** Python 3.11+, Django 5.2, DRF (still installed), django-scoped-rbac 0.12.1, scrudful 0.10.1, safetydance 0.5 tests.

**Spec:** `docs/superpowers/specs/2026-11-11-ninja-front-end-design.md`, §"Phase 1" (1.1–1.4) and the Anti-goals. Read it first. Then read `CLAUDE.md`, and the docstrings of `LockedWriteMixin`, `ListValidatorsMixin` and `ResourceViewMixin` (`scrud_django/mixins/view_mixins.py:459-1583`), which state the behaviour that must survive.

## Global Constraints

- **Branch.** Work on `feature/neutral-core`, created from `docs/ninja-front-end-design` so the spec travels with it. Merge into `develop`. Don't bump the version: it stays `0.16.0` (owner: the ninja work folds into 0.16.0).
- **No answer changes.**
  - Every status, header and body the DRF routes and the UI give today stays the same, and so does the order of checks.
  - `uv run pytest -q` and `uv run pytest -q --reverse` pass at every commit.
  - Only `tests/test_upstream_drift.py` and import-path lines may change in existing tests. Any other existing-test edit means the refactor changed behaviour: stop and fix the code instead (CLAUDE.md: never weaken an assertion).
- **Neutral means neutral.**
  - Nothing under `scrud_django/core/` imports `rest_framework`, `django_filters`, `scrudful.rest_framework`, `scoped_rbac.rest`, `scoped_rbac.filters`, or `scoped_rbac.permissions`.
  - It may use `scoped_rbac.access`, `.policy`, `.conditional`, `.problems`, `.cel`, `.conf`, `.rbac_contexts`, `scrudful.conditional`, and `scrudful._django` only where the DRF code already used it.
  - Task 1's test enforces this for every module under `scrud_django/core/`.
- **Errors.** Core code raises `scrud_django.exceptions.ScrudException` subclasses, `scoped_rbac.access.AccessError` subclasses, or `django.http.Http404`, never DRF exceptions. The DRF adapter maps them: `drf_error(...)` for access errors, as today.
- **Public names stay.** Every name importable today from `scrud_django.*` (`views`, `exceptions`, `permissions`, `filters`, `validators`, `mixins.view_mixins`, `headers`, `registration`) still imports and behaves the same. Moved functions stay importable from their old module by re-export.
- **URLs.** scrud configures no DRF versioning, so `rest_framework.reverse.reverse(name, args, request=r)` equals `r.build_absolute_uri(django.urls.reverse(name, args=args))`. Use the latter in core.
- **Repo conventions (CLAUDE.md).**
  - safetydance tests; `@pytest.mark.django_db` above `@test_context`; steps only in `tests/steps.py`; settings only in `tests/settings.py::SETTINGS`.
  - The code must work on all four databases: run the PostgreSQL and MySQL suites (CLAUDE.md commands) after Tasks 5, 6 and 9.
  - Run `ruff format` and `ruff check --fix` only on touched files.
  - Each commit ends with `Claude-Session: https://claude.ai/code/session_01SAZeWvu8MUgWeP2fTMBG43`.
- **Verify every task** with `uv run pytest -q`, `uv run pytest -q --reverse`, `uv run ruff check .`, and `uv run ruff format --check .`.

## Review Focus

1. **An adopter's own DRF `EXCEPTION_HANDLER`, not scrud's, renders a scrud error.**
   - Today a `ScrudException` is an `APIException`, so DRF's default handler answers `{"detail": message}` with the error's status.
   - After Task 1 it must still. The DRF views wrap a neutral `ScrudException` in an `APIException` before any handler sees it.
   - Test in Task 1.
2. **Core functions called with a plain Django request.** There is no `request.data`, `request.query_params` or `request.accepted_renderer`. The user comes from `request.user`, the query from `request.GET`, and the body is passed in. Every core unit test in Tasks 3–7 builds its request with `django.test.RequestFactory` and sets `.user`.
3. **The workflow check runs exactly once per write.** After Task 3, the DRF validator and the core must not both evaluate it: a double evaluation would double-mark the request and double-log. Test in Task 6.
4. **A slug registered again** (URLconf re-import under `DEBUG`, or a test URLconf registering a slug the default URLconf also has) replaces its type options. It must not merge them or keep stale hooks. Test in Task 8.
5. **The neutral requirement must match DRF's on composed permissions.** DRF's `&`, `|` and `~` over `IsAuthorized` subclasses (`tests/urls.py`) must give the same list filtering and 404/403 through the neutral requirement as before. A `~IsAuthorized` (NOT) has no neutral form, so such a view must keep using its DRF path. Test in Task 2.

---

### Task 1: DRF-free imports and neutral errors

**Files:**
- Create: `scrud_django/core/__init__.py` (empty docstring module), `scrud_django/core/authz.py`
- Modify:
  - `scrud_django/exceptions.py:1-48`: `ScrudException` base, lazy DRF imports in `scrud_exception_handler`;
  - `scrud_django/permissions.py`: move the neutral parts out;
  - `scrud_django/conf.py:9,22`: `import_string`;
  - `scrud_django/__init__.py:9,37`: reverse;
  - `scrud_django/apps.py`: the operator import;
  - `scrud_django/checks.py`: make any module-level DRF/viewset import lazy;
  - `scrud_django/mixins/view_mixins.py`: `ResourceViewMixin.handle_exception`;
  - `scrud_django/computed.py`, `scrud_django/views.py`: views that are not `ResourceViewMixin`s get the same wrapping through a small mixin.
- Test: `tests/test_core_imports.py`, `tests/test_exceptions.py` (additions only)

**Interfaces:**
- Produces:
  - **`scrud_django.core.authz`**, moved verbatim from `permissions.py:68-205` and `:332-400`, all re-exported from `scrud_django.permissions`:
    - `current_transition`, `_checking_transition`, `_checking_write`, `_not_checking_write`;
    - `_mark_workflow_authorized`, `_workflow_authorized`;
    - `content_of`, `resource_contexts`, `with_context`, `in_context`, `condition_resource`;
    - `may_act_in_context`, `may_read_in_context`;
    - `authorized_transitions`, `_report_malformed`.

    `reads_each_context`, `_leaves`, `reading_content` and `checks_read_content` stay in `permissions.py`, because they read DRF permission classes.
  - **`ScrudException(Exception)`** keeps `status_code`, `default_detail`, `default_code`, `.detail`, and `response_data(context)`. `context` is a mapping with `"request"`. `.detail` is a `str` subclass with a `.code` attribute (`scrud_django.exceptions.ErrorText`), so code reading `exc.detail` and `exc.detail.code` still works.
  - **`scrud_django.exceptions.as_drf(exc: ScrudException) -> APIException`** wraps the error in a `ScrudAPIException`, an `APIException` defined inside a function, or behind a lazy import, so that the module imports without DRF. Its `status_code` and `detail` (`ErrorDetail(message, code=default_code)`) are the error's, and `.scrud` holds the original.
  - **`scrud_exception_handler`** recognises both `ScrudException` and `ScrudAPIException` (unwrapping `.scrud`) and answers exactly as today.

- [ ] **Step 1: Write the failing tests**

  - **`tests/test_core_imports.py::test_setup_and_core_import_without_drf`**
    - Runs `sys.executable -c <script>` in a subprocess, with `cwd` the repo root.
    - The script installs a `sys.meta_path` finder that raises `ModuleNotFoundError` for `rest_framework`, `django_filters`, `scrudful.rest_framework` and `scoped_rbac.rest` and their submodules.
    - It calls `settings.configure(INSTALLED_APPS=["django.contrib.auth", "django.contrib.contenttypes", "reversion", "scoped_rbac", "scrud_django"], DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}})` and then `django.setup()`.
    - It imports `scrud_django.models`, `scrud_django.workflow`, `scrud_django.exceptions`, and every module under `scrud_django/core/` (found with `pkgutil.walk_packages`).
    - Asserts the return code is 0, printing stderr on failure.
  - **`tests/test_core_imports.py::test_core_modules_import_no_drf`**
    - Parses each `scrud_django/core/**/*.py` with `ast`.
    - Asserts that no `Import`/`ImportFrom` names a module in the neutral-means-neutral deny list (Global Constraints).
  - **`tests/test_exceptions.py::test_scrud_error_under_drf_default_handler`**
    - Under `override_settings(REST_FRAMEWORK={**current, "EXCEPTION_HANDLER": "rest_framework.views.exception_handler"})`, a POST that fails JSON Schema validation is a 400.
    - Its body is `{"detail": <the JsonValidationError message>}`, as before.
    - Assert the exact body, captured on `develop` before the change.
  - **`tests/test_exceptions.py::test_scrud_error_is_not_api_exception`**
    - `not isinstance(JsonValidationError(...), APIException)`.
    - `isinstance(as_drf(e), APIException)`, and `as_drf(e).detail.code == "json_invalid"`.

- [ ] **Step 2: Run them to verify they fail**
  Run: `uv run pytest tests/test_core_imports.py tests/test_exceptions.py -q`
  Expected: the subprocess test fails, with `ModuleNotFoundError: rest_framework` from `scrud_django/conf.py` or `exceptions.py`; `test_scrud_error_is_not_api_exception` fails.

- [ ] **Step 3: Implement**
  - Move the neutral `permissions.py` functions to `core/authz.py` and re-export them.
  - Swap `rest_framework.settings.import_from_string` for `django.utils.module_loading.import_string`, wrapped to raise the same `ImportError` message DRF's gives: `"Could not import '%s' for API setting '%s'. %s: %s."`.
  - Make `scrud_django/__init__.py`'s service URL use `request.build_absolute_uri(django.urls.reverse(...))` when given a request, and the relative path otherwise (DRF's behaviour).
  - Re-base `ScrudException` as specified.
  - Add `handle_exception(self, exc)` to `ResourceViewMixin`, `GenericViewMixin`, and a new `scrud_django.mixins.view_mixins.ScrudErrorsMixin` used by the `APIView`s in `views.py` and `computed.py`. It does `super().handle_exception(as_drf(exc) if isinstance(exc, ScrudException) else exc)`.
  - The UI's `@api_view` functions that raise scrud errors (`grep -rn "raise UI" scrud_django/ui`) get the same wrapping through a decorator `scrud_django.exceptions.drf_errors`.

- [ ] **Step 4: Run the tests and the suite**
  Run: `uv run pytest -q` and `uv run pytest -q --reverse`
  Expected: all pass, with no edits to existing assertions.

- [ ] **Step 5: Commit** `refactor(core): scrud imports without DRF; neutral ScrudException`

### Task 2: The neutral requirement

**Files:**
- Create: `scrud_django/core/requirements.py`, `scrud_django/drf_requirements.py`
- Modify: `scrud_django/filters.py:274-466`, which keeps the view-taking functions as wrappers.
- Test: `tests/test_core_requirements.py`

**Interfaces:**
- Produces, in `core/requirements.py`:
  - **`class Leaf(Protocol)`**, a view-free `IsAuthorized`:
    - `type_iri: str`
    - `reads_each_context: bool`
    - `overrides_object_checks: bool`
    - `contexts_for(obj: Mapping) -> list[str]`
    - `has_permission(request, body: Mapping | None) -> bool`
    - `has_object_permission(request, obj: Mapping) -> bool`
    - `has_context_permission(request, obj: Mapping, context: str) -> bool`
  - **`Requirement = list[Leaf | scoped_rbac.access.AnyOf]`**: every entry must allow; an `AnyOf` holds alternatives, as scoped-rbac's `rbac_requirement`.
  - **`class GrantsLeaf`** gives scoped-rbac's default `IsAuthorized` answers for `type_iri`, with contexts read from the content (`rbac_contexts_of`). `overrides_object_checks = False` and `reads_each_context = True`.
  - **Functions moved from `filters.py`,** taking `(requirement, request, obj, ...)` where they took `view`:
    - `grants_allow(requirement, request, obj, permission) -> bool`
    - `readable_by_grants(requirement, request, obj) -> bool`
    - `meets_object_checks(requirement, request, obj) -> bool`
    - `readable_by_object_checks(requirement, request, obj) -> bool`
    - `object_check_requirement(requirement) -> Requirement | None`
    - `filter_by_object_checks(queryset, request, requirement, object_for_checks) -> QuerySet`
    - `readable_removal(request, requirement, resource_type, using=None)`

    "As a GET of the detail route" becomes: the caller passes a GET clone of the request (`core.requirements.as_get(request) -> HttpRequest`, a shallow copy with `method = "GET"`).
- Produces, in `drf_requirements.py`:
  - **`class ViewLeaf`** implements `Leaf` over an `IsAuthorized` instance and its view. It calls `permission.has_object_permission(request, view, obj)` under `filters.as_detail_route(view)` when the request is a GET clone.
  - **`requirement_of(view) -> Requirement`** maps `view.rbac_requirement()` (or the view's `IsAuthorized` leaves) to `ViewLeaf`s, keeping `AnyOf`s.
  - **NOT is refused:** `requirement_of` raises `NotNeutral` when a `~` (DRF `NOT`) wraps an `IsAuthorized`. The `filters.py` wrappers then keep their current view-based code path for that view (Review Focus 5).

- [ ] **Step 1: Write the failing tests** (`tests/test_core_requirements.py`)
  - **`test_grants_leaf_matches_is_authorized`**
    - For a matrix of users (`tests/fixtures.py`: superuser, a user with GET in context `a`, one with a CEL-conditioned GET, anonymous) and resources in contexts `""`, `"a"` and `["a", "b"]`:
    - `readable_by_grants([GrantsLeaf(NOTE)], as_get(r), content)` equals `filters.readable_by_grants(view, ...)` for a `ResourceViewSet` of the same type.
  - **`test_view_leaf_composed_permissions`**
    - For the composed viewsets in `tests/urls.py` (the `&` and `|` ones; find them by grepping `&`/`|` there), `filter_by_object_checks(qs, r, requirement_of(view), content_of)` returns the same pks as today's view-based call.
  - **`test_not_permission_is_not_neutral`**: `requirement_of(view_with(~IsAuthorized))` raises `NotNeutral`.
- [ ] **Step 2: Run them to verify they fail** (ImportError).
- [ ] **Step 3: Implement** the move. The `filters.py` view functions become `return core_fn(requirement_of(view), ...)`, except where `NotNeutral` is raised: there they keep the existing body.
- [ ] **Step 4: Run the new tests and the suite.** Expected: all pass.
- [ ] **Step 5: Commit** `refactor(core): neutral authorization requirement`

### Task 3: Workflow as a neutral check

**Files:**
- Create: `scrud_django/core/workflow.py`
- Modify: `scrud_django/validators.py:199-496` (`WorkflowValidator` becomes an adapter), `scrud_django/mixins/view_mixins.py:1151-1182` (`write_checks` delegates)
- Test: `tests/test_core_workflow.py`

**Interfaces:**
- Consumes: `Leaf` and `Requirement` (Task 2); `core.authz` (Task 1).
- Produces, in `core/workflow.py`:
  - **`authorizing_write(request, *, covered: bool, authorized: bool = False) -> ContextManager`**, today's `write_checks`.
    - When `covered` is true (and, with `authorized`, only once the request is marked), `scrud_workflow` allows inside the block (`_checking_write`).
    - Otherwise it refuses (`_not_checking_write`).
    - `ResourceViewMixin.write_checks` becomes `authorizing_write(request, covered=self.checks_workflow(request), authorized=authorized)`.
  - **`workflow_check(request, resource_type, *, stored: Mapping | None, body: Mapping, contexts_before: list[str] | None = None, contexts_after: list[str] | None = None, narrow: Sequence[Leaf] = (), readable: Callable[[], bool] | None = None) -> list[tuple[str | None, WorkflowPolicy, TransitionData]]`**
    - It is `WorkflowValidator.__call__`'s algorithm, minus `validate_policy`.
    - Contexts default to `rbac_contexts_of(stored)` and `rbac_contexts_of(body)`.
    - `narrow` replaces `rbac_permissions_of(view)`: each leaf's `has_context_permission(request, body, context)` runs under `_checking_transition`.
    - `readable` replaces `errors_hidden_unless_readable`'s call to the view's `object_readable`. `None` means the stored resource is treated as readable.
    - It marks the request (`_mark_workflow_authorized`) and returns the transitions it authorized.
  - **`validate_policy_write(body: Mapping, stored_pk: int | None) -> None`**, moved from `WorkflowValidator.validate_policy` plus `check_policy_is_unique`.
- `WorkflowValidator.__call__` keeps its signature and DRF behaviour:
  - it builds `stored`, `body` (`body_of`), `narrow` (`[ViewLeaf(p, view) for p in rbac_permissions_of(view)]`) and `readable`;
  - it calls `validate_policy_write` for a `WorkflowPolicy` type, then `workflow_check`.

- [ ] **Step 1: Write the failing tests** (`tests/test_core_workflow.py`, with a stored two-state `WorkflowPolicy` for `NOTE` in context `"a"`, as `tests/test_rest_workflow.py` sets one up)
  - **`test_allowed_transition_marks_request`**: `workflow_check(r, rtype, stored={"state": "draft", "rbac_context": "a"}, body={"state": "published", "rbac_context": "a"})` returns one transition named as in the policy. `_workflow_authorized(r)` is then true.
  - **`test_refused_transition`**: a user without the transition grant gets `WorkflowTransitionError` (status 403, or as `test_rest_workflow.py` asserts for the same case).
  - **`test_hidden_when_unreadable`**: with `readable=lambda: False`, the same refusal is `HiddenWorkflowTransitionError`.
  - **`test_context_without_policy`**: a write in context `"b"` (no policy) by a user whose only grant is `scrud_workflow`-conditioned raises `access.AccessDenied`.
  - **`test_narrow_refuses`**: a `narrow` leaf whose `has_context_permission` returns False refuses the transition.
- [ ] **Step 2: Run them to verify they fail.**
- [ ] **Step 3: Implement** the move and the adapter.
- [ ] **Step 4: Run** the new tests, then `tests/test_rest_workflow.py tests/test_default_workflow_policies.py tests/test_custom_operators.py`, then the suite. Expected: all pass.
- [ ] **Step 5: Commit** `refactor(core): workflow check without DRF`

### Task 4: Bodies and links

**Files:**
- Create: `scrud_django/core/bodies.py`, `scrud_django/core/urls.py`
- Modify: `scrud_django/mixins/view_mixins.py:149-276` (the body checks become wrappers), `scrud_django/headers.py` (no DRF `reverse`)
- Test: `tests/test_core_bodies.py`

**Interfaces:**
- Produces, in `core/bodies.py`:
  - **`check_body(body: object, *, present: bool) -> None`**. In order: missing (`RequestBodyMissingError`, as `check_body_present`), depth (`RequestBodyTooDeepError`, `max_body_depth()`), NUL (`RequestBodyNulError`), then written contexts (`check_written_contexts`' errors).
  - The DRF wrappers compute `present` and `body` from `request.stream` and `request.data` exactly as today, then call it.
- Produces, in `core/urls.py`:
  - **`absolute_reverse(name: str, *, args=(), kwargs=None, request=None) -> str`**.
  - `headers.get_schema_uri_for`, `get_context_uri_for` and `get_link_header_for` use it. Their signatures are unchanged.
- [ ] **Step 1: Write the failing tests**
  - `test_check_body_order`: a body that is both too deep and has a NUL gets the depth error (today's order).
  - `test_missing_body`: `present=False` gives `RequestBodyMissingError`.
  - `test_absolute_reverse_matches_drf`: for `"collections-json-schema"` with args `["notes"]` and a `RequestFactory` request with host `testserver`, the result equals `rest_framework.reverse.reverse(...)` (DRF imported in the test only).
- [ ] **Step 2: Fail. Step 3: Implement. Step 4: Suite passes.**
- [ ] **Step 5: Commit** `refactor(core): body checks and links without DRF`

### Task 5: Lists

**Files:**
- Create: `scrud_django/core/lists.py`
- Modify:
  - `scrud_django/mixins/view_mixins.py:701-1058`: `ListValidatorsMixin` delegates;
  - `:1555-1583`: `list_last_modified`;
  - `scrud_django/filters.py:505-620`: `ResourceRbacFilter` and `ResourceContentFilter` delegate.
- Test: `tests/test_core_lists.py`

**Interfaces:**
- Consumes: `Requirement` and `filter_by_object_checks` (Task 2).
- Produces, in `core/lists.py`:
  - **`readable_queryset(request, resource_type, requirement, *, using: str | None = None) -> QuerySet[Resource]`**
    - the type's resources (`Resource.objects.filter(resource_type=...)`, on `using`);
    - filtered by `filter_readable_resources` for `http.GET` on the item type;
    - then narrowed by `filter_by_object_checks` when `object_check_requirement(requirement)` is not None;
    - `ConditionalListTooLarge` propagates.
    - This is today's order: the RBAC filter, then object checks, then other filters.
  - **`content_filtered(queryset, params: QueryDict, *, ignored: frozenset[str]) -> QuerySet`**: `ResourceContentFilter.filter_queryset`'s logic, with the same 400s raised as `ScrudException`s or `access` errors. `ignored` is `{"page", "page_size", "format"} | ui_parameters()`, computed by the caller.
  - **`list_etag(resource_type, *, items: Sequence[Resource], count: int, query: list[tuple[str, list[str]]]) -> str`**: the exact formula of `view_mixins.py:876-901`. `query` is `sorted(QueryDict.lists())`.
  - **`list_last_modified(request, resource_type, queryset, requirement) -> datetime | None`**: the newest `modified_at`, or `readable_removal(...)` on `queryset.db`, whichever is later.
  - **`evaluate_list_preconditions(method: str, headers, validators: Callable[[], tuple[str | None, datetime | None]]) -> scrudful.conditional result | None`**
    - It is `list_precondition_response`'s evaluation, minus rendering.
    - `validators` is called only when `list_validators_needed(headers)`.
    - Rendering (`_early_response`) stays in the DRF mixin.
- [ ] **Step 1: Write the failing tests**
  - `test_list_etag_matches_drf`: GET `/notes/?page=2&page_size=1` as a user who sees 3 of 5 notes, through the DRF route. Its `ETag` equals `quote_etag(list_etag(rtype, items=<page 2>, count=3, query=[("page", ["2"]), ("page_size", ["1"])]))`.
  - `test_readable_queryset_object_checks`: with a `Requirement` holding a leaf whose `has_object_permission` refuses pk X, `readable_queryset` excludes X, and still counts it against `CONDITIONAL_LIST_MAX_ROWS` (set to 1 → `ConditionalListTooLarge`).
  - `test_content_filtered_ignores_paging_and_ui`: `?page=1&ui-mode=edit&text=a` filters on `text` only.
  - `test_list_last_modified_includes_removal`: deleting a readable note moves it.
- [ ] **Step 2: Fail. Step 3: Implement the move.**
- [ ] **Step 4:** Run `tests/test_list_rbac.py tests/test_list_removals.py tests/test_list_object_checks.py tests/test_conditional*.py tests/test_query_budget.py`, then the suite.
  - Then run the PostgreSQL and MySQL suites (CLAUDE.md).
  - Expected: all pass, with **no new queries** in `test_query_budget.py`.
- [ ] **Step 5: Commit** `refactor(core): list queryset, validators and preconditions`

### Task 6: Writes

**Files:**
- Create: `scrud_django/core/writes.py`
- Modify:
  - `scrud_django/mixins/view_mixins.py:1288-1522`: `perform_create`, `check_post_save_permissions`, `check_every_context_of_policy`, `serialized_policy_save`, `recheck_saved`, `save_checked`, `cache_resource_type`, `create_resource`, `update`, `destroy` delegate;
  - `scrud_django/validators.py`: `JsonValidator` delegates its validation.
- Test: `tests/test_core_writes.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces, in `core/writes.py`:
  - **`validate_write(request, resource_type, body: Mapping, *, stored: Resource | None, narrow: Sequence[Leaf], readable: Callable[[], bool] | None) -> list[TransitionData]`**
    - JSON Schema first (`JsonValidator.validator_for(resource_type)`, raising `JsonValidationError`);
    - then `validate_policy_write` for a `WorkflowPolicy` type;
    - then `workflow_check`.
    - This is the order `ResourceSerializer.Meta.validators` gives today. The serializer's validators call the first part and the second part respectively, so DRF evaluates each once (Review Focus 3).
  - **`check_post_save(request, resource_type, obj: Resource, requirement) -> None`**: `check_post_save_permissions`' algorithm over `Leaf`s, raising `access.AccessDenied`.
  - **`save_checked(request, resource_type, save: Callable[[], Resource | None], *, instance: Resource | None, requirement, using: str, covered: bool) -> Resource`**
    - It is today's method: a `SaveCheck` in `transaction.atomic(using)`, the policy serialisation for `WorkflowPolicy`, the post-save recheck under `authorizing_write(request, covered=covered, authorized=True)`, and re-caching the type.
    - It raises `access.AccessError`; the DRF wrapper applies `drf_error`.
  - **`check_every_context_of_policy(request, instance, requirement, *, covered: bool) -> None`**
  - **`create(request, resource_type, body, *, requirement, using, covered=True) -> Resource`**: `register_instance` inside `save_checked`, with reversion when `uses_reversion`.
  - **`update(request, resource_type, instance, body, *, requirement, using, covered=True) -> Resource`**: `update_locked` inside `save_checked`.
  - **`destroy(request, resource_type, instance, *, requirement, using) -> None`**: `delete_locked`, with `ProtectedError` mapped to `ResourceInUseError`.
  - **`write_transaction(using: str) -> ContextManager[Callable[[], None]]`**: `transaction.atomic(using)` yielding a `rollback()` callable (`transaction.set_rollback(True, using=using)`). `LockedWriteMixin.dispatch` uses it; it keeps deciding *when* to roll back (an error status) and refusing an unevaluated conditional create, because those depend on DRF's response.
  - **`conditional_create_lock(resource_type, using: str) -> ContextManager`**: `type_locked(resource_type, using)`. `ResourceViewMixin.conditional_create_lock` delegates.
  - **`write_db()` and `type_locked(...)`** stay in `registration`; core imports them.
  - **`destroy` runs no workflow check,** as today (follow-up `delete-no-workflow-check`); don't fix it here.
  - `create`/`update` do **not** call `validate_write`: the adapter calls it first (DRF through the serializer, ninja in phase 3).
- [ ] **Step 1: Write the failing tests** (`tests/test_core_writes.py`)
  - `test_create_and_post_save_denial_rolls_back`:
    - A user may POST in `"a"` but not in `"b"`.
    - `create(r, rtype, {"text": "x", "rbac_context": ["a", "b"]}, requirement=[GrantsLeaf(NOTE)], using="default")` raises `access.AccessDenied`.
    - No `Resource` row remains.
  - `test_update_returns_saved`: content, `etag` and `modified_at` advance.
  - `test_destroy_in_use_is_409`: deleting the `json-schema` resource a type uses raises `ResourceInUseError`.
  - `test_workflow_evaluated_once` (Review Focus 3):
    - Patch `scrud_django.core.workflow.allowed_on` with a counting wrapper.
    - A DRF PUT that makes one transition in one context calls it exactly once (`allowed_on` runs once per context and loops over the narrowing leaves itself).
- [ ] **Step 2: Fail. Step 3: Implement the move.**
- [ ] **Step 4:** Run `tests/test_rest.py tests/test_atomic_writes.py tests/test_delete_locking.py tests/test_conditional_create.py tests/test_policy_duplicates.py tests/test_post_commit.py tests/test_reversion.py`, then the suite, then PostgreSQL and MySQL. Expected: all pass.
- [ ] **Step 5: Commit** `refactor(core): writes, post-save checks and policy serialisation`

### Task 7: Reads and `Allow`

**Files:**
- Create: `scrud_django/core/resources.py`, the facade that phase 3 calls.
- Modify: `scrud_django/mixins/view_mixins.py:701-799,1221-1283` (`object_readable`, `check_object_permissions`, `check_grants_in_each_context`, `get_object` delegate)
- Test: `tests/test_core_resources.py`

**Interfaces:**
- Consumes: Tasks 2, 5 and 6.
- Produces, in `core/resources.py`, which also re-exports `create`, `update`, `destroy`, `validate_write`, `readable_queryset`, `content_filtered`, `list_etag`, `list_last_modified` and `evaluate_list_preconditions`:
  - **`object_readable(request, resource_type, obj: Resource, requirement) -> bool`**: `readable_by_grants` on a GET clone, **and** `readable_by_object_checks`, under `_not_checking_write()`.
  - **`load_for_read(request, resource_type, pk, *, requirement, method: str, lock_using: str | None = None) -> Resource`** (the spec's name; it serves every method's lookup, not only reads)
    - It reads from `readable_queryset`-equivalent filtering for the item. A reserved context gives `Http404`.
    - The object check for `method` runs under `authorizing_write(..., covered=method in ("PUT",))`.
    - A refusal of an unreadable object gives `Http404`; a refusal of a readable one gives `access.AccessDenied`.
    - With `lock_using` set, the row is read there with `select_for_update()`.
    - The type is cached on the instance, as `get_object` does.
  - **`item_validators(obj: Resource) -> tuple[str, datetime]`**: `(obj.etag, obj.modified_at)`.
  - **`item_links(request, resource_type) -> list[str]`**: the schema and context Link values `ScrudfulMeta` produces.
  - **`allowed_methods(request, resource_type, obj: Resource | None, requirement) -> list[str]`**: the methods DRF's OPTIONS lists today for that requester and object, in DRF's order.
- [ ] **Step 1: Write the failing tests**
  - `test_load_404_vs_403`:
    - A user who may GET but not PUT note N: `load_for_read(..., method="PUT")` raises `AccessDenied`.
    - A user who may not GET it: `Http404` for both methods.
    - Anonymous: `Http404`.
  - `test_allowed_methods_match_drf_options`: for the user matrix of Task 2 and a stored note, `allowed_methods(...)` equals the `Allow` header of a DRF OPTIONS on `/notes/<pk>/`, split and stripped. Do the same for the list (`obj=None`) and `/notes/`.
- [ ] **Step 2: Fail. Step 3: Implement.** The DRF `get_object` keeps DRF's `get_object_or_404` path and its subclass hooks (`get_queryset`, `filter_queryset`), and calls `object_readable`/`check_*` from the core.
- [ ] **Step 4: Suite passes,** including `tests/test_object_checks.py tests/test_object_check_readability.py tests/test_options.py tests/test_context_smuggling.py`.
- [ ] **Step 5: Commit** `refactor(core): reads, readability and Allow`

### Task 8: Per-type hooks at registration

**Files:**
- Create: `scrud_django/core/types.py`
- Modify:
  - `scrud_django/registration.py:799-899`: `json_resource_type`, `computed_resource_type`;
  - `scrud_django/mixins/view_mixins.py`: `ResourceViewMixin` adds the hook leaf to its requirement and honours `content_filter`;
  - `scrud_django/computed.py`: `compute` defaults to the registered callable.
- Test: `tests/test_type_hooks.py`; register its types in `tests/resource_types.py` and route them in `tests/urls.py`.

**Interfaces:**
- Produces, in `core/types.py`:
  - **`@dataclass(frozen=True) class TypeOptions`**: `checks: tuple[Callable] = ()`, `object_checks: tuple[Callable] = ()`, `content_filter: bool = False`, `compute: Callable | None = None`.
    - `checks` take `(request, resource_type, body: Mapping, context: str) -> bool`.
    - `object_checks` take `(request, resource_type, obj: Mapping) -> bool`.
    - `compute` takes `(request, key: str) -> Mapping`.
  - **`register_type_options(slug: str, options: TypeOptions) -> None`**: it **replaces** any earlier entry (Review Focus 4).
  - **`type_options(slug: str) -> TypeOptions`**: `TypeOptions()` when none is registered.
  - **`class HookLeaf(Leaf)`**: `has_context_permission` = all `checks`; `has_object_permission` = all `object_checks`; `overrides_object_checks` = `bool(object_checks or checks)`.
  - **`requirement_for(resource_type, slug) -> Requirement`**: `[GrantsLeaf(type_iri)]`, plus `HookLeaf` when there are hooks.
- Registration:
  - `json_resource_type(..., checks=(), object_checks=(), content_filter=False)`.
  - `computed_resource_type(..., compute=None)`; `viewset_class` stays optional there when `compute` is given.
  - DRF's `ResourceViewMixin` requirement is `requirement_of(view)` plus `HookLeaf` for the slug's hooks.
  - `content_filter=True` adds `ResourceContentFilter` to the view's `filter_backends`.
- [ ] **Step 1: Write the failing tests** (`tests/test_type_hooks.py`, with a type `hooked-notes` registered with `checks=(deny_context_b,)`, `object_checks=(hide_secret,)`, `content_filter=True`, and a computed type `summaries` with `compute=`)
  - `test_check_refuses_write_in_context`: POST to `hooked-notes` in `"b"` gives 403; in `"a"`, 201.
  - `test_object_check_hides_from_list_and_detail`: a note with `secret: true` is missing from the list, and its detail is 404 for a reader whose grants allow GET on it.
  - `test_content_filter`: `?text=a` filters.
  - `test_compute_hook`: GET `summaries/k/` returns `compute`'s mapping with a strong ETag (`computed_etag`).
  - `test_reregistering_replaces_options`: registering `hooked-notes` again with no hooks makes the context-`b` POST a 201.
- [ ] **Step 2: Fail. Step 3: Implement. Step 4: Suite passes.**
- [ ] **Step 5: Commit** `feat(core): per-type checks, object checks, content filter and compute hooks`

### Task 9: Drift pins, docs, acceptance

**Files:**
- Modify:
  - `tests/test_upstream_drift.py`: repoint pins at the core functions;
  - `README.md`: document the new `json_resource_type`/`computed_resource_type` arguments under "Registering resource types";
  - `docs/release-notes.md`: the 0.16.0 section gains "Framework-neutral core" and the new hooks;
  - `CLAUDE.md`: an "Architecture" note that domain logic lives in `scrud_django/core/` and DRF views delegate;
  - `docs/follow-ups.md`: note any behaviour found odd and left alone, in its format.
- Test: the whole suite on four databases, and the docs build.

- [ ] **Step 1:** Update the drift module docstring and pins: `filters.readable_by_grants` becomes `core.requirements.readable_by_grants`, and so on. Its assertions on upstream behaviour are unchanged.
- [ ] **Step 2:** Run the full verification:
  - `uv run pytest -q`, `uv run pytest -q --reverse`;
  - the PostgreSQL and MySQL suites;
  - `uv run ruff check .`, `uv run ruff format --check .`;
  - `uv run python manage.py check`;
  - `uv run python manage.py makemigrations scrud_django --check --dry-run`;
  - `uv run --group docs sphinx-build -W -b html docs docs/_build`.

  Expected: all pass, and `git diff develop -- tests/ ':!tests/test_core_*' ':!tests/test_type_hooks.py' ':!tests/test_upstream_drift.py' ':!tests/resource_types.py' ':!tests/urls.py'` shows only import-path changes.
- [ ] **Step 3: Commit** `docs: framework-neutral core; release notes`
