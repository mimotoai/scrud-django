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

Add `rest_framework`, `reversion`, `scoped_rbac` and `scrud_django` to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
  ...
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
keeping scrud-django's own validation and workflow error bodies:

```python
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["scoped_rbac.permissions.IsAuthorized"],
    "EXCEPTION_HANDLER": "scrud_django.exceptions.scrud_exception_handler",
    ...
}
```

DRF's own default permission is `AllowAny`. The scrud-django viewsets set
`IsAuthorized` themselves, but any other view in your project (and any view you
build on scoped-rbac) relies on `DEFAULT_PERMISSION_CLASSES` to fail closed.

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

The API root is at http://127.0.0.1:8000/api/.
