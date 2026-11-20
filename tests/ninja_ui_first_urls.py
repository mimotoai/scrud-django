"""A URLconf that includes the UI app before the test types' ninja API
(`tests.ninja_api`), both at ``/``: the UI's ``<slug>/`` and ``<slug>/<pk>/``
patterns come first at the API's own URLs (``tests.test_ui_routes``)."""

from django.urls import include, path

from . import ninja_api

urlpatterns = [
    path("", include("scrud_django.ui.urls")),
    path("", ninja_api.URLS),
]
