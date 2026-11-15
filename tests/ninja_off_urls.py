"""`tests.parity_urls` with the test types' ninja API built while
``SCOPED_RBAC["UNIFORM_ERRORS"]`` is off, as a project configured with them
off builds it: the DRF routes of `tests.urls` under ``/drf/``, that API under
``/ninja/`` (the harness's mounts, `tests.front_ends.MOUNTS`), and the DRF
views' HTTP Schema Resources at the root. The API has a URL namespace of its
own (`NAMESPACE`): ninja registers an API's namespace when its URL patterns
are read, and refuses a second read for the same one (`tests.ninja_api`'s)."""

from django.conf import settings
from django.test import override_settings
from django.urls import include, path
from scrudful.rest_framework import hsr_urls

import scrud_django.ninja

from .resource_types import ninja_type_specs

NAMESPACE = "scrud-ninja-off"

with override_settings(SCOPED_RBAC={**settings.SCOPED_RBAC, "UNIFORM_ERRORS": False}):
    API = scrud_django.ninja.api(types=ninja_type_specs(), urls_namespace=NAMESPACE)
    URLS = API.urls

urlpatterns = [
    path("drf/", include("tests.urls")),
    path("ninja/", URLS),
    path("", include(hsr_urls())),
]
