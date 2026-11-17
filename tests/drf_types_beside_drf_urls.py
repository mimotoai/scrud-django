"""The same DRF registrations beside ``include("scrud_django.drf.urls")``,
as an upgrader staying on DRF writes them (`tests.test_default_urls`: no
``scrud_django.E011``)."""

from django.urls import include, path
from scrudful.rest_framework import hsr_urls

from scrud_django import registration

from .resource_types import PARTNER_PROFILES_TYPE

urlpatterns = [
    path("api/", include("scrud_django.drf.urls")),
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
