import importlib.resources

from django.urls import include, path

from scrud_django import registration

app_name = "scrud_django-demo"

api_urlpatterns = [
    path("", include("scrud_django.urls")),
]
api_urlpatterns.extend(
    registration.resource_types(
        registration.json_resource_type(
            resource_type_uri="tests://PartnerProfiles",
            revision="1",
            slug="partner-profiles",
            schema_func=lambda: importlib.resources.read_text(
                "demo", "PartnerProfile-schema.json"
            ),
            context_func=lambda: importlib.resources.read_text(
                "demo", "PartnerProfile-ld.json"
            ),
        ),
    )
)

urlpatterns = [
    path("api/", include(api_urlpatterns)),
]
