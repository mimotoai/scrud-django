"""An app's own scrud API beside ``include("scrud_django.urls")``, in a
namespace of its own (`tests.test_default_urls`: no collision)."""

from django.urls import include, path

import scrud_django.ninja

urlpatterns = [
    path("api/", include("scrud_django.urls")),
    path(
        "mine/",
        scrud_django.ninja.api(types=[], urls_namespace="mine", ui_types=False).urls,
    ),
]
