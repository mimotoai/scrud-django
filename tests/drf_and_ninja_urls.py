"""The DRF routes (`tests.urls`, outside any namespace) beside a ninja API
serving a type of its own that DRF does not route (``ninja-only-things``,
namespace ``ninja-beside-drf``, under ``/ninja/``), and the UI app last
(``tests.test_core_urls``: a link follows the API serving the request, DRF's
included)."""

from django.urls import include, path

import scrud_django.ninja
from scrud_django import registration

API = scrud_django.ninja.api(
    types=[
        registration.json_resource_type_spec(
            "tests://NinjaOnlyThing", "ninja-only-things"
        )
    ],
    urls_namespace="ninja-beside-drf",
    ui_types=False,
)

urlpatterns = [
    path("", include("tests.urls")),
    path("ninja/", API.urls),
    path("", include("scrud_django.ui.urls")),
]
