"""An upgrader's pre-0.17 URLconf (`tests.test_default_urls`): DRF
registrations of its own types beside ``include("scrud_django.urls")``,
which serves the ninja API since 0.17.0, so the bare built-in route names
(``json-schema-detail``, ``json-ld-detail``) their links reverse are not
routed (the system check ``scrud_django.E011``)."""

from django.urls import include, path
from scrudful.rest_framework import hsr_urls

from scrud_django import registration

from .resource_types import PARTNER_PROFILES_TYPE

urlpatterns = [
    path("api/", include("scrud_django.urls")),
    path(
        "api/",
        include(
            registration.resource_types(
                registration.json_resource_type(**PARTNER_PROFILES_TYPE)
            )
        ),
    ),
    path("", include(hsr_urls())),
]
