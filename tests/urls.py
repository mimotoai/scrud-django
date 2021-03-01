import importlib.resources

from django.urls import include, path

from scrud_django import registration

urlpatterns = [
    path("", include("scrud_django.urls")),
]
urlpatterns.extend(
    registration.resource_types(
        registration.json_resource_type(
            resource_type_uri="tests://PartnerProfiles",
            revision="1",
            slug="partner-profiles",
            schema_func=lambda: importlib.resources.read_text(
                "tests.static.json_schema", "PartnerProfile"
            ),
            context_func=lambda: importlib.resources.read_text(
                "tests.static.json_ld", "PartnerProfile"
            ),
            indexing_policy_func=lambda: importlib.resources.read_text(
                "tests.static.json_schema", "PartnerProfileIndexing"
            ),
        ),
    )
)
