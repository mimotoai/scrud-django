# scrud-django modernization — design

Date: 2026-10-28
Status: reviewed; awaiting owner approval

## Intent

scrud-django ("Semantic CRUD" for Django REST Framework) was last worked on in
2022, against Django 2.2 and Python 3.7/3.8, with poetry, conda, and an
openteams-era toolchain. The owner wants to **get it running and tested on
current Python and current Django, then resume developing and improving it**.
Publishing a release is not a goal of this work (but nothing here should make a
later release harder).

The owner has already modernized and released the packages scrud-django builds
on, so this project consumes them from PyPI rather than updating them:

| Package | Released | Role for scrud-django |
|---|---|---|
| django-scoped-rbac | 0.11.1 | runtime: RBAC, policies, `scoped_rbac.cel` |
| scrudful | 0.8.0 | runtime (already a transitive dep via scoped-rbac): conditional requests, Link headers |
| safetydance | 0.5.0 | tests: context data, `@step`, `Given/When/Then/And` |
| safetydance-django | 0.1.0 | tests: `Http` context data and `http.*` steps, `test_context` |

The project should also be set up to work well with Claude Code.

## Goals

1. `uv sync && uv run pytest` passes on a fresh clone, on Python 3.11–3.13,
   against Django 5.2 LTS and the released packages above.
2. Packaging is standard PEP 621 `pyproject.toml` managed by uv, with a
   committed `uv.lock`. Poetry, conda, rever, npm, setuptools leftovers are gone.
3. Lint/format is ruff (replacing black, isort, seed-isort-config, flake8), run
   through pre-commit and CI.
4. A `.gitlab-ci.yml` exists for the repo's remote
   (`gitlab.com/dcharbon/scrud-django`), and the commands it runs pass locally
   on Python 3.11, 3.12 and 3.13 (`uv run --python 3.1x …`). *(Owner's
   decision:)* nothing is pushed as part of this work; the owner pushes and
   opens the merge request.
5. The test suite uses the new safetydance API, and checks it was only
   pretending to make before (see "Known test gaps") are real assertions.
6. A `CLAUDE.md` and project `.claude/settings.json` make Claude Code effective
   here from the first prompt.

## Non-goals

- Publishing a release to PyPI or conda-forge.
- New features or API redesign beyond what dependency upgrades force.
- Updating any of the four upstream packages (they are released; if a defect is
  found in one, record it and work around it here, don't edit that repo).
- Rebranding the `api.openteams.com` IRIs baked into runtime code, static
  JSON-LD/schema files, and fixtures. They are part of the API's semantics;
  changing them is a separate, breaking decision.
- Django 6.x support. django-haystack declares support only through 5.2;
  revisit when it moves.
- Postgres. The tests use in-memory SQLite and nothing needs Postgres.

## Anti-goals (would count as failure even if tests pass)

- Tests made to pass by deleting or weakening assertions. When a test fails
  because behavior changed for a sound reason (e.g. scrudful's stricter RFC 9110
  preconditions), the test is updated to assert the new behavior, explicitly.
- Silent loss of a test: every currently-existing test function ends up either
  ported and passing, or explicitly marked `xfail` with a reason pointing at a
  recorded follow-up. *(My call:)* follow-ups — xfails, deferred latent bugs,
  upstream defects — are recorded in-repo in `docs/follow-ups.md`, one entry
  per item with an anchor the `xfail` reason cites. No issue tracker access is
  needed.
- Vendoring or copying code from the upstream packages instead of depending on
  them.

## Constraints and decided choices

### Versions

- `requires-python = ">=3.11"` — django-scoped-rbac's floor (cel-expr-python
  wheels exist for 3.11–3.14). CI matrix 3.11, 3.12, 3.13. (scoped-rbac excludes
  3.14 from CI because of a known 3.14 issue; follow its lead.)
- `django>=5.2,<6.0`.
- Runtime deps, from PyPI: `django-scoped-rbac[drf]>=0.11.1,<0.12`,
  `scrudful[drf]>=0.8,<0.9` (same pin as scoped-rbac), `djangorestframework>=3.15`,
  `django-filter`, `django-reversion`, `jsonpath-ng`, `fastjsonschema`.
- Optional extra `search`: `django-haystack`, `whoosh-reloaded` (haystack is
  only used by `search_indexes.py`; nothing queries search at runtime).
- Dev dependency group (uv default group): `safetydance>=0.5,<0.6`,
  `safetydance-django>=0.1,<0.2`, `pytest`, `pytest-django`, `pytest-cov`,
  `factory-boy`, `rdflib`, `ruff`, `pre-commit`, `django-extensions`, plus the
  `search` extra so the full suite runs. A separate `docs` group for Sphinx.
- Dropped: `django-jsonfield-backport`, `python-decouple`, `python-dotenv`
  (moves with dev settings, see below; drop if unused there), `django-cors-headers`
  (demo only — keep in demo if demo keeps it), black, flake8, isort,
  seed-isort-config, mypy (its hook pointed at a nonexistent `toki/`),
  docker-compose, `safetydance-test`, `type-extensions`, `cel-python`/`celpy`,
  solar-theme, sphinxcontrib-bibtex, sphinxcontrib-django, sphinx-autobuild.
- Build backend: `uv_build` (matches scoped-rbac, safetydance, scrudful).
- Version: single source in `pyproject.toml`; `scrud_django.__version__` reads it
  via `importlib.metadata` (today `__init__.py` says 0.1.9 while pyproject says
  0.12.0).

### Django 2.2 → 5.2 code changes (known list)

- `django_jsonfield_backport.models.JSONField` → `django.db.models.JSONField` in
  `mixins/resource_mixin.py` and **edit migrations 0001, 0004, 0005 in place**
  (column type is unchanged; `makemigrations --check` must report no changes).
- Remove `default_app_config` from `scrud_django/__init__.py`.
- `ScrudConfig.default_auto_field = "django.db.models.AutoField"` (0001 uses
  AutoField; avoids W042 now and a spurious id migration later).
- Remove `USE_L10N`, `TEMPLATE_LOADERS`; set `USE_TZ = True` explicitly in test
  settings.
- Test password hashers: remove `SHA1PasswordHasher`, `CryptPasswordHasher`
  (use `MD5PasswordHasher` for speed).
- `@pytest.mark.django_db` on fixtures (tests/fixtures.py, test_reversion.py) is
  a collection error under pytest 9: request the `db` fixture instead.
- `importlib.resources.read_text` → `files(...).joinpath(...).read_text()`.

### django-scoped-rbac 0.11 adoption

- `scoped_rbac.permissions.DEFAULT_CONTEXT` was removed → import from
  `scoped_rbac.rbac_contexts` (`scrud_django/validators.py:3`,
  `tests/fixtures.py:13`).
- `scrud_django/workflow.py` stops importing `celpy` (it only ever got it
  transitively from scoped-rbac's old vendored copy). Use `scoped_rbac.cel`:
  compile each state condition once with `compile_expression(expr,
  new_environment({}))`, evaluate with `is_true(compiled, data)`. Errors already
  meant "state doesn't match", which is exactly `is_true`'s semantics, so the
  `CELEvalError` handling and `json_to_cel`/`BoolType` go away. Catch
  `InvalidExpression` where policies are compiled and surface it as a
  configuration error.
- Include `scoped_rbac.semantic_urls` once in the URLconfs that include
  `scoped_rbac.urls` (system check E002 otherwise).
- Exception handling *(my call)*: scrud's own error bodies are part of its API
  and must survive — `JsonValidationError` (its `api.openteams.com/http-problem`
  `type` IRI plus `name`, `value`, `rule`, `rule_definition`) and the workflow
  errors (`policy`, `prior_state`, `proposed_state`, `message`);
  `tests/test_rest.py::test_resource_post_bad_request` keeps asserting them.
  scrud-django ships a handler in the package (e.g.
  `scrud_django.exceptions.exception_handler`) that renders scrud's exceptions
  itself and delegates everything else to `scoped_rbac.rest.exception_handler`.
  README/CLAUDE.md tell consuming projects to configure it; tests and demo use
  it.
- Behavior changes the tests will see and should assert as the new truth:
  unreadable objects are 404 (not 403); views lacking a resource-type IRI are
  denied; list filtering of `Resource` happens per object and is capped by
  `CONDITIONAL_LIST_MAX_ROWS`.

### scrudful adoption (decided by owner: adopt now, with `object_validators=True`)

`scrud_django/decorators.py` is the code scrudful was extracted from in 2021;
scrudful 0.8 is its RFC 9110 rewrite and is already in the dependency tree.
Delete `decorators.py` and import `scrudful_viewset`/`scrudful_api_view` from
`scrudful.rest_framework`; the `Meta` blocks in `mixins/view_mixins.py` keep
working. This fixes a real bug (If-Modified-Since / If-Unmodified-Since raise
`TypeError` on any Django ≥ 3) and an unnoticed one (schema rel/type passed as
context rel/type). Observable changes the tests must be updated to assert:
missing preconditions → 428 (was 400; either If-Match or If-Unmodified-Since now
suffices); PATCH now requires a precondition; ETags no longer carry the
`__version__` suffix; Link `rel="describedby"` with `application/schema+json`;
412 has a JSON body; OPTIONS body is scrudful's Semantic HTTP format
(`tests/test_rdf.py:35,56` read the old shape).

Use scrudful's `object_validators=True`: the `Meta.etag_func` /
`last_modified_func` callbacks in `mixins/view_mixins.py` are rewritten to take
the already-authorized object instead of doing their own unscoped
`get_object_or_404`, so a 412/428 can no longer reveal that a resource the
caller can't read exists. Add a test proving that a caller without read access
gets 404 (not 412/428) on a conditional PUT/DELETE.

### Test suite port (safetydance 0.3 → 0.5 / safetydance-django 0.1)

Mechanical, per safetydance-django's CHANGELOG migration table:

- `from safetydance_test import ...` / `scripted_test` → `from
  safetydance_django.test import *`; `@scripted_test` → `@test_context`, placed
  directly under `@pytest.mark.*` (22 sites).
- `Given/When/Then/And.http.*` calls stay as they are (102 sites).
- Bare `http_response[...]`/`http_response.get(...)` → `Http.response[...]`
  (31 sites).
- `tests/steps.py` is rewritten: `step_data` stores become fields on a
  `@context_data` class (with `initializer(lambda ctx: {})` where needed);
  `@step_extension` → `@step`; `step_extension(f=..., target_type=Http)` →
  `@step_namespace("http")`. `tests/conftest.py` imports `tests.steps` once to
  register steps; steps are never defined in `conftest.py` (double registration
  raises `ValueError`).
- `@context_data` field names must not collide with pytest fixture names the
  tests don't mean to read (`test_context` stores fixtures by name).

### Known test gaps to close during the port

- `tests/test_rest.py:37,51` compare `Last-Modified` without `assert`.
- `json_path_matches` (tests/steps.py ~105) assigns a local, so
  `tests/test_rdf.py:16,39,60` read a value that is never set.

### Known latent bugs that will surface once tests run

Fix them (each with a test that fails first) if they block the suite;
otherwise record them as follow-ups:

- `validators.py:99-106` `WorkflowValidator` uses undefined
  `self.workflow_policies` and nonexistent `policy.version`.
- `serializer_mixins.py:66` calls the value of a `cached_property`.
- `views.py:53-54` `content__contains` raises `NotSupportedError` on SQLite.
- (`decorators.py:96` If-Modified-Since `TypeError` — removed by the scrudful
  adoption.)

### Project layout and settings

- The package stops shipping a Django project: delete
  `scrud_django/settings.py`, `wsgi.py`, `asgi.py` (hard-coded secret key,
  `ALLOWED_HOSTS=["*"]`). `scrud_django/urls.py` stays (it's the includable app
  URLconf).
- Tests keep the `settings.configure()` pattern in `tests/conftest.py` (same as
  scoped-rbac), cleaned up; pytest config moves into `pyproject.toml`
  (`[tool.pytest.ini_options]`), `pytest.ini` is deleted.
- Root `manage.py` is pointed at a small dev/test settings module (needed for
  `makemigrations --check`), or replaced by a documented equivalent — builder's
  choice.
- `demo/` is kept and updated to run against the new stack (settings cleanup,
  `scoped_rbac.urls.OpenRouter` for its API root, semantic URLs). It is a smoke
  test of the integration story, not covered by CI.

### Tooling: delete / replace

Delete: `poetry.lock` (already deleted in the working tree), `environment.yml`,
`docker/` (conda envs + Postgres 10 compose), `.github/` (GitHub Actions for an
openteams GitHub repo, Python 3.7, a gh-pages deploy with someone else's
token), `package.json`, `package-lock.json`, `rever.xsh`, `news/`,
`MANIFEST.in`, `.flake8`, `pytest.ini`, the `[settings] known_third_party` and
`[tool.poetry*]` blocks.

Replace/update:
- `Makefile` → either thin wrappers over `uv run …` or removed in favor of
  documenting the uv commands in README/CLAUDE.md — builder's choice, but no
  target may call conda, pip, or `docker-compose`.
- `.pre-commit-config.yaml` → ruff (lint + format) and basic hygiene hooks.
- `.gitlab-ci.yml` (new) → `uv sync --locked`, ruff check/format --check,
  pytest with coverage, Python 3.11–3.13; `makemigrations --check`.
- `.gitignore` → drop pipenv/rever/Scrapy/Flask cruft, add `.ruff_cache/`.
- `docs/` → keep Sphinx + myst-parser, swap solar-theme for sphinx-rtd-theme,
  drop bibtex, fix stale openteams/conda/`setup.py install` instructions in
  `installation.md`, `contributing.md`, `contributors.md`; add
  `superpowers/` to `exclude_patterns`. Docs build in CI is optional;
  hosting is out of scope.
- `README.md` → uv-based install and dev instructions.

### Claude Code setup

- `CLAUDE.md` at the repo root: what scrud-django is (one paragraph), the
  upstream packages and that they're consumed from PyPI, exact commands (sync,
  test, single test, lint/format, makemigrations check, demo), test conventions
  (safetydance 0.5 idioms, decorator order, steps live in `tests/steps.py`),
  and gotchas found during this work (SQLite-only tests, settings.configure in
  conftest, double step registration, migration edits must keep
  `makemigrations --check` clean).
- `.claude/settings.json` (committed): allow `uv run pytest*`, `uv run ruff*`,
  `uv sync*`, `uv run python manage.py makemigrations --check*`, read-only git
  commands. No hooks are required; a ruff-format-on-edit hook is optional.

## Order of work

Work happens on a branch off `develop`. The guiding principle: get the test
suite **collecting** as early as possible, then use it to drive the rest.

1. Packaging + dependencies + tooling cleanup (uv, pyproject, ruff, deletes).
2. Django 5.2 / scoped-rbac 0.11 compatibility edits needed to import and
   migrate (JSONField + migrations, app config, `DEFAULT_CONTEXT`, celpy →
   `scoped_rbac.cel`, semantic URLs, conftest settings).
3. Port the test suite to safetydance 0.5 and close the known test gaps.
4. Make the suite green: fix the latent bugs that block it; adopt scrudful
   with `object_validators=True` and update affected assertions.
5. CI, docs, README, demo.
6. CLAUDE.md and `.claude/settings.json`, written last so they describe what
   actually exists.

## Left to the builder

- Exact task breakdown and commit granularity within each step.
- Whether `Makefile` survives as thin `uv run` wrappers.
- How root `manage.py` gets its settings.
- Ruff rule selection beyond a sensible default (start from scoped-rbac's
  config, including its per-file ignores for star-imported test steps).
- Whether to xfail or fix each latent bug that doesn't block the suite.
