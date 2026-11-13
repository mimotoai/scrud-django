"""The test types' ninja API (`tests.ninja_api`) at the root, as `tests.urls`
mounts the DRF routes, for the generic suites run on both front ends (the
``front_end`` fixture of `tests.conftest`). The API's URL patterns are those
`tests.ninja_api` read once (ninja refuses a second read of a namespace).
Also the DRF views' HTTP Schema Resources, and scoped-rbac's semantic
documents, which the role routes' Links name."""

from django.urls import include, path
from scrudful.rest_framework import hsr_urls

from . import ninja_api

urlpatterns = [
    path("", ninja_api.URLS),
    path("", include(hsr_urls())),
    path("semantic/", include("scoped_rbac.semantic_urls")),
]
