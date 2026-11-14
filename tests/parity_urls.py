"""Both front ends in one URLconf, over one database, for the parity harness
(`tests.front_ends`): the DRF routes of `tests.urls` under ``/drf/`` (not
namespaced, so their global URL names resolve there) and the test types'
ninja API (`tests.ninja_api`) under ``/ninja/``; and the DRF views' HTTP
Schema Resources at the root, where they are linked."""

from django.urls import include, path
from scrudful.rest_framework import hsr_urls

from . import ninja_api

urlpatterns = [
    path("drf/", include("tests.urls")),
    path("ninja/", ninja_api.URLS),
    path("", include(hsr_urls())),
]
