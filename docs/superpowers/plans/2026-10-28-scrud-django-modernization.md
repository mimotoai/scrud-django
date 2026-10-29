# scrud-django Modernization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** scrud-django installs with uv and its full test suite passes on Python 3.11–3.13 / Django 5.2 against the released django-scoped-rbac 0.11, scrudful 0.8, safetydance 0.5 and safetydance-django 0.1.

**Architecture:** Replace poetry/conda/rever tooling with a PEP 621 `pyproject.toml` + `uv.lock`; make the minimum code changes Django 5.2 and scoped-rbac 0.11 force; port the tests to safetydance 0.5; then use the suite to drive adopting scrudful (deleting `scrud_django/decorators.py`) and fixing the latent bugs it exposes. Finish with GitLab CI, docs, demo, and Claude Code setup.

**Tech Stack:** Python 3.11+, uv / `uv_build`, Django 5.2, DRF ≥3.15, django-scoped-rbac[drf] 0.11, scrudful[drf] 0.8, pytest + pytest-django, safetydance 0.5, safetydance-django 0.1, ruff, pre-commit, GitLab CI.

**Spec:** `docs/superpowers/specs/2026-10-28-scrud-django-modernization-design.md` — read it first; this plan argues from it.

## Global Constraints

- `requires-python = ">=3.11"`; verify on 3.11, 3.12, 3.13 (not 3.14).
- `django>=5.2,<6.0`.
- Runtime: `django-scoped-rbac[drf]>=0.11.1,<0.12`, `scrudful[drf]>=0.8,<0.9`, `djangorestframework>=3.15`, `django-filter`, `django-reversion`, `jsonpath-ng`, `fastjsonschema`.
- Extra `search`: `django-haystack`, `whoosh-reloaded`.
- Dev group (default): `safetydance>=0.5,<0.6`, `safetydance-django>=0.1,<0.2`, `pytest`, `pytest-django`, `pytest-cov`, `factory-boy`, `rdflib`, `ruff`, `pre-commit`, `django-extensions`, and `scrud-django[search]`. Group `docs`: `sphinx`, `myst-parser`, `sphinx-rtd-theme`.
- Build backend `uv_build`. Version lives only in `pyproject.toml` (`0.12.0`); `scrud_django.__version__` reads `importlib.metadata.version("scrud-django")`.
- Upstream packages are consumed from PyPI and never edited. Defects found in them go in `docs/follow-ups.md`.
- Never weaken or delete an assertion to get green. A behavior change gets an explicit new assertion; an unfixed failure gets `@pytest.mark.xfail(reason="docs/follow-ups.md#<anchor>", strict=True)` plus an entry in `docs/follow-ups.md`.
- `api.openteams.com` IRIs stay unchanged. No Postgres. No pushing to any remote.
- Work on branch `modernize-uv` cut from `develop`.

## Review Focus

1. A caller who can't read a resource sends a conditional PUT/DELETE/GET (If-Match / If-None-Match / If-Unmodified-Since) → 404, never 412/428/304. Test in Task 7.
2. A workflow policy containing a CEL syntax error → `ImproperlyConfigured` when the policy is compiled, not a policy that silently never matches. Test in Task 3.
3. A JSON-schema validation failure → 400 with `application/problem+json` and scrud's `type`/`name`/`value`/`rule`/`rule_definition` body, under the combined exception handler; a non-scrud error (e.g. 404) gets scoped-rbac's problem body. Test in Task 4.
4. PATCH on a resource without If-Match/If-Unmodified-Since → 428. Test in Task 7.
5. GET with `If-Modified-Since` equal to the resource's `Last-Modified` → 304 (this crashed with `TypeError` before). Test in Task 7.

---

### Task 1: Branch, packaging, tooling cleanup

**Files:**
- Modify: `pyproject.toml` (full rewrite), `scrud_django/__init__.py:5`, `.gitignore`, `.pre-commit-config.yaml` (rewrite)
- Create: `uv.lock` (generated)
- Delete: `environment.yml`, `docker/`, `.github/`, `package.json`, `package-lock.json`, `rever.xsh`, `news/`, `MANIFEST.in`, `.flake8`, `pytest.ini`

**Interfaces:**
- Produces: `uv sync` environment with every dependency in Global Constraints; `[tool.pytest.ini_options]` with `testpaths = ["tests"]` and `python_files = ["tests.py", "test_*.py", "*_tests.py"]`; `[tool.ruff]` config.

- [ ] **Step 1: Create the branch and commit the spec and this plan**

```bash
git switch -c modernize-uv develop
git add docs/superpowers
git commit -m "docs: add modernization design and plan"
```
The working tree's existing `pyproject.toml` edit and `poetry.lock` deletion carry over and are committed in Step 6.

- [ ] **Step 2: Rewrite `pyproject.toml`**

PEP 621 `[project]` with name `scrud-django`, version `0.12.0`, description and author as today (`David Charboneau <david@adadabase.com>`), `license = "BSD-3-Clause"` (matches `LICENSE`), `readme = "README.md"`, the dependencies/extras/groups in Global Constraints, `[project.urls] Repository = "https://gitlab.com/dcharbon/scrud-django"`, `[build-system]` `uv_build>=0.9,<0.10`, `[tool.uv.build-backend] module-root = ""` (package is at repo root, not `src/`), `[tool.uv] default-groups = ["dev"]`. Copy `[tool.ruff*]` from `~/dev/django-scoped-rbac/pyproject.toml` (read-only), set `target-version = "py311"`, `line-length = 88`, `[tool.ruff.format] quote-style = "preserve"` (the codebase used black with skip-string-normalization). Delete the `[settings]`, `[tool.black]`, `[tool.isort]`, `[tool.poetry*]` blocks. Add `[tool.pytest.ini_options]` per Interfaces.

- [ ] **Step 3: Version from metadata**

`scrud_django/__init__.py`: replace the hard-coded `__version__ = "0.1.9"` with `importlib.metadata.version("scrud-django")`.

- [ ] **Step 4: Delete obsolete files; rewrite `.pre-commit-config.yaml` and `.gitignore`**

pre-commit: `astral-sh/ruff-pre-commit` (`ruff-check --fix`, `ruff-format`) and `pre-commit-hooks` (`trailing-whitespace`, `end-of-file-fixer`, `check-yaml`, `check-toml`), current revs. `.gitignore`: remove the pipenv, pyflow, rever, Scrapy, Flask sections; add `.ruff_cache/`.

- [ ] **Step 5: Lock and verify**

Run: `uv lock && uv sync && uv run python -c "import scrud_django; print(scrud_django.__version__)"`
Expected: prints `0.12.0`; the lock resolves Django 5.2.x, django-scoped-rbac 0.11.x, scrudful 0.8.x, safetydance 0.5.x, safetydance-django 0.1.x (`uv tree --depth 1` to confirm).

- [ ] **Step 6: Commit packaging**

```bash
git add -A pyproject.toml uv.lock scrud_django/__init__.py .gitignore .pre-commit-config.yaml
git add -A environment.yml docker .github package.json package-lock.json rever.xsh news MANIFEST.in .flake8 pytest.ini poetry.lock
git commit -m "build: move from poetry to uv; replace black/isort/flake8 with ruff"
```

- [ ] **Step 7: Apply ruff formatting as its own commit**

Run: `uv run ruff format . && uv run ruff check --fix .` then commit (`style: ruff format`). Remaining lint findings that need code changes are left for later tasks; record the count in the commit message.

---

### Task 2: Django 5.2 / scoped-rbac 0.11 compatibility; test settings

**Files:**
- Create: `tests/settings.py`
- Modify: `tests/conftest.py`, `manage.py`, `scrud_django/apps.py`, `scrud_django/__init__.py` (drop `default_app_config`), `scrud_django/mixins/resource_mixin.py:5`, `scrud_django/migrations/0001_initial.py`, `0004_auto_20210306_1600.py`, `0005_fix_resource_content_column_type.py`, `scrud_django/urls.py`, `scrud_django/validators.py:3`, `scrud_django/registration.py:303-326`, `tests/resource_types.py`, `tests/workflow_defaults.py:13`, `tests/urls.py`, `tests/fixtures.py` (lines 77–178), `tests/test_reversion.py:9`
- Delete: `scrud_django/settings.py`, `scrud_django/wsgi.py`, `scrud_django/asgi.py`

**Interfaces:**
- Produces: `tests.settings.SETTINGS: dict[str, Any]` — the single source of test/dev settings; both `tests/conftest.py::pytest_configure` and `manage.py` call `django.conf.settings.configure(**SETTINGS)`.
- Produces: `ScrudConfig.default_auto_field = "django.db.models.AutoField"`.

- [ ] **Step 1: Move the settings dict out of `conftest.py` into `tests/settings.py::SETTINGS`**

Changes while moving: drop `django_jsonfield_backport`, `USE_L10N`, `TEMPLATE_LOADERS`; `PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]`; add `USE_TZ = True`, `DEFAULT_AUTO_FIELD = "django.db.models.AutoField"`. Drop the `--no-pkgroot` option (copied from DRF, unused). `manage.py` configures from `SETTINGS` when `DJANGO_SETTINGS_MODULE` is unset.

- [ ] **Step 2: JSONField and migrations**

`django_jsonfield_backport.models.JSONField` → `django.db.models.JSONField` in `resource_mixin.py` and the three migrations (keep each migration's operations otherwise byte-identical).

- [ ] **Step 3: App config and package project files**

Remove `default_app_config`; add `default_auto_field` per Interfaces; delete `settings.py`/`wsgi.py`/`asgi.py`.

- [ ] **Step 4: scoped-rbac 0.11 API changes**

`from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT` in `validators.py` (and `tests/fixtures.py`). In `scrud_django/urls.py` add `path("semantic/", include("scoped_rbac.semantic_urls"))` next to the existing `include("scoped_rbac.urls")`.

- [ ] **Step 5: `importlib.resources.read_text(pkg, name)` → `importlib.resources.files(pkg).joinpath(name).read_text()`** in every file listed above.

- [ ] **Step 6: Fixture marks**

Remove `@pytest.mark.django_db` from fixtures; add the `db` fixture as a parameter to each such fixture instead.

- [ ] **Step 7: Verify**

Run: `uv run python manage.py check && uv run python manage.py makemigrations scrud_django --check --dry-run`
Expected: `System check identified no issues`, then `No changes detected`.

Run: `uv run pytest tests/test_workflow_policy.py tests/test_reversion.py tests/test_indexing_policy.py tests/test_custom_django_signals.py -q`
Expected: collection succeeds. `test_workflow_policy.py` may fail on `celpy` until Task 3; any other failure is a Task 2 bug.

- [ ] **Step 8: Commit** (`refactor: Django 5.2 and scoped-rbac 0.11 compatibility`)

---

### Task 3: Workflow CEL on `scoped_rbac.cel`

**Files:**
- Modify: `scrud_django/workflow.py:1-140`
- Test: `tests/test_workflow_policy.py`

**Interfaces:**
- Consumes: `scoped_rbac.cel.compile_expression(expr, environment)`, `new_environment(functions)`, `is_true(compiled, data) -> bool`, `InvalidExpression(ValueError)`.
- Produces: `WorkflowPolicy(policy: dict)` raises `django.core.exceptions.ImproperlyConfigured` (with `__cause__` the `InvalidExpression`) when any state condition fails to compile. `validate()` / `determine_state()` signatures unchanged.

- [ ] **Step 1: Write the failing test**

```python
def test_invalid_state_expression_is_a_configuration_error():
    policy = {"initial_states": {"draft": "status == "}, "states": {}, "transitions": {}}
    with pytest.raises(ImproperlyConfigured) as excinfo:
        WorkflowPolicy(policy)
    assert isinstance(excinfo.value.__cause__, InvalidExpression)
```

- [ ] **Step 2: Run** `uv run pytest tests/test_workflow_policy.py -q` — Expected: FAIL (`celpy` import error or `CELParseError`).

- [ ] **Step 3: Replace celpy**

Build one module-level environment with `new_environment({})`; `compile_expression(condition, environment)` per condition, wrapping `InvalidExpression` as `ImproperlyConfigured`; `determine_state` uses `is_true(condition, data)`. Remove `json_to_cel`, `BoolType`, `CELEvalError`.

- [ ] **Step 4: Run** `uv run pytest tests/test_workflow_policy.py -q` — Expected: all PASS.

- [ ] **Step 5: Commit** (`refactor: evaluate workflow policies with scoped_rbac.cel`)

---

### Task 4: Combined exception handler

**Files:**
- Modify: `scrud_django/exceptions.py:6-17`, `tests/settings.py` (REST_FRAMEWORK)
- Test: `tests/test_exceptions.py` (new)

**Interfaces:**
- Produces: `scrud_django.exceptions.scrud_exception_handler(exc, context) -> Response | None` (name kept). For `ScrudException`: scrud's body (`exc.response_data(context)` + `status`) and `application/problem+json` for JSON requests. Anything else: the result of `scoped_rbac.rest.exception_handler(exc, context)`.

- [ ] **Step 1: Write the failing tests** (use DRF's `APIRequestFactory` to build `context = {"request": ..., "view": ...}`)

```python
def test_scrud_errors_keep_their_body(api_context):
    exc = WorkflowInvalidStateError(policy="todo")
    response = scrud_exception_handler(exc, api_context)
    assert response.status_code == 400
    assert response.data["type"] == "http://api.openteams.com/http-problem/workflow-transition-invalid"
    assert response.data["message"] == "The request does not match a valid state"
    assert response["Content-Type"].startswith("application/problem+json")

def test_other_errors_use_scoped_rbac_problem_details(api_context):
    response = scrud_exception_handler(NotFound(), api_context)
    assert response.status_code == 404
    assert set(response.data) >= {"type", "title", "status"}
```

- [ ] **Step 2: Run** `uv run pytest tests/test_exceptions.py -q` — Expected: second test FAILS (DRF body has only `detail`).

- [ ] **Step 3: Implement** — delegate non-`ScrudException` to `scoped_rbac.rest.exception_handler`; for `ScrudException` keep today's logic on top of DRF's handler. Set `REST_FRAMEWORK["EXCEPTION_HANDLER"]` in `tests/settings.py` to this handler (unchanged path).

- [ ] **Step 4: Run** `uv run pytest tests/test_exceptions.py -q` — Expected: PASS.

- [ ] **Step 5: Commit** (`feat: delegate non-scrud errors to scoped-rbac problem details`)

---

### Task 5: Rewrite `tests/steps.py` for safetydance 0.5

**Files:**
- Modify: `tests/steps.py` (rewrite), `tests/conftest.py` (add `import tests.steps  # noqa: F401  registers steps`)

**Interfaces:**
- Consumes: `safetydance.context_data`, `initializer`, `step`, `step_namespace`; `safetydance_django.test.Http` (`Http.response`, `Http.api_client`); `safetydance_django.steps.json_values_match`.
- Produces:
  - `@context_data class Scrud` with fields `named_instances: dict = initializer(lambda ctx: {})`, `json_path_expressions: dict = initializer(lambda ctx: {})`, `json_path_match: list` (set by `json_path_matches`).
  - Bare `@step`s (same names and parameters as today): `an_instance_named`, `is_valid_resource`, `is_valid_resource_type`, `an_registration_data`, `check_registration_results`, `json_path_matches`, `ld_context_states_sub_class_of`.
  - `@step_namespace("http")` steps: `resource_json_is`, `response_json_matches`.
  - `__all__` exports `Scrud` plus the step names.

- [ ] **Step 1: Rewrite.** Every former `step_data` read/write becomes `Scrud.<field>`; every `http_response` becomes `Http.response`. `json_path_matches` must assign `Scrud.json_path_match` (today it assigns a local — the bug the spec lists).

- [ ] **Step 2: Verify registration**

Run: `uv run python -c "import django; from django.conf import settings; from tests.settings import SETTINGS; settings.configure(**SETTINGS); django.setup(); import tests.steps; from safetydance import Given; Given.json_path_matches; Given.http.resource_json_is; Given.http.get"`
Expected: no output, exit 0 (no `StepNotFound`, no `ValueError` from double registration).

- [ ] **Step 3: Commit** (`test: port custom steps to safetydance 0.5`)

---

### Task 6: Port the test modules

**Files:**
- Modify: `tests/test_rest.py`, `tests/test_rest_workflow.py`, `tests/test_authz.py`, `tests/test_models.py`, `tests/test_rdf.py`
- Create: `docs/follow-ups.md`

**Interfaces:**
- Consumes: Task 5's `Scrud` and steps; `from safetydance_django.test import *` (`Given/When/Then/And`, `test_context`, `Http`).

- [ ] **Step 1: Mechanical port** — `safetydance_test` imports → `from safetydance_django.test import *`; `@scripted_test` → `@test_context` directly below `@pytest.mark.*` (22 sites); bare `http_response` → `Http.response` (31 sites); `json_path_match` → `Scrud.json_path_match`. `Given/When/Then/And.http.*` calls are unchanged.

- [ ] **Step 2: Close the known gaps** — `tests/test_rest.py:37` and `:51` become `assert Http.response["Last-Modified"] == last_modified_for(...)`.

- [ ] **Step 3: Run the whole suite**

Run: `uv run pytest -q -p no:randomly`
Expected: everything collects. Write the list of failing test ids into the commit message; each failure must belong to one of: scrudful behavior (Task 7), scoped-rbac 0.11 behavior (403→404, IRI-less views, per-object list filtering), or a spec-listed latent bug (Task 8). Anything else is a porting bug — fix it in this task.

- [ ] **Step 4: Commit** (`test: port suite to safetydance 0.5 and safetydance-django 0.1`) with `docs/follow-ups.md` created (heading + empty list).

---

### Task 7: Adopt scrudful with `object_validators=True`

**Files:**
- Delete: `scrud_django/decorators.py`
- Modify: `scrud_django/views.py:10`, `scrud_django/mixins/view_mixins.py` (both `Meta` blocks, `get_instance`), `scrud_django/utils.py` (drop `get_string_or_evaluate` if now unused)
- Test: `tests/test_conditional.py` (new), `tests/test_rdf.py`, `tests/test_rest.py`, `tests/test_rest_workflow.py`

**Interfaces:**
- Consumes: `scrudful.rest_framework.scrudful_viewset`, `scrudful_api_view`; scoped-rbac `AccessControlledAPIView.get_object` (404 for unreadable objects).
- Produces: `ResourceViewMixin` and `GenericViewMixin` `Meta` with `object_validators = True`, `etag_func(obj) -> str` returning `obj.etag`, `last_modified_func(obj) -> datetime` returning `obj.modified_at`; link callbacks accept `**kwargs`. `ResourceViewMixin.get_queryset()` returns `Resource.objects.filter(resource_type=self.get_resource_type())`, `lookup_url_kwarg = "slug"`; `get_instance(request, slug)` returns `self.get_object()`. `GenericViewMixin` documents that subclasses provide DRF `get_queryset()`/lookup instead of `get_instance`.

- [ ] **Step 1: Write the failing tests** in `tests/test_conditional.py` (fixtures from `tests/fixtures.py`; `login_no_roles` can't read partner profiles)

```python
@pytest.mark.django_db
@test_context
def test_conditional_write_on_unreadable_resource_is_404(partner_profiles, login_no_roles):
    url = reverse(RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id})
    Given.http.force_authenticate(login_no_roles)
    When.http.put(url, data={}, format="json", HTTP_IF_MATCH='"anything"')
    Then.http.status_code_is(404)
    When.http.delete(url, HTTP_IF_UNMODIFIED_SINCE="Thu, 01 Jan 1970 00:00:00 GMT")
    Then.http.status_code_is(404)
    When.http.get(url, HTTP_IF_NONE_MATCH="*")
    Then.http.status_code_is(404)

@pytest.mark.django_db
@test_context
def test_patch_without_precondition_is_428(partner_profiles, admin_login): ...
    # PATCH detail url with a valid body and no If-* header → status 428

@pytest.mark.django_db
@test_context
def test_if_modified_since_current_is_304(partner_profiles, admin_login): ...
    # GET detail → read Last-Modified; GET again with HTTP_IF_MODIFIED_SINCE=<that value> → 304
```

- [ ] **Step 2: Run** `uv run pytest tests/test_conditional.py -q` — Expected: FAIL (first: 412/428; third: `TypeError`).

- [ ] **Step 3: Switch to scrudful** per Interfaces; delete `decorators.py`.

- [ ] **Step 4: Update behavior assertions** — `tests/test_rdf.py:35,56` JSONPaths become `get.responses['200'].content['application/json']['x-rdf-context'].href` and `post.requestBody.content['application/json']['x-rdf-context'].href` (confirm against a real OPTIONS response); missing-precondition assertions 400 → 428; any test that compares an ETag to `instance.etag + version` compares to the plain quoted etag; Link `rel="describedby"` / `application/schema+json`.

- [ ] **Step 5: Run** `uv run pytest -q -p no:randomly` — Expected: `tests/test_conditional.py` passes; no failure remains in the scrudful category of Task 6's list.

- [ ] **Step 6: Commit** (`feat: use scrudful for conditional requests and links`)

---

### Task 8: Green suite — latent bugs and scoped-rbac 0.11 behavior

**Files:**
- Modify: `scrud_django/validators.py:99-106`, `scrud_django/mixins/serializer_mixins.py:66`, `scrud_django/views.py:53-54`, `tests/test_authz.py`, `docs/follow-ups.md`
- Test: whichever existing test exercises each bug; add one where none does

- [ ] **Step 1:** For each failure left from Task 6, in order: write or point to a test that fails for that reason, fix it, rerun. Spec-listed bugs: `WorkflowValidator` (`self.workflow_policies` undefined, `policy.version` nonexistent), `serializer_mixins.py:66` calling a `cached_property` value, `content__contains` on SQLite in `ResourceViewSetGenericFilter.list` (filter in Python on SQLite, or `xfail` with a follow-up — builder's choice). scoped-rbac behavior: assertions in `test_authz.py` that expected 403 on an object the user can't read become 404, with a comment citing scoped-rbac 0.11.

- [ ] **Step 2:** Anything not fixed → `xfail(strict=True)` with a `docs/follow-ups.md` anchor.

- [ ] **Step 3: Verify on all three Pythons**

Run: `for v in 3.11 3.12 3.13; do uv run --python $v pytest -q || break; done`
Expected: each run ends with `passed` and only listed `xfailed`; zero `failed`/`error`.

- [ ] **Step 4: Commit** (`fix: issues surfaced by the restored test suite`)

---

### Task 9: CI, docs, README, Makefile, demo

**Files:**
- Create: `.gitlab-ci.yml`
- Modify: `Makefile`, `README.md`, `docs/conf.py`, `docs/installation.md`, `docs/contributing.md`, `docs/contributors.md`, `demo/demo/settings.py`, `demo/demo/urls.py`

- [ ] **Step 1: `.gitlab-ci.yml`** — image `ghcr.io/astral-sh/uv:python3.13-bookworm-slim`; `lint` job: `uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run python manage.py makemigrations scrud_django --check --dry-run`; `test` job with `parallel: matrix: PYTHON: ["3.11","3.12","3.13"]` running `uv run --python $PYTHON pytest --cov=scrud_django`.

- [ ] **Step 2: Run every CI command locally** (lint commands once, test command for each Python). Expected: all exit 0.

- [ ] **Step 3: Makefile** — keep only thin `uv run …` targets (`test`, `lint`, `format`, `docs`, `migrations-check`); none may call conda, pip or docker-compose.

- [ ] **Step 4: Docs** — `docs/conf.py`: theme `sphinx_rtd_theme`, drop `sphinxcontrib.bibtex` and `solar_theme`, add `superpowers` to `exclude_patterns`, project/copyright to the owner. Rewrite install/contributing for uv and the GitLab repo. Run `uv run --group docs sphinx-build -W -b html docs docs/_build` — Expected: build succeeds with no warnings.

- [ ] **Step 5: README** — uv install, `INSTALLED_APPS` (no `django_jsonfield_backport`), `REST_FRAMEWORK["EXCEPTION_HANDLER"] = "scrud_django.exceptions.scrud_exception_handler"`, the semantic URL include, the `search` extra, dev commands.

- [ ] **Step 6: Demo** — remove `django_jsonfield_backport`, `USE_L10N`; API root via `scoped_rbac.urls.OpenRouter`; include `scoped_rbac.semantic_urls`; `read_text` per Task 2. Run `cd demo && uv run python manage.py check && uv run python manage.py migrate && uv run python manage.py runserver --noreload` briefly and GET `/` → 200.

- [ ] **Step 7: Commit** (`ci: GitLab pipeline; docs and demo for the uv/Django 5.2 stack`)

---

### Task 10: Claude Code setup

**Files:**
- Create: `CLAUDE.md`, `.claude/settings.json`

- [ ] **Step 1: `CLAUDE.md`** — contents per the spec's "Claude Code setup" section: one-paragraph purpose; upstream packages (PyPI, never edited from here); exact commands (`uv sync`, `uv run pytest`, `uv run pytest tests/test_rest.py::test_resource_get_list`, `uv run ruff check --fix . && uv run ruff format .`, `uv run python manage.py makemigrations scrud_django --check --dry-run`, demo); test conventions (`@pytest.mark.django_db` → `@test_context` order, steps live in `tests/steps.py` and are registered by `conftest.py`, never define steps in `conftest.py`, `@context_data` fields must not shadow fixture names); gotchas (settings come from `tests/settings.py::SETTINGS`, SQLite only, migrations must keep `makemigrations --check` clean, follow-ups go in `docs/follow-ups.md`). Under ~120 lines.

- [ ] **Step 2: `.claude/settings.json`** — `permissions.allow`: `Bash(uv sync:*)`, `Bash(uv run pytest:*)`, `Bash(uv run ruff:*)`, `Bash(uv run python manage.py check:*)`, `Bash(uv run python manage.py makemigrations:*)`, `Bash(git status:*)`, `Bash(git diff:*)`, `Bash(git log:*)`. Validate: `python3 -m json.tool .claude/settings.json`.

- [ ] **Step 3: Verify the instructions** — run each command written in `CLAUDE.md` once; each exits 0.

- [ ] **Step 4: Commit** (`chore: add CLAUDE.md and Claude Code project settings`)
