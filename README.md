# scrud-django
A Django application for SCRUD REST services. Semantic REST API generation.

Requires Python 3.11+ and Django 5.2.

## Installation

```bash
uv add scrud-django
# or: pip install scrud-django
```

For haystack search support, install the `search` extra:

```bash
uv add "scrud-django[search]"
```

## Configuration

### Required

Add `django.contrib.auth`, `django.contrib.contenttypes`, `rest_framework`,
`reversion`, `scoped_rbac` and `scrud_django` to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
  ...
  'django.contrib.auth',
  'django.contrib.contenttypes',
  'rest_framework',
  'reversion',
  'scoped_rbac',
  'scrud_django',
  ...
]
```

Include the scrud-django URLs. They include the scoped-rbac semantic URLs
(`semantic/`) that serve each resource type's schema and JSON-LD context:

```python
urlpatterns = [
    path("api/", include("scrud_django.urls")),
]
```

If you mount URLs yourself, include `scoped_rbac.semantic_urls`, for example
`path("semantic/", include("scoped_rbac.semantic_urls"))`.

Configure Django REST Framework to authorize with scoped-rbac and to answer
errors as [RFC 7807](https://tools.ietf.org/html/rfc7807) problem details while
keeping scrud-django's own validation and workflow error bodies (the scrud
views' other errors, precondition failures included, are problem details while
scoped-rbac's `SCOPED_RBAC["UNIFORM_ERRORS"]` is on, its default, and DRF's
native `{"detail": ...}` bodies when it is off):

```python
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["scoped_rbac.permissions.IsAuthorized"],
    "EXCEPTION_HANDLER": "scrud_django.exceptions.scrud_exception_handler",
    ...
}
```

Request bodies must be JSON: a form or multipart body gets 415, and a POST or
PUT with an empty body, or without a `Content-Length` (chunked), 400. JSON
Schema `default` values are not applied: a resource is validated against its
type's schema, stored and checked against its workflow policy exactly as it
was sent, so send every value you want stored.

If you use `DATABASE_ROUTERS`, route the writes of `scrud_django`'s models
(`Resource`, `ResourceType`, `ResourceContext`, `ResourceRemoval`) to one
database alias:
scrud-django writes a resource, its context rows and its type's row in one
transaction there, and the `scrud_django.E004` system check reports a split.
Routing these models by instance hint is not supported.

Run the database at READ COMMITTED, the supported transaction isolation level
(PostgreSQL's default; Django's MySQL backend uses it unless
`OPTIONS["isolation_level"]` says otherwise). `manage.py check --database
<alias>`, with the alias your router gives for `Resource` writes (and
`migrate` of it), warns about another (`scrud_django.W002`). A file SQLite
database that takes concurrent writes needs
`"OPTIONS": {"transaction_mode": "IMMEDIATE"}`: otherwise a second concurrent
writer fails with "database is locked" instead of waiting.

DRF's own default permission is `AllowAny`. The scrud-django viewsets set
`IsAuthorized` themselves, but any other view in your project (and any view you
build on scoped-rbac) relies on `DEFAULT_PERMISSION_CLASSES` to fail closed.

Resource type metadata is public by design: the service list (`services/`),
each type's collection schema (`collections-json-schema/<slug>/`) and
collection JSON-LD context (`collections-json-ld/<slug>/`) answer anyone,
anonymous users included. Clients need the schemas and contexts to build
requests, and resource types are global, not per context. The resources
themselves, a type's schema and context documents among them, are
RBAC-checked on their own routes.

The Django admin shows `Resource` and `ResourceType` to superusers only: a
write there bypasses scoped-rbac, the workflow and JSON Schema validation.

### Search (optional)

With the `search` extra, also add `haystack` to `INSTALLED_APPS` and configure a
connection, for example:

```python
HAYSTACK_CONNECTIONS = {
    "default": {
        "ENGINE": "haystack.backends.whoosh_backend.WhooshEngine",
        "PATH": BASE_DIR / "whoosh_index",
    },
}
HAYSTACK_SIGNAL_PROCESSOR = "scrud_django.search.OnCommitSignalProcessor"
```

`OnCommitSignalProcessor` updates the index when a write commits. haystack's
own `RealtimeSignalProcessor` indexes inside the write, so a write that
scrud-django then rolls back (for example with 403, when the stored resource
lands in a context the writer may not write in) would stay in the index.

### Workflow actions

`SCRUD_WORKFLOW_ACTIONS` lists dotted paths to mappings of type URI to
`{"on_save": callable, "on_delete": callable}`. An action runs when the write
of a resource of its type commits, never for a write that is rolled back. The
`scrud_post_save` and `scrud_post_delete` signals fire inside the write; a
receiver whose work leaves the database should wrap it in
`transaction.on_commit`.

### Upgrading

Version 0.12.0 has breaking changes (Django 5.2, uv, scrudful 0.8, scoped-rbac
0.11, JSON-only bodies, workflow policies in every context, RBAC-filtered
lists with new migrations to run, problem details for precondition errors, a
superuser-only admin). Read the
[0.12.0 release notes](https://gitlab.com/dcharbon/scrud-django/-/blob/develop/docs/release-notes.md) before upgrading.

## Development Get Started

Development uses [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pre-commit install
```

### Confirm setup

```bash
make test
make lint
make migrations-check
```

Build the documentation with `make docs`.

### Start the demo application

```bash
cd demo
uv run python manage.py migrate
uv run python manage.py runserver
```

The API root is at http://127.0.0.1:8000/api/. The demo reads
`DJANGO_SECRET_KEY`, `DJANGO_DEBUG` and `DJANGO_ALLOWED_HOSTS` from the
environment; its defaults (a public development key, DEBUG on, localhost
only) are for running it locally, and with DEBUG off it needs a key of its
own.

The `manage.py` at the repository root runs on the in-memory test settings
(nothing persists): use it for `check` and the migrations check only.
