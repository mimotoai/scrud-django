"""The URLconf of the no-DRF harness (``tests/no_drf.py``), a project without
DRF: the default ``scrud_django.urls`` (ninja's API and scoped-rbac's
semantic documents) at ``api/``, and the UI app, which has no DRF."""

from django.urls import include, path

urlpatterns = [
    path("api/", include("scrud_django.urls")),
    path("", include("scrud_django.ui.urls")),
]
