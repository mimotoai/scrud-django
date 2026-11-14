"""A URLconf of a ninja API alone, without the DRF routes
(`tests.test_ninja_routers`): what its routes link to must be served by it,
or by scoped-rbac's semantic documents (its role routes' schemas and
contexts), which a project includes once, whichever front ends it serves."""

from django.urls import include, path

import scrud_django.ninja
from scrud_django import registration

from .resource_types import PARTNER_PROFILES_TYPE, SUMMARIES_TYPE

API = scrud_django.ninja.api(
    types=[
        registration.json_resource_type_spec(**PARTNER_PROFILES_TYPE),
        registration.computed_resource_type_spec(**SUMMARIES_TYPE),
    ],
    urls_namespace="ninja-only",
)

urlpatterns = [
    path("api/", API.urls),
    path("semantic/", include("scoped_rbac.semantic_urls")),
]
