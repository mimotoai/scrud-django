import importlib.resources

from articles.api import router as articles_router
from django.contrib import admin
from django.urls import include, path

import scrud_django.ninja
from scrud_django import registration

app_name = "scrud_django-demo"


def packaged(name):
    """A function reading the demo package's file `name`, as a type's schema or
    context hook."""
    return lambda: importlib.resources.files("demo").joinpath(name).read_text()


# One ninja API at /api/: the two stored types, the UI app's and the studio's
# types, and (added before its URL patterns are read, which happens once) an
# app's own model with its workflow.
api = scrud_django.ninja.api(
    types=[
        registration.json_resource_type_spec(
            resource_type_uri="tests://PartnerProfiles",
            revision="1",
            slug="partner-profiles",
            schema_func=packaged("PartnerProfile-schema.json"),
            context_func=packaged("PartnerProfile-ld.json"),
        ),
        registration.json_resource_type_spec(
            resource_type_uri="http://example.org/todo#TodoList",
            revision="1",
            slug="todo-lists",
            schema_func=packaged("TodoList-schema.json"),
            context_func=packaged("TodoList-ld.json"),
        ),
    ]
)
api.add_router("articles/", articles_router)

urlpatterns = [
    path("api/", api.urls),
    # Django's own login, logout and password views (a session login for the
    # UI, the studio and the API).
    path("accounts/", include("django.contrib.auth.urls")),
    path("admin/", admin.site.urls),
    # The schema and context links of the API name types by IRI.
    path("semantic/", include("scoped_rbac.semantic_urls")),
    # The UI app (its pages at /<slug>/, the studio at /studio/), last: it
    # claims only the slugs it serves.
    path("", include("scrud_django.ui.urls")),
]
