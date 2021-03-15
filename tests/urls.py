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
            revision="2",
            slug="partner-profiles",
            schema_func=lambda: importlib.resources.read_text(
                "tests.static.partner_profile", "schema.json"
            ),
            context_func=lambda: importlib.resources.read_text(
                "tests.static.partner_profile", "ld.json"
            ),
            indexing_policy_func=lambda: importlib.resources.read_text(
                "tests.static.partner_profile", "indexing-policy.json"
            ),
        ),
    )
)
