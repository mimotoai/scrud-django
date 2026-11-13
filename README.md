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

## Server-rendered UI

The optional `scrud_django.ui` app (`pip install "scrud-django[ui]"`) serves
every resource URL as `text/html` as well, rendered on the server through
templates and stylesheets stored as resources, layered per RBAC context and
shown exactly as the viewer is authorized, with htmx for editing. See the
[UI documentation](docs/ui.md).

## django-ninja (preview)

scrud-django can also serve its resource types through
[django-ninja](https://django-ninja.dev), answering as the DRF routes do (the
same statuses, headers and bodies; `tests/test_front_end_parity.py` replays
the same exchanges against both). It is additive and a preview: nothing
changes for a DRF project.

```bash
uv add "scrud-django[ninja]"
# or: pip install 'scrud-django[ninja]'
```

```python
# urls.py
from django.urls import include, path

from scrud_django import registration
from scrud_django.ninja import api

scrud_api = api(
    types=[
        registration.json_resource_type_spec(
            # the arguments of registration.json_resource_type
        ),
    ],
)

urlpatterns = [
    path("api/", scrud_api.urls),
    path("", include("scoped_rbac.semantic_urls")),
]
```

* The factory is `scrud_django.ninja.api(*, types=(), urls_namespace="scrud",
  **ninja_kwargs)`. `types` are `TypeSpec`s, from
  `registration.json_resource_type_spec(...)` and
  `registration.computed_resource_type_spec(...)`; it serves them and always
  the built-in types (`json-schema/`, `json-ld/`, `indexing-policies/`,
  `workflow-policies/`: every type's Links name its schema and context
  resources there), with the service catalog (`services/`), scoped-rbac's
  role routes and the collection documents.
* It serves no interactive docs and no OpenAPI document unless you pass
  `docs_url=` and `openapi_url=` (they go to `NinjaAPI`, as the rest of
  `ninja_kwargs` do); those routes are public. Each operation has an
  `operationId` of its own (`<slug>_list`, `<slug>_retrieve`, ...), so the
  document is valid.
* Do not pass `auth=`: `api()` raises `TypeError`. scrud resolves the user of
  every request itself, so an anonymous request gets the anonymous user's
  grants and a hidden item is a 404.
* Two APIs in one process need distinct `urls_namespace`s. Links (`href`,
  `Location`, `Link` headers, `services/`) are reversed within the namespace
  the request was resolved under.
* A ninja-only URLconf must also `include("scoped_rbac.semantic_urls")`: the
  role routes' (`roles/`, `role-assignments/`, `user-rbac-policy/`) semantic
  Links reverse those global names, and answer 500 without it.
* `services/` lists every type registered in the process. Serving two APIs
  with different `types=` in one process, or a ninja-only URLconf after DRF
  routes registered other types, makes `services/` fail (500); see
  `services-unrouted-types` in the [follow-ups](docs/follow-ups.md).
* The UI types (`scrud_django.ui`) and the studio types are not served on
  ninja yet, nor are the `ui-css-files` and `ui-js-files` views, and a ninja
  route never answers `text/html`.
* `scrud_django.urls` stays DRF until the release that switches it; include
  it as before for a DRF project.

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

Include the scrud-django URLs. They include scoped-rbac's semantic URLs
(`semantic/`), which serve the schemas and JSON-LD contexts of scoped-rbac's
own Role, RoleAssignment and Policy resources (the role endpoints' `Link`
headers point there); a scrud resource type's schema and context are its
`json-schema/<id>/` and `json-ld/<id>/` resources:

```python
urlpatterns = [
    path("api/", include("scrud_django.urls")),
]
```

If you mount URLs yourself, include `scoped_rbac.semantic_urls`, for example
`path("semantic/", include("scoped_rbac.semantic_urls"))`.

Include scrudful's HTTP Schema Resource route once, in the root URLconf, and
set the base URL the API is served at. An OPTIONS is a 204 without content
whose `Link` names the path's HTTP Schema Resource (HSR), a JSON document
describing its methods that a GET serves; scrudful builds it without a request,
so its links, and the HSR link each response carries, resolve against
`SCRUDFUL["BASE_URL"]` (scrudful resolves only relative links against it;
scrud's other response Links are absolute URLs built from the request).
`Allow` lists the methods the requester's grants allow there. A list's POST is
checked without a body, so it is listed when they may write in some context,
whatever conditions their grants put on the body. A detail route's methods are
checked against the stored resource, which the OPTIONS loads as the first
method the requester may use somewhere loads it: a resource that is missing, or
that method may not reach, is a 404; a requester who may use no method there
gets 204 with `Allow: OPTIONS` whether or not it exists.

```python
# urls.py (the root URLconf)
from scrudful.rest_framework import hsr_urls

urlpatterns = [
    path("api/", include("scrud_django.urls")),
    path("", include(hsr_urls())),
]
```

```python
# settings.py
SCRUDFUL = {"BASE_URL": "https://api.example.com"}
```

Without the include, every request of a scrud view raises
`ImproperlyConfigured` (and `manage.py check` reports `scrudful.E003`).

### Databases

scrud-django supports SQLite, PostgreSQL, MySQL 8.4+ and MariaDB 11.4+. CI
runs the suite on SQLite, PostgreSQL 14, MySQL 8.4 and MariaDB 11.4.

Run the database at READ COMMITTED, the supported transaction isolation level
(PostgreSQL's default; Django's MySQL backend, which MariaDB uses too, sets it
unless `OPTIONS["isolation_level"]` says otherwise). `manage.py check --database
<alias>`, with the alias your router gives for `Resource` writes (and
`migrate` of it), warns about another (`scrud_django.W002`). The database
checks (`W003`-`W006`, and `E002`, `E003`, `E005` and `W001` for stored
workflow policies) report nothing on a database whose scrud_django
migrations are not all applied, so that `migrate` can run, nor on one where
your routers' `allow_migrate` keeps scrud_django's tables out (an analytics
database); one that fails reading a migrated database (a missing grant, a
schema out of step with the migrations) warns `scrud_django.W007`, naming
the check and the error, rather than passing. A file SQLite database that
takes concurrent writes needs
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
runs when the URLconf is imported. When the database fails then, it still
returns the routes and logs the failure (logger
`scrud_django.registration`): a warning while the database is
unreachable, not migrated (before the first `migrate`, which is expected),
or its migration state cannot be read (a database error); an error when it
is reachable and migrated (a missing grant, a schema out of step with the
migrations), which needs fixing, or when its migration state cannot be told
for another reason (a broken migration graph, logged with its traceback).
Whether it is migrated is asked once for the registrations that fail
together, not once each. The type is registered when a request (or code)
next reads it, so the process serves it once the database is back, and
until then a request for it fails (500), not 404. A process start stores
nothing when the type and its documents are unchanged; under `DEBUG`, a
type's row follows the registration's arguments (`slug`, `revision`,
`schema_uri`, ...); outside
it, the stored row keeps its slug, revision and URIs, and only its
documents (and `uses_reversion`) are updated. Pass
`viewset_class=ResourceViewSetGenericFilter` (`scrud_django.views`) for a
list that filters on its query parameters (`?text=...`; with `scrud_django.ui`
installed, its URL parameters `ui-branch`, `ui-branch-confirm`, `ui-scope`,
`ui-mode` and `ui-safe` are not filters).

Hooks on `json_resource_type`, honoured by every front end, narrow what the
grants allow (they can only refuse, and refuse superusers too, as an
`IsAuthorized` subclass does). A hook must return `True` to allow: any other
return (`None`, `"yes"`, `1`) refuses.

- `checks`: callables `(request, resource_type, body, context) -> bool` that
  govern **writes**: asked of a write in each context it writes, of the
  stored resource of a PUT, PATCH or DELETE in each of its contexts, of the
  saved resource in each of its contexts, and of a workflow transition in
  each context it is authorized in. Unlike an `IsAuthorized` subclass, whose
  object check allows a PUT or DELETE when any context the resource is
  stored in allows it, `checks` must allow in every one: a resource in
  `["a", "b"]` whose check refuses `"b"` can be neither updated (not even
  moved to `"a"`) nor deleted (403). They never hide a read, and a type with
  only `checks` lists by SQL; a type that wants both registers both.
- `object_checks`: callables `(request, resource_type, obj) -> bool` that
  govern **reads and objects**, asked of a resource's content wherever the
  object is checked (the detail route of every method, each listed
  resource). A refused resource is hidden (404). A write of a resource they
  would hide is refused too (403, after the save, which is rolled back).
- `content_filter=True`: the list filters on its query parameters. It is read
  when the routes are built, so registering a slug again replaces the other
  hooks but not an already built route's filter.

Hooks are keyed by the type's IRI: registering a type again replaces the
hooks registered for it before (a different set logs a warning on
`scrud_django.core.types`), and `requirement_for` imports `ROOT_URLCONF`
once per process, so a worker that never served a request still has them.
Slugs sharing a type IRI must register the very same hook objects, or the
second gets `ImproperlyConfigured`: use module-level functions, since
closures a factory builds in the URLconf are new objects each time it is
imported, and a re-import then raises.

To move the rule of an `IsAuthorized` subclass's `has_context_permission`
to hooks, register it twice. As a `checks=` callable it governs writes, but
it no longer hides reads, and it is stricter on a resource stored in
several contexts: the subclass allowed a PUT or DELETE that any of them
allowed, a check must allow it in every one. To hide what the subclass
refused to read, add an `object_checks=` callable that applies the same rule to each of the
resource's contexts (`rbac_contexts_of(obj)`, from
`scrud_django.mixins.resource_mixin`) and combines the answers as the
subclass did. scoped-rbac's `has_object_permission` allows when any context
allows, so use `any` unless the subclass overrode it:

```python
from scrud_django.mixins.resource_mixin import rbac_contexts_of


def not_archived(request, resource_type, obj, context):
    return context != "archived"


def readable_somewhere(request, resource_type, obj):
    return any(
        not_archived(request, resource_type, obj, context)
        for context in rbac_contexts_of(obj)
    )


json_resource_type(..., checks=[not_archived], object_checks=[readable_somewhere])
```

Grant roles (scoped-rbac) on the type URI for its resources (`http.GET`,
`http.POST`, `http.PUT`, `http.DELETE`) and `http.GET` on its list type,
`https://api.openteams.com/json-schema/ResourceCollection?contents_type=<type URI>`
(`scrud_django.collection_type_uri_for`), in each context whose resources a
user may list. A resource's context is its content's `rbac_context` (the
default context `""` without one).

A grant conditioned on the `scrud_workflow` operator
(`{"condition": {"operator": "scrud_workflow", "authorized_transitions": ["cancel"]}}`)
grants workflow transitions and nothing else: the transitions it lists, in a
POST or PUT the workflow check covers, a scrud viewset's `create` (POST to
the list) or `update` (PUT to the detail route). It never allows a GET, a
list or a DELETE, even with the action `"*"`, nor any other write, such as an
`@action` of a viewset subclass, which the workflow check does not see, so a
role that should read needs its own `http.GET` grant, and one that should
call such an action its own grant for its method. `authorized_transitions` must be a list of transition names
(`__INITIALIZE__` for a create, `__SAME_STATE__` for an update that keeps its
state); anything else refuses everywhere.

A list shows what the detail route shows, as far as the view's
`IsAuthorized` permissions (and subclasses of it) decide: an object such a
subclass refuses to read is missing from the list and a 404 on its detail
route, for every method (an object the user may read but not write gets
403). Another DRF
`BasePermission` on the view, such as an owner check in
`has_object_permission`, is not asked for lists, as in DRF: its detail route
refuses a resource that its list still shows. Put such a rule in an
`IsAuthorized` subclass (override `has_object_permission` or
`has_context_permission`), or apply it in a filter backend of the view (or its
`get_queryset`), for the list to follow it.

A POST to a list evaluates its `If-Match`, `If-Unmodified-Since` and
`If-None-Match` (RFC 9110) against the list's `ETag` and `Last-Modified` as
the requester would GET them from that URL: a failed one is a 412 and
nothing is created. Computing them is a GET of the list, so where that GET
is a 400 `conditional_list_too_large` (past
`SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]` rows evaluated one by one), so is
such a POST. `If-Match: *` and `If-None-Match: *` need no validators (the
list always exists): the first creates as an unconditional POST does, the
second is always a 412. A POST that needs the validators evaluates them and
creates in one transaction holding its type's `ResourceType` row, so two
such POSTs with the same `If-Match` give one 201 and one 412.

A write is checked in the contexts the resource ends up in, every one of
them, and (by the object check of a PUT or DELETE) in any one context it is
stored in. Removing a resource from a context, by a PUT whose `rbac_context`
leaves it out, needs no grant in that context unless a workflow policy applies
there, as a DELETE allowed in one context deletes the resource from all of
them.

An `rbac_context` that is a list of strings stores the resource in each of
them. Any other value that is not a string (`null`, a number, an object,
or a list holding a number, bool or null) stores it in the default context
`""`. A list holding an array or an object is a 400 (`invalid`, naming
`rbac_context`).

A grant's CEL condition that reads `rbac_context` reads the context it is
evaluated in: the content with that context as its `rbac_context`. scrud's
post-save check, the object checks of a detail route (and whether a refused
resource is a 404 or a 403) and lists read every context so, and the
workflow check every context it checks, those an update moves the resource
out of included; scoped-rbac's request check of a POST or PUT reads each
value of a list of strings so, and any other body as written. So
`rbac_context != "x"` refuses a write into `"x"` (or into `""` with an
`rbac_context` that is not a string), and refuses a resource in `"x"`
however it is stored. A resource stored in several contexts is readable,
and may be updated or deleted, where any one of its contexts allows it:
`rbac_context != "x"` does not hide a resource stored as `["x", "y"]` from
a grant that holds in `"y"`. To keep a grant out of a context, assign it
in the contexts it covers (`RoleAssignment.rbac_context`) instead.

Code of your own reads what it always did: an operator function, and a
`has_context_permission` of your own, get the content as stored (or the
body as sent), its `rbac_context` the whole value. Its `super()` call reads
that too; to have scoped-rbac's check read each context as the object is
there, pass it `scrud_django.permissions.condition_resource(obj, context)`.
On a view with such an override, the scrud views also ask the grants,
each context read as the resource is there, on the detail route and after
a write, as its list does: the override narrows what the grants allow.
A context that a `contexts_for` of your own names, and that is not one of
the resource's, leaves the object as it is (`docs/follow-ups.md`,
`custom-contexts-conditions`). A view of your own that composes scoped-rbac's
`AccessControlledAPIView` with `ResourceViewMixin` puts
`scrud_django.mixins.view_mixins.ReadableMixin` first, as the scrud viewsets
do, so that a refused write of a resource readable in one of its contexts is
a 403, not a 404.

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

Server code that writes a `WorkflowPolicy` resource should go through
`ResourceRegistration`. Code that locks a policy's row itself
(`Resource.objects.select_for_update()`) and then saves or deletes it must
call `scrud_django.registration.lock_if_workflow_policy` in the same
transaction, on every database, **before** the `select_for_update()`: the
save or delete takes the `WorkflowPolicy` type lock, and taking it after the
row's lock deadlocks with an API write of the policy, which holds the type
lock and waits for the row. Read the type without locking the row:

```python
from django.db import transaction

from scrud_django.models import Resource
from scrud_django.registration import (
    get_resource_type_for,
    lock_if_workflow_policy,
    write_db,
)

db = write_db()
with transaction.atomic(using=db):
    # Before the row lock; the type is read without it.
    lock_if_workflow_policy(get_resource_type_for("workflow-policies", using=db), db)
    policy = Resource.objects.using(db).select_for_update().get(pk=pk)
    policy.content = new_content
    policy.save()  # or policy.delete()
```

A plain `save()` or `delete()`, without a row lock of its own, takes the type
lock before the row by itself.

Lists filter resources by their `ResourceContext` rows, one per context the
content names, which `Resource.save()` (and so `ResourceRegistration` and the
API) keeps in step. Rows written with `QuerySet.update()`, `bulk_create()` or
`bulk_update()` go stale (and run no workflow action, see
[Workflow actions](#workflow-actions)). `manage.py scrud_sync_contexts
--check` lists the resources whose rows disagree with their content (exit
status 1 if any), and `manage.py scrud_sync_contexts` re-syncs them; the
`scrud_django.W004` database check reports resources that have no row at
all. The workflow policy of a context is found by those rows too, and also
by the policy's content, so that stale rows do not hide a policy (see
`scrud_django.workflow.stored_policies_in` for the one case they still can).

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

### Computed resource types

A computed type has a schema and a JSON-LD context like any resource type, but
its content is computed per request, never stored: a read-only view of data
the server derives for the requesting user. Subclass
`scrud_django.computed.ComputedResourceViewSet`, implement
`compute(request, key)` (return a mapping; raise `Http404` or
`PermissionDenied`), and register it with `computed_resource_type`:

```python
from scrud_django import registration
from scrud_django.computed import ComputedResourceViewSet


class BranchSummaryViewSet(ComputedResourceViewSet):
    def compute(self, request, key):
        # Return only what request.user may read as JSON.
        return {"name": key, "items": []}


urlpatterns += registration.computed_resource_type(
    resource_type_uri="http://example.org/BranchSummary",
    slug="branch-summaries",
    schema_func=lambda: SCHEMA_JSON,
    context_func=lambda: CONTEXT_JSON,
    viewset_class=BranchSummaryViewSet,
)
```

It routes `branch-summaries/<key>/` only (route name `branch-summaries-detail`;
no list route, so a computed type is not in the service catalog, which gives
each type's list URL) and adds no revision history. A GET answers the content with an
`ETag` (the SHA-256 of its canonical JSON) and a 304 for a matching
`If-None-Match`; any method other than GET, HEAD and OPTIONS is a 405. The response carries the
`Link` headers to the type's schema and JSON-LD context, as a stored resource's
does. The view requires an authenticated
user and applies no authorization of its own to the content: `compute` must ask
the same checks the stored resources' views do. The type is registered, so a
published branch's prefix claims its slug, and HTML negotiation renders it
through the UI cascade as view-only content.

Instead of subclassing, pass `compute=` to `computed_resource_type`: a
callable `(request, key) -> Mapping` (raise `Http404` or `PermissionDenied`)
that supplies the content for the default `ComputedResourceViewSet`. One of
`compute=` and `viewset_class=` is needed; registering a slug again replaces
its `compute`.

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

### Workflow policies

A workflow policy (a `WorkflowPolicy` resource for one type in its
`rbac_context`, else the type's `SCRUD_WORKFLOW["DEFAULT_WORKFLOW_POLICIES"]`
entry) names its states in `initial_states` (where a create may put a
resource) and `states` (the others); a state is named in one of them, not
both. Each state's condition is a CEL expression over the resource's content,
each top-level property a variable. Unlike a grant's condition, it reads
`rbac_context` as stored: the whole value (a list for a resource in several
contexts), the same in every context the write is checked in, not the context
being checked. State conditions are compiled without type checking, so a
misspelled property (`stauts == "done"`) is accepted, never present, and its
state unreachable: `manage.py check --database default` warns
(`scrud_django.W005`) where a default policy's condition reads a property its
type's JSON Schema does not define (`rbac_context` is always allowed); stored
policies are not checked. It says nothing where it cannot tell: for a type not
registered on that database or whose schema is not stored there, a schema
without a `properties` object or one that may allow other properties
(`additionalProperties` `true` or a schema; absent counts as `false`) or add
some (`patternProperties`, `allOf`, `anyOf`, `oneOf`, `$ref`,
`if`/`then`/`else`, `dependencies`, `dependentSchemas`,
`unevaluatedProperties`), a default that is invalid (`scrud_django.E001`), or
a database that is not migrated. A condition that fails to evaluate (a
property the content lacks) is no match, and is logged at DEBUG on the
`scrud_django.workflow` logger, naming the policy and the state.

A settings default is a fallback, not a floor: a stored `WorkflowPolicy` for
a type in a context replaces the default for that type there (deleting it
restores the default); it is not combined with it and need not be stricter.
Writing a `WorkflowPolicy` is authorized by grants on the `WorkflowPolicy`
type in its context alone, not on the type it governs. So whoever may POST a
`WorkflowPolicy` in a context can, with no rights on the governed type, give
any type there a workflow of their own, looser than the default, and rename or
redefine its transitions, which changes what every `scrud_workflow` grant in
that context allows (its `authorized_transitions` name transitions). Treat
`http.POST`, `http.PUT` and `http.DELETE` on
`http://api.openteams.com/json-ld/WorkflowPolicy` as administrative grants;
no deployment-wide workflow invariant can be enforced through the defaults
(`docs/follow-ups.md`, `policy-defaults-are-fallbacks`).

A policy's state conditions must be mutually exclusive: content is in the
one state whose condition it meets. The order of the conditions decides
nothing, since a policy stored in a JSON column does not keep it
(PostgreSQL's `jsonb` and MySQL's JSON reorder an object's keys). Content
that meets the conditions of more than one state is ambiguous: a create or
update whose body is ambiguous, or an update of a resource whose stored
content is, is refused (400, `workflow-transition-invalid`, with the states
it meets in `matching_states` and in its message, unless the error is
hidden from a writer who may not read the policy or the resource). A policy
whose conditions overlap is not refused when it is written (whether two CEL
conditions can both hold is not decidable in general); write each condition
so that it excludes the others (`status == "review" && !published`, not
`status == "review"` beside `published`).

### Workflow actions

`SCRUD_WORKFLOW_ACTIONS` is a list (or tuple) of dotted paths to mappings of
type URI to `{"on_save": callable, "on_delete": callable}`; a later mapping's
entry for a type replaces an earlier one's. An action runs when the write
of a resource of its type commits, never for a write that is rolled back. The
`scrud_post_save` and `scrud_post_delete` signals fire inside the write; a
receiver whose work leaves the database should wrap it in
`transaction.on_commit(..., using=using)`. Their `using` is the database the
write went to (the router's `db_for_write`), even for an instance read from a
replica.

Actions and receivers are called as `action(sender, **kwargs)`, with the
keyword arguments Django gives a receiver: `signal` (the signal sent),
`instance`, `using`, `resource_type_uri` and `action` (`CREATE`, `UPDATE` or
`DELETE`); `scrud_post_save` adds `update_fields`, `transition_data`,
`transition_error` and `transitions`, and `scrud_post_delete` adds `pk`, the
deleted resource's primary key (the `instance` is the deleted one, whose `pk`
Django has set to `None`).

`transition_data` is a `scrud_django.workflow.TransitionData`
(`proposed_state`, `prior_state`, `transition`; immutable) under the
workflow policy in effect in the first context the content names that has
one, or `None` when no policy applies or the write is invalid there;
`transition_error` is then the workflow error, and `None` otherwise. Its
`transition` is the policy's transition name, `__SAME_STATE__` for an update
that stays in its state, or `__INITIALIZE__` (no `prior_state`) for a create
and for an update that moves the resource into that context. A write is
invalid in a context when no transition leads from the stored state to the
new one (or a `restrict_update` forbids staying), when a create or move in
is in no initial state (`WorkflowInvalidInitialStateError`), or when the new
or stored content meets the conditions of more than one state
(`WorkflowAmbiguousStateError`). `transitions` is a read-only mapping
(`types.MappingProxyType`, shared by every receiver; copy it with `dict()`
to change it) of every context the stored resource or the new content is
in, where a policy applies, to the write's `TransitionData` there (`None`
if invalid there): a transition where the resource stays, `__INITIALIZE__`
where it enters, and `__LEAVE__` (no `proposed_state`; `prior_state` is the
state it left) where it leaves. A save of an instance whose row is not on
the database written to (a copy with `save(using=...)`) is reported as a
create.

Only API writes are checked against the workflow, so only a save that skipped
the check (the admin, `ResourceRegistration`, server code) can be invalid: it
is saved anyway, reported as above, and each context where it is invalid is
logged as a WARNING on the `scrud_django.workflow` logger (before
`scrud_post_save` is sent, so a receiver that raises cannot suppress it),
naming the resource's pk, its type URI, the context, the policy and the
error (with the states the content meets, when it meets several). Such a
save still reads the policy of every context it concerns, and fails
(`ImproperlyConfigured`) where one has several stored policies or an invalid
one, as an API write does.

The requesting user is not passed: read it from a `contextvars.ContextVar`
that a middleware sets (see the example below), since receivers and actions
run in the request's thread.

Actions are delivered:

- **At most once.** An action waits in the connection's in-memory list of
  `on_commit` callbacks: a crash between the COMMIT and the call loses it,
  and nothing retries it. One that raises is logged (below) and dropped.
- **Synchronously**, in the thread that wrote, after the commit: in a
  request, before the response, so a slow action (SMTP, an HTTP call) adds
  to the request's latency. Outside a transaction an action runs at once.
- **In write order**, per database connection.
- **With the live `instance`**: changes made to it after the write, in the
  same transaction, are visible. Rely on `transition_data`, or snapshot
  what you need in a `scrud_post_save` receiver.

The signals are sent by `Resource.save()` and `delete()` (the API,
`ResourceRegistration`, the admin's change and delete pages) and, for each
resource it deletes, by a `Resource.objects` queryset's `delete()` (bulk
deletes, the admin's "delete selected"). `QuerySet.update()`,
`bulk_create()` and `bulk_update()` send none. Nor does a raw save:
`loaddata` and a django-reversion revert (`Version.revert()`) call
`save_base(raw=True)`, and as in Django, loading data runs no side effects.
To act on a revert, connect a receiver to Django's `post_save` for
`Resource`, which they send with `raw=True` (`loaddata` too).

A setting that is not a list or tuple of paths to mappings stops the process
at startup (`ImproperlyConfigured`). `manage.py check` reports a type's entry
that is not a mapping (`scrud_django.E007`), a key other than `on_save` and
`on_delete` (`E008`) and a value that is neither callable nor `None`
(`E009`); `check --database <alias>` warns about a type URI that is not a
registered resource type (`W006`). At write time such an entry runs nothing
and never fails the write. Neither does an action that raises: it is logged,
with its traceback, on the `scrud_django.workflow_actions` logger, naming
the action, the type URI, `on_save` or `on_delete` and the resource's pk.

The actions are run by a `scrud_django.scrud_signals.ScrudSignalProcessor`,
which reads the setting again when it changes (`override_settings` in
tests). One processor receives the signals at a time (a fixed
`dispatch_uid`, held strongly): building another one, such as an instance
of a subclass in the `ready()` of an app listed after `scrud_django`,
replaces the default one instead of running each action twice.

An event that must not be lost belongs in a transactional outbox: a
receiver stores it in a table in the write's own transaction, so it
commits or rolls back with the write, and a worker delivers it later, at
least once:

```python
# myapp/models.py
from django.db import models


class OutboxEvent(models.Model):
    resource_pk = models.BigIntegerField()
    type_uri = models.TextField()
    action = models.CharField(max_length=6)  # CREATE, UPDATE or DELETE
    transition = models.JSONField(null=True)
    user_id = models.BigIntegerField(null=True)
    content = models.JSONField(null=True)
    sent_at = models.DateTimeField(null=True)


# myapp/outbox.py, imported in your AppConfig.ready()
import dataclasses
from contextvars import ContextVar

from django.db import router, transaction
from django.dispatch import receiver
from django.utils import timezone
from scrud_django.scrud_signals import scrud_post_delete, scrud_post_save

from .models import OutboxEvent

current_request = ContextVar("current_request", default=None)


def current_request_middleware(get_response):  # add it to MIDDLEWARE
    def middleware(request):
        token = current_request.set(request)
        try:
            return get_response(request)
        finally:
            current_request.reset(token)

    return middleware


@receiver([scrud_post_save, scrud_post_delete], dispatch_uid="myapp.outbox")
def store_event(sender, instance, using, resource_type_uri, action, **kwargs):
    transition = kwargs.get("transition_data")
    # DRF sets the user it authenticates on the Django request too.
    user = getattr(current_request.get(), "user", None)
    OutboxEvent.objects.using(using).create(
        resource_pk=kwargs.get("pk", instance.pk),  # pk: sent for a delete
        type_uri=resource_type_uri,
        action=action,
        transition=dataclasses.asdict(transition) if transition else None,
        user_id=user.pk if user is not None and user.is_authenticated else None,
        content=instance.content,
    )


def send_events(deliver, batch=100):
    """Run by a worker (cron, Celery beat). `deliver` must be idempotent
    (keyed on the event's pk): an event is sent again if marking it fails.
    store_event writes each event where its write went (`using`): route
    OutboxEvent's writes to the alias scrud_django's models are written to,
    so that this reads them there, not from a replica."""
    using = router.db_for_write(OutboxEvent)
    with transaction.atomic(using=using):
        events = (
            OutboxEvent.objects.using(using)
            .select_for_update(skip_locked=True)
            .filter(sent_at=None)
            .order_by("pk")[:batch]
        )
        for event in events:
            deliver(event)
            event.sent_at = timezone.now()
            event.save(using=using, update_fields=["sent_at"])
```

### Upgrading

Version 0.15.0 refuses workflow writes whose content (new or stored) meets
more than one state; policies with two transitions for one pair of states or a
state in both `initial_states` and `states`; an update in place of a
`restrict_update` state through a transition whose `from` or `to` lists other
states too (only a transition from that state to itself alone allows it); and
POST or PUT `@action`s of your viewsets (and views that are not viewsets) for a
role whose only grant there is conditioned on `scrud_workflow` (403: give it a
grant that is not). It adds keyword arguments to `scrud_post_save`, makes
`TransitionData` immutable, runs `on_delete` actions for bulk deletes, fails a
server-side save closed on duplicate policies, or an invalid one, in any
context it concerns, and logs failed actions on
`scrud_django.workflow_actions` and invalid server-side saves on
`scrud_django.workflow`. Run `manage.py check --database <alias>` for
each database and work through the Upgrading list of the
[0.15.0 release notes](https://gitlab.com/dcharbon/scrud-django/-/blob/develop/docs/release-notes.md)
before upgrading.

Version 0.14.0 adopts django-scoped-rbac 0.12 and scrudful 0.10, which change
OPTIONS (a 204 linking an HTTP Schema Resource) and need the `hsr_urls()`
include, `SCRUDFUL["BASE_URL"]` and a scoped-rbac migration. Work through the
Upgrading list of the
[0.14.0 release notes](https://gitlab.com/dcharbon/scrud-django/-/blob/develop/docs/release-notes.md)
before upgrading.

Version 0.12.0 has breaking changes (Django 5.2, uv, scrudful 0.8, scoped-rbac
0.11, JSON-only bodies, object-level RBAC on detail routes, RBAC-filtered
lists with new migrations to run, workflow policies in every context, 428
for a missing precondition and new ETags, problem details for errors, a
superuser-only admin). Stop every 0.11 process before you migrate. Work
through the Upgrading list of the
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
uv run python manage.py createsuperuser
uv run python manage.py runserver
```

The API root is at http://127.0.0.1:8000/api/. Resource routes need a login
(anonymous requests get 401): log in to the browsable API at
http://127.0.0.1:8000/api-auth/login/ (the API root's "Log in" link) with
the superuser, or send Basic auth, for example
`curl -u <user>:<password> http://127.0.0.1:8000/api/partner-profiles/`.
The superuser may do everything; other users need scoped-rbac roles
(`api/roles/`, `api/role-assignments/`). The Django admin is at
http://127.0.0.1:8000/admin/. The demo reads
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
