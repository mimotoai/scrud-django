"""The URLconf of the no-DRF harness (``tests/no_drf.py``): ninja's API, the
semantic routes and the UI app, which has no DRF."""

from django.urls import include, path

import scrud_django.ninja

urlpatterns = [
    path("api/", scrud_django.ninja.api(types=[]).urls),
    path("semantic/", include("scoped_rbac.semantic_urls")),
    path("", include("scrud_django.ui.urls")),
]
