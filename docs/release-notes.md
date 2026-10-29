# Change Log

## 0.12.0

This release moves scrud-django to Django 5.2, uv, scrudful 0.8 and
django-scoped-rbac 0.11. It has several breaking changes.

### Security caveat: lists are not filtered per resource

List endpoints do not check each resource's RBAC context. A user who may list a
resource type (a grant in any context) sees every resource of that type in the
list, including resources in contexts where they have no access, and which
their detail URL hides from them with a 404. Do not rely on RBAC contexts to
keep resources of one type apart from users who may list that type. See
[list-not-rbac-filtered](follow-ups.md#list-not-rbac-filtered).

### Requirements and packaging

* Python 3.11 or newer and Django 5.2 are required.
* `django_jsonfield_backport` is no longer used. Remove it from `INSTALLED_APPS`.
  If your project has migrations of its own that import
  `django_jsonfield_backport.models.JSONField`, change those imports to
  `django.db.models.JSONField` before you uninstall the backport; otherwise
  those migrations no longer import.
* django-haystack and Whoosh are no longer installed by default; they are in
  the `search` extra. If `haystack` is in your `INSTALLED_APPS`, install
  `scrud-django[search]`.

### Conditional requests

Conditional request handling now comes from scrudful 0.8.

* A PUT or DELETE with no precondition returns 428 instead of 400. Either
  `If-Match` or `If-Unmodified-Since` is enough.
* ETags no longer carry a version suffix.
* The `Link` header now uses `rel="describedby"` with the type
  `application/schema+json`.
* A failed precondition returns 412 with a JSON body.
* OPTIONS bodies use scrudful's Semantic HTTP format, with `x-rdf-context`.

### Authorization

* RBAC is enforced by scoped-rbac's `IsAuthorized` permission class. DRF's
  default permission is `AllowAny`, so a view without `IsAuthorized` is open to
  everyone, anonymous users included. `ResourceViewSet` and
  `ResourceViewSetGenericFilter` now set `permission_classes = [IsAuthorized]`
  themselves, so they fail closed whatever `DEFAULT_PERMISSION_CLASSES` says.
  Still set `"DEFAULT_PERMISSION_CLASSES": ["scoped_rbac.permissions.IsAuthorized"]`
  in `REST_FRAMEWORK` so that your other views fail closed too (see the README).
* Detail routes enforce object-level RBAC through scoped-rbac. An object the
  user cannot read now gives 404. A resource's `rbac_context` names its context
  only when it is a string or a list of strings. Resources with no
  `rbac_context`, or with any other value there (for example a JSON-LD context
  document's `{"@id": ...}` term definition), belong to the default context `""`.
* Unauthenticated requests now get 401.
* Request bodies must be JSON. `ResourceViewSet` and
  `ResourceViewSetGenericFilter` set `parser_classes = [RbacJSONParser]`
  (`scoped_rbac.rest`): a form-encoded or multipart body gets 415, and a JSON
  body that is not an object (for example a list) gets 400. A form body could
  name `rbac_context` twice; scoped-rbac authorized the request by the last
  value while the resource was stored in both contexts. Give your own
  `GenericViewMixin` views the same `parser_classes`.
* A create or update is checked again after the save, before it commits: the
  request's method must be allowed in **every** context the stored resource is
  in, or the write is rolled back with 403.
* `"rbac_context": ""` now means the default context everywhere, exactly as
  leaving the key out does. Before, a body with `"rbac_context": ""` skipped the
  default workflow policy, so its initial-state check, transition validity and
  transition authorization did not apply.

### API changes

* `scrud_django.decorators` is removed. Use `scrudful.rest_framework` instead.
* `scrud_django.settings`, `scrud_django.wsgi` and `scrud_django.asgi` are
  removed: the package no longer ships a Django project. Use your own project's
  settings, WSGI and ASGI modules.
* `scrud_django.utils.get_string_or_evaluate` is removed.
* `ResourceViewSet` and `ResourceViewSetGenericFilter` set
  `permission_classes = [IsAuthorized]`. A subclass that relied on a different
  `DEFAULT_PERMISSION_CLASSES` must set `permission_classes` itself.
* `scrud_django.urls` now also mounts scoped-rbac's semantic URLs at
  `semantic/`, which serve each resource type's schema and JSON-LD context. If
  you mount URLs yourself, include `scoped_rbac.semantic_urls` once.
* The Role and RoleAssignment endpoints that `scrud_django.urls` includes from
  `scoped_rbac.urls` now serve scoped-rbac 0.11's Role and RoleAssignment
  shapes. Clients of those endpoints must follow scoped-rbac 0.11.
* `GenericViewMixin` subclasses must provide the DRF `get_queryset()` and a
  lookup instead of `get_instance`.
* `scrud_exception_handler` keeps scrud-django's own error bodies and passes
  every other exception to scoped-rbac's problem details handler.
* The envelope's `last_modified` (and the search index's `modified_at`) no
  longer depend on `USE_TZ`. With `USE_TZ = False`, a stored modification time
  is naive and in `TIME_ZONE`; it used to be read as UTC, so the envelope
  disagreed with the `Last-Modified` header whenever `TIME_ZONE` was not UTC.
  Both now read it in `TIME_ZONE` (`scrud_django.utils.http_date_for`).

### Workflows

* Workflow state conditions are evaluated with scoped-rbac's CEL engine,
  `scoped_rbac.cel`, instead of `celpy`. A condition that cannot be evaluated
  for a resource, for example because it reads a property the resource does not
  have, still counts as not matching.
* An invalid workflow state condition (one that does not compile) now raises
  `django.core.exceptions.ImproperlyConfigured` when the policy is loaded.
* CEL equality differs from `celpy` in a few cases. Equality between
  different numeric types and between unrelated types is now defined: `1 == 1.0`
  and `3 != "x"` are true where they used to be evaluation errors (which
  counted as not matching), while `true == 1` is false. Resource data that CEL
  cannot represent (a mapping that is not a `dict`, a tuple, a `Decimal`, or an
  integer of 2**64 or more) now makes the condition not match.

### Known issues

See [follow-ups](follow-ups.md) for known issues. In particular:

* The default workflow policy is enforced only for resources in the default
  context. In any other context a user can move a resource to a state, or into
  another context, that the policy would forbid
  ([workflow-context-hop](follow-ups.md#workflow-context-hop)).
* An empty body sent as `application/json` is read as `{}`
  ([empty-json-body](follow-ups.md#empty-json-body)).
* Search-index and workflow-action side effects of a write that is rolled back
  with 403 are not undone
  ([post-save-side-effects](follow-ups.md#post-save-side-effects)).
