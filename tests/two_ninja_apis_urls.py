"""Two ninja APIs in one URLconf, each serving a type of its own:
``partner-profiles`` under ``/a/`` (namespace ``ninja-a``), ``todos``
under ``/b/`` (``ninja-b``), neither the UI's types, and the UI app at
``/`` (``tests.test_core_urls``: links follow the API serving the
request)."""

from django.urls import include, path

import scrud_django.ninja
from scrud_django import registration

from .resource_types import PARTNER_PROFILES_TYPE, TODOS_TYPE

API_A = scrud_django.ninja.api(
    types=[registration.json_resource_type_spec(**PARTNER_PROFILES_TYPE)],
    urls_namespace="ninja-a",
    ui_types=False,
)
API_B = scrud_django.ninja.api(
    types=[registration.json_resource_type_spec(**TODOS_TYPE)],
    urls_namespace="ninja-b",
    ui_types=False,
)

urlpatterns = [
    path("a/", API_A.urls),
    path("b/", API_B.urls),
    path("", include("scrud_django.ui.urls")),
]
