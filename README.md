# scrud-django
A Django application for SCRUD REST services. Semantic REST API generation.

Requires Python 3.11+ and Django 5.2. Supported databases: SQLite,
PostgreSQL, MySQL 8.4+ and MariaDB 11.4+ (see [Databases](#databases)).

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

### Databases

scrud-django supports SQLite, PostgreSQL, MySQL 8.4+ and MariaDB 11.4+. CI
runs the suite on SQLite, PostgreSQL 14, MySQL 8.4 and MariaDB 11.4.

Run the database at READ COMMITTED, the supported transaction isolation level
(PostgreSQL's default; Django's MySQL backend, which MariaDB uses too, sets it
unless `OPTIONS["isolation_level"]` says otherwise). `manage.py check --database
<alias>`, with the alias your router gives for `Resource` writes (and
`migrate` of it), warns about another (`scrud_django.W002`). A file SQLite
database that takes concurrent writes needs
`"OPTIONS": {"transaction_mode": "IMMEDIATE"}`: otherwise a second concurrent
writer fails with "database is locked" instead of waiting.

MySQL and MariaDB:

- Use Django's MySQL backend with [mysqlclient](https://pypi.org/project/mysqlclient/),
  InnoDB tables (the default), and a utf8mb4 database (MariaDB stores a
  resource's JSON content as text in the database's character set) and
  connection (Django's default `OPTIONS["charset"]`).
- Contexts are matched exactly, code point for code point, whatever the
  database's collation: the `ResourceContext` and `ResourceRemoval` contexts
  are stored as their UTF-8 bytes (`VARBINARY(2048)`), so `"acme"`,
  `"ACME"`, `"acmé"` and `"acme "` are four contexts, as on the detail route.
- On MariaDB a request body may be nested at most 31 levels deep (it is 100
  elsewhere): MariaDB's JSON column refuses a deeper document.

### Registering resource types

`scrud_django.urls` serves the built-in types (JSON Schemas, JSON-LD
contexts, indexing and workflow policies). Register each type of your own
with `registration.json_resource_type`, which stores the type, its JSON
Schema and its JSON-LD context (as resources) and returns the URLs of its
list (`<slug>/`: GET, HEAD, POST) and detail (`<slug>/<pk>/`: GET, HEAD,
PUT, DELETE) routes, named `<slug>-list` and `<slug>-detail`:

```python
from django.urls import include, path
from scrud_django import registration

NOTE = "https://example.com/json-ld/Note"

urlpatterns = [
    path("api/", include("scrud_django.urls")),
    path(
        "api/",
        include(
            registration.resource_types(
                registration.json_resource_type(
                    resource_type_uri=NOTE,
                    slug="notes",
                    schema_func=lambda: {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                    context_func=lambda: {
                        "@context": {"text": "https://schema.org/text"}
                    },
                ),
            )
        ),
    ),
]
```

`schema_func` and `context_func` return the document (a `dict`, or its JSON
text, for example read from a file of your package, as the demo's
`demo/demo/urls.py` does). A POST or PUT body is validated against the
schema, whose `$ref`s may only point inside it (`#/...`). The registration
runs when the URLconf is imported: before the first `migrate` it logs that
it failed and registers nothing, which is expected. Pass
`viewset_class=ResourceViewSetGenericFilter` (`scrud_django.views`) for a
list that filters on its query parameters (`?text=...`).

Grant roles (scoped-rbac) on the type URI for its resources (`http.GET`,
`http.POST`, `http.PUT`, `http.DELETE`) and `http.GET` on its list type,
`https://api.openteams.com/json-schema/ResourceCollection?contents_type=<type URI>`
(`scrud_django.collection_type_uri_for`), in each context whose resources a
user may list. A resource's context is its content's `rbac_context` (the
default context `""` without one).

A grant conditioned on the `scrud_workflow` operator
(`{"condition": {"operator": "scrud_workflow", "authorized_transitions": ["cancel"]}}`)
grants workflow transitions and nothing else: the transitions it lists, in a
POST or PUT the workflow check covers. It never allows a GET, a list or a
DELETE, even with the action `"*"`, so a role that should read needs its own
`http.GET` grant. `authorized_transitions` must be a list of transition names
(`__INITIALIZE__` for a create, `__SAME_STATE__` for an update that keeps its
state); anything else refuses everywhere.

A list shows what the detail route shows, as far as the view's
`IsAuthorized` permissions (and subclasses of it) decide. Another DRF
`BasePermission` on the view, such as an owner check in
`has_object_permission`, is not asked for lists, as in DRF: its detail route
refuses a resource that its list still shows. Put such a rule in an
`IsAuthorized` subclass (override `has_object_permission` or
`has_context_permission`), or apply it in a filter backend of the view (or its
`get_queryset`), for the list to follow it.

A write is checked in the contexts the resource ends up in, every one of
them, and (by the object check of a PUT or DELETE) in any one context it is
stored in. Removing a resource from a context, by a PUT whose `rbac_context`
leaves it out, needs no grant in that context unless a workflow policy applies
there, as a DELETE allowed in one context deletes the resource from all of
them.

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
scrud-django writes a resource, its context rows and its removal stamps in one
transaction there, and the `scrud_django.E004` system check reports a split.
Routing these models by instance hint is not supported.

Lists filter resources by their `ResourceContext` rows, one per context the
content names, which `Resource.save()` (and so `ResourceRegistration` and the
API) keeps in step. Rows written with `QuerySet.update()`, `bulk_create()` or
`bulk_update()` go stale. `manage.py scrud_sync_contexts --check` lists the
resources whose rows disagree with their content (exit status 1 if any), and
`manage.py scrud_sync_contexts` re-syncs them; the `scrud_django.W004`
database check reports resources that have no row at all.

DRF's own default permission is `AllowAny`. The scrud-django viewsets set
`IsAuthorized` themselves, but any other view in your project (and any view you
build on scoped-rbac) relies on `DEFAULT_PERMISSION_CLASSES` to fail closed.

Permission checks get plain data, as scoped-rbac's own do. On a POST or PUT,
your `IsAuthorized` subclasses (`has_permission`, `contexts_from`) and the
operators of your grants' conditions get the parsed body itself
(`request.data`); the workflow check gives their `has_context_permission`
and operators the body as sent, with the transition it authorizes as
`scrud_django.permissions.current_transition()`; and every object check of a
`Resource` view (the detail route's, the post-save check, a list's
per-object check) gives `has_object_permission`, `has_context_permission`,
`contexts_for` and operators the resource's content, the `dict` the
`Resource` holds (`{}` for content that is not a JSON object). These are
exact `dict`s and `list`s, so on `Resource` views an operator that evaluates
CEL itself passes what it got. `GenericViewMixin` views give their object
checks your model's instance, as scoped-rbac does, so an operator there
that evaluates CEL itself passes the model's `to_cel()`: give the model one.
`scoped_rbac.access.cel_mapping(None, obj)` reads it too, but without a
request it leaves out the fields a model's `cel_resource(request)` gives the
policy's CEL, and for a model without `to_cel()` it is `{}`. An operator
written as a denial (`not cel.is_true(...)`) must therefore refuse when the
fields it reads are missing: on `{}` the CEL result is undetermined, and
`not` turns it into an allow. A check must only read what it gets (`obj.get("owner")`, never
`obj.pop("owner")`): one that changes it changes what is stored (or, on a
read, what the response shows), a bug in that check. Scoped-rbac's default
`contexts_for` reads a resource's contexts from its content on the scrud
views, whatever your `get_permissions` returns; a `contexts_for` of your own
calls `scrud_django.permissions.resource_contexts(obj)` where it would call
`super().contexts_for(obj)`, which cannot read a `dict` (a 500, failing
closed).

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
(nothing persists): use it for `check` and the migrations check only. With
`SCRUD_TEST_PG_HOST` set (see `tests/settings.py`) it uses that PostgreSQL
server's `SCRUD_TEST_PG_NAME` database (default `scrud`) instead, where
writes persist; likewise `SCRUD_TEST_MYSQL_HOST` for a MySQL or MariaDB
server.

To run the suite on MySQL or MariaDB, add the driver, which builds against
the MySQL client library (not a dev dependency):

```bash
SCRUD_TEST_MYSQL_HOST=127.0.0.1 SCRUD_TEST_MYSQL_PASSWORD=... \
  uv run --with mysqlclient==2.3.0 pytest
```
