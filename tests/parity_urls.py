"""Both front ends in one URLconf, over one database, for the parity harness
(`tests.front_ends`): the DRF routes of `tests.urls` under ``/drf/`` (not
namespaced, so their global URL names resolve there) and the test types'
ninja API (`tests.ninja_api`) under ``/ninja/``; and the DRF views' HTTP
Schema Resources at the root, where they are linked. Without DRF
(``SCRUD_TEST_NO_DRF``), the ninja API alone."""

from django.urls import include, path

from . import ninja_api
from .settings import NO_DRF

urlpatterns = [path("ninja/", ninja_api.URLS)]
if not NO_DRF:
    from scrudful.rest_framework import hsr_urls

    urlpatterns = [
        path("drf/", include("tests.urls")),
        *urlpatterns,
        path("", include(hsr_urls())),
    ]
