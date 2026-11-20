"""The test types' ninja API (`tests.ninja_api`, in its URL namespace) under
``/api/`` with the UI app at ``/``, and no DRF routes: a ninja-only project
that includes the UI (``tests.test_ui_routes``)."""

from django.urls import include, path

from . import ninja_api

urlpatterns = [
    path("api/", ninja_api.URLS),
    path("", include("scrud_django.ui.urls")),
]
