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

Python 3.13 is pinned in `.python-version`; CI also runs 3.11 and 3.12. Always go
through `uv run` so the project environment is used. The default uv or system
Python may be 3.14, which is unsupported, so leave `.python-version` at 3.13.

Run these directly. Each of the first block is pre-approved in
`.claude/settings.json` (an exact-match allow rule, or `uv run pytest:*`) and
runs without a prompt:

```bash
uv run pytest -q
uv run pytest tests/test_rest.py::test_resource_get_list   # one test
uv run ruff check .
uv run ruff format --check .
uv run python manage.py makemigrations scrud_django --check --dry-run
uv run --group docs sphinx-build -W -b html docs docs/_build   # warnings are errors
```

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

Demo app (API root at http://127.0.0.1:8000/api/):

```bash
cd demo
uv run python manage.py migrate
uv run python manage.py runserver
```

CI is `.gitlab-ci.yml` (lint, migrations check, docs build, tests with coverage
on 3.11-3.13). Run the lint and test commands above before committing.

## Test conventions

Tests use safetydance 0.5 and safetydance-django 0.1 (`Given`/`When`/`Then`).
See `tests/test_conditional.py` for the idiom.

- Decorator order is `@pytest.mark.django_db` first, then `@test_context`
  directly above the function (the order every test module uses).
- Custom steps live in `tests/steps.py` only. `tests/conftest.py::pytest_configure`
  imports that module after `django.setup()` so the steps register before
  collection. Test modules also `from .steps import *`; the module is imported
  once, so steps are not registered twice. Never define steps in `conftest.py`
  (importing it first would register them before Django models are usable).
- `@context_data` fields become test-context attributes. A field name must not
  shadow a pytest fixture name, or it collides with the fixture.
- Test modules star-import safetydance and steps; ruff per-file ignores for
  F403/F405 in `pyproject.toml` exist for that. Keep the idiom.
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
- Tests run on in-memory SQLite only. Code the tests exercise must work on SQLite.
- Migrations must keep the migrations check above clean. If a model change needs a
  migration, generate it with `uv run python manage.py makemigrations scrud_django`
  and commit it with the model change.
- The `api.openteams.com` IRIs (JSON-LD contexts, schema and type identifiers)
  are part of the API's semantics and stored data. Do not rebrand or "fix" them.
- The `demo/` project has its own settings and creates `demo/db.sqlite3`, which
  is gitignored; do not commit it.
- `ruff format` uses `quote-style = "preserve"`; do not mass-convert quotes.
- Known issues and deferred bugs are listed in `docs/follow-ups.md`; read it
  before assuming odd behaviour is new.
