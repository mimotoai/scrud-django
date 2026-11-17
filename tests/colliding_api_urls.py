"""An app's own scrud API beside ``include("scrud_django.urls")``, both in
the default namespace ``scrud`` (`tests.test_default_urls`: the system
check reports the collision, scrud_django.E010)."""

from django.urls import include, path

import scrud_django.ninja

urlpatterns = [
    path("api/", include("scrud_django.urls")),
    path("mine/", scrud_django.ninja.api(types=[], ui_types=False).urls),
]
