"""The default URLconf a project includes (`tests.test_default_urls`):
``scrud_django.urls`` (ninja's API, and scoped-rbac's semantic documents)
under ``/api/``."""

from django.urls import include, path

urlpatterns = [
    path("api/", include("scrud_django.urls")),
]
