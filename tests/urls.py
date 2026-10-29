from django.urls import include, path

from scrud_django import registration
from scrud_django.views import ResourceViewSetGenericFilter

from .resource_types import json_resource_types

urlpatterns = [path("", include("scrud_django.urls"))]
urlpatterns.extend(registration.resource_types(*json_resource_types()))

# The partner profiles list again, served by ResourceViewSetGenericFilter, whose
# list filters on query parameters.
urlpatterns.append(
    path(
        "filtered/partner-profiles/",
        ResourceViewSetGenericFilter.as_view(
            {"get": "list", "head": "list"}, resource_type_name="partner-profiles"
        ),
        name="filtered-partner-profiles-list",
    )
)
