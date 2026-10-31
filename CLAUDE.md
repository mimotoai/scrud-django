# scrud-django

scrud-django is a Django/DRF library for building SCRUD (Search, Create, Read,
Update, Delete) REST APIs whose resources and resource types are stored as data
and described with JSON Schema and JSON-LD. It layers on django-scoped-rbac for
authorization. `README.md` covers usage; this file covers working in the repo.

## Upstream packages

django-scoped-rbac, scrudful, safetydance and safetydance-django are separate
projects installed from PyPI. Never edit them from here; if one has a bug, work
around it in this repo and record it in `docs/follow-ups.md`. Their docs and
source are in `.venv/lib/python3.13/site-packages/<pkg>/` (for example
`scoped_rbac`, `scrudful`, `safetydance`, `safetydance_django`).

- scrudful handles conditional requests (ETag, If-Match, 304/412/428).
- Object-level RBAC comes from scoped-rbac; detail routes authorize through
  `get_object()`. `ResourceViewSet` and `ResourceViewSetGenericFilter` set
  `permission_classes = [IsAuthorized]` themselves so they fail closed even
  under DRF's `AllowAny` default; keep it that way (`tests/test_authz.py`).
- The combined exception handler is `scrud_django.exceptions.scrud_exception_handler`.

## Commands

Python 3.13 is pinned in `.python-version`; CI covers 3.11-3.13, not 3.14 (the
suite passes on it locally). The default uv or system Python may be 3.14, so
leave the pin. Always go through `uv run`, which syncs the project environment
first (CI uses `--locked`; these commands do not, so the allow rules in
`.claude/settings.json` match them exactly).

Run these directly. Each is pre-approved in `.claude/settings.json` by an
exact-match allow rule (one rule per command line below, no wildcards) and runs
without a prompt:

```bash
uv run pytest
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python manage.py check
uv run python manage.py makemigrations scrud_django --check --dry-run
uv run --group docs sphinx-build -W -b html docs docs/_build
```

The docs build treats warnings as errors; its first run installs the `docs`
dependency group, which needs network access. The read-only `git status`,
`git diff`, `git diff --stat`, `git diff --cached` and `git log --oneline -20`
are pre-approved the same way.

Any other pytest invocation prompts, because the rules are exact. That includes
a single test, which is the usual way to run one while working:

```bash
uv run pytest tests/test_rest.py::test_resource_get_list
uv run pytest -q --reverse
```

`--reverse` (from `tests/conftest.py`) runs the suite in reverse order, as CI
also does.

These write files or touch the environment, so they prompt. Run them on the
files you touched only; `ruff format .` and `ruff check --fix .` over the whole
repo rewrite files you did not mean to edit, and `uv sync` can rewrite the
environment:

```bash
uv run ruff check --fix <files>
uv run ruff format <files>
uv sync
```

The `Makefile` (`make test`, `make lint`, `make format`, `make docs`,
`make migrations-check`) is for humans; Claude should use the raw commands.

The suite on PostgreSQL, MySQL or MariaDB (these prompt too; start a
throwaway container, named for your task, on a free port, and remove it
after). The MySQL driver, mysqlclient, is not a dev dependency: add it with
`--with`, never `uv add`:

```bash
SCRUD_TEST_PG_HOST=localhost SCRUD_TEST_PG_PORT=<port> uv run pytest -q
SCRUD_TEST_MYSQL_HOST=127.0.0.1 SCRUD_TEST_MYSQL_PORT=<port> \
  SCRUD_TEST_MYSQL_PASSWORD=<pw> uv run --with mysqlclient==2.3.0 pytest -q
```

On macOS, mysqlclient builds against Homebrew's MySQL client, which cannot
log in to MariaDB (it lacks the `mysql_native_password` plugin). Run the
MariaDB suite in CI's image instead
(`ghcr.io/astral-sh/uv:0.9-python3.13-bookworm-slim` plus `build-essential
default-libmysqlclient-dev pkg-config`, on a docker network with the server),
as the `test-mysql` job does.

Demo app (API root at http://127.0.0.1:8000/api/; resource routes need a
login, at `/api-auth/login/` or with Basic auth):

```bash
cd demo
uv run python manage.py migrate
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

CI is `.gitlab-ci.yml`: lint, migrations check, docs build, tests with
coverage on 3.11-3.13 (then again with `--reverse`), the suite on PostgreSQL
14 via `SCRUD_TEST_PG_*` (`test-postgres`) and on MySQL 8.4 and MariaDB 11.4
via `SCRUD_TEST_MYSQL_*` (`test-mysql`), a package build/README/installed-
wheel check, and a version check on merge requests. Run the lint and test
commands above before committing. Publishing to PyPI runs in CI when a merge
to `develop` carries a version not yet on PyPI (`scripts/release.py`), and
fails for one lower than PyPI's latest; never run `uv publish` locally or
create release tags by hand.

## Test conventions

Tests use safetydance 0.5 and safetydance-django 0.1 (`Given`/`When`/`Then`).
See `tests/test_conditional.py` for the idiom.

- Decorator order is `@pytest.mark.django_db` first, then `@test_context`
  directly above the function (the order every test module uses).
- Custom steps live in `tests/steps.py` only.
  `tests/conftest.py::pytest_configure` imports that module after
  `django.setup()` so the steps register before collection. Test modules that
  use them also `from .steps import *`; the module is imported once, so steps
  are not registered twice. Never define steps in `conftest.py` (importing it
  first would register them before Django models are usable).
- `@context_data` fields become test-context attributes. A field name must not
  shadow a pytest fixture name, or it collides with the fixture.
- Test modules star-import safetydance and steps; ruff per-file ignores for
  F403/F405 in `pyproject.toml` exist for that. Keep the idiom.
- Tests must pass in any order and alone. Connect signal receivers per test
  (`setUp` with `addCleanup`, or a fixture that disconnects), never in
  `__init__` or at import: pytest builds `TestCase` instances at collection.
  Importing the URLconf (`reverse()`, `checks.run_checks()`) registers
  resource types in the database, so such a test needs `django_db`.
- Never weaken an assertion to make a test pass. When behaviour changes, add
  explicit new assertions for the new behaviour (status code, headers, body) and
  say why in the commit message.
- A real bug you are not fixing now goes in `docs/follow-ups.md` as a section
  with an anchor (`<a id="short-slug"></a>` then `## short-slug: title`), and the
  test that would expose it is described there.

## Gotchas

- Settings come from one place: `tests/settings.py::SETTINGS`. `conftest.py` and
  the root `manage.py` both call `settings.configure(**SETTINGS)`; there is no
  settings module on disk and no `DJANGO_SETTINGS_MODULE`. Add settings there.
- Request bodies are JSON-only (`parser_classes = [RbacJSONParser]` in
  `scrud_django/views.py`): form or multipart bodies get 415.
- Tests run on in-memory SQLite unless `SCRUD_TEST_PG_HOST` or
  `SCRUD_TEST_MYSQL_HOST` is set (CI's `test-postgres` and `test-mysql`; see
  `tests/settings.py`). Code the tests exercise must work on all four
  databases. MySQL and MariaDB differ in ways tests trip on: their default
  collations ignore case, accents and trailing spaces (contexts are stored
  as bytes, `scrud_django.models.fields.ContextField`); a TRUNCATE (`flush`
  with its default `reset_sequences`) commits the test's transaction;
  `bulk_create()` returns no keys on MySQL; SQL text quotes names with
  backticks (build expected SQL with `connection.ops.quote_name`); and
  MariaDB's JSON column refuses documents nested 32 levels or more.
- The root `manage.py` reads the same settings: with `SCRUD_TEST_PG_HOST`
  (or `SCRUD_TEST_MYSQL_HOST`) exported it uses that server's real database
  (`SCRUD_TEST_PG_NAME` or `SCRUD_TEST_MYSQL_NAME`, default `scrud`, not
  pytest's `test_` copy), so a `migrate` or a write there persists. Unset it
  (or point it at a throwaway server) before running `manage.py`.
- Migrations must keep the migrations check above clean. If a model change needs a
  migration, generate it with `uv run python manage.py makemigrations scrud_django`
  and commit it with the model change.
- The `api.openteams.com` IRIs (JSON-LD contexts, schema and type identifiers)
  are part of the API's semantics and stored data. Do not rebrand or "fix" them.
- Lists filter by `ResourceContext` rows, which `Resource.save()` (raw
  saves included) keeps in step; `QuerySet.update()`, `bulk_create()` and
  `bulk_update()` do not. `manage.py scrud_sync_contexts [--check]` compares
  and re-syncs them, and the `scrud_django.W004` database check reports
  resources with no row. Tests that write resources should go through
  `save()` or `ResourceRegistration`.
- The `demo/` project has its own settings and creates `demo/db.sqlite3`, which
  is gitignored; do not commit it.
- `ruff format` uses `quote-style = "preserve"`; do not mass-convert quotes.
- Known issues and deferred bugs are listed in `docs/follow-ups.md`; read it
  before assuming odd behaviour is new.
