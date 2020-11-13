import importlib

from scrud_django import registration

urlpatterns = registration.resource_types(
    registration.json_resource_type(
        resource_type_uri="http://api.openteams.com/json-ld/PartnerProgramApplication",
        revision="0",
        slug="partner-program-applications",
        schema_func=lambda: importlib.resources.read_text(
            "partner_applications", "partner_application_schema.json",
        ),
        context_func=lambda: importlib.resources.read_text(
            "partner_applications", "partner_application_context.json",
        ),
    ),
)
