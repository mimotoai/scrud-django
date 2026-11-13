"""A URLconf of a ninja API alone, without the DRF routes
(`tests.test_ninja_routers`): what its routes link to must be served by it."""

from django.urls import path

import scrud_django.ninja
from scrud_django import registration

from .resource_types import PARTNER_PROFILES_TYPE

API = scrud_django.ninja.api(
    types=[registration.json_resource_type_spec(**PARTNER_PROFILES_TYPE)],
    urls_namespace="ninja-only",
)

urlpatterns = [path("api/", API.urls)]
