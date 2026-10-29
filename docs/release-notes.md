# Change Log

## 0.12.0

This release moves scrud-django to Django 5.2, uv, scrudful 0.8 and
django-scoped-rbac 0.11. It has several breaking changes.

### Requirements

* Python 3.11 or newer and Django 5.2 are required.
* `django_jsonfield_backport` is no longer used. Remove it from `INSTALLED_APPS`.

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
  user cannot read now gives 404. Resources with no `rbac_context` belong to the
  default context `""`.
* Unauthenticated requests now get 401.

### API changes

* `scrud_django.decorators` is removed. Use `scrudful.rest_framework` instead.
* `GenericViewMixin` subclasses must provide the DRF `get_queryset()` and a
  lookup instead of `get_instance`.
* `scrud_exception_handler` keeps scrud-django's own error bodies and passes
  every other exception to scoped-rbac's problem details handler.

### Known issues

See [follow-ups](follow-ups.md) for known issues.
