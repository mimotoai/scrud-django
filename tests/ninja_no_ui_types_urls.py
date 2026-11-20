"""A ninja API that does not serve the UI's types (``api(ui_types=False)``),
under ``/api/``, with the UI app at ``/`` and no DRF route
(``tests.test_ui_on_ninja``): the UI finds the API's base from another
type's list."""

from django.urls import include, path

import scrud_django.ninja

API = scrud_django.ninja.api(
    types=[], urls_namespace="ninja-no-ui-types", ui_types=False
)

urlpatterns = [
    path("api/", API.urls),
    path("", include("scrud_django.ui.urls")),
]
