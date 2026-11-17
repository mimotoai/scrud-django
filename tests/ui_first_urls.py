"""A URLconf that includes the UI app before the API, both at ``/``
(``tests.urls``' routes): the UI's ``<slug>/`` and ``<slug>/<pk>/`` patterns
come first at the API's own URLs (``tests.test_ui_routes``)."""

from django.urls import include, path

from . import urls

urlpatterns = [
    path("", include("scrud_django.ui.urls")),
    path("", include(urls.urlpatterns)),
]
