import importlib.resources

from articles.api import router as articles_router
from django.contrib import admin
from django.urls import include, path
from scoped_rbac.rest import UserViewSet
from scoped_rbac.urls import OpenRouter
from scrudful.rest_framework import hsr_urls

import scrud_django.ninja
from scrud_django import registration

app_name = "scrud_django-demo"

router = OpenRouter()
router.register(r"users", UserViewSet)

api_urlpatterns = [
    path("", include(router.urls), name="api"),
    path("", include("scrud_django.urls")),
]
api_urlpatterns.extend(
    registration.resource_types(
        registration.json_resource_type(
            resource_type_uri="tests://PartnerProfiles",
            revision="1",
            slug="partner-profiles",
            schema_func=lambda: (
                importlib.resources.files("demo")
                .joinpath("PartnerProfile-schema.json")
                .read_text()
            ),
            context_func=lambda: (
                importlib.resources.files("demo")
                .joinpath("PartnerProfile-ld.json")
                .read_text()
            ),
        ),
        registration.json_resource_type(
            resource_type_uri="http://example.org/todo#TodoList",
            revision="1",
            slug="todo-lists",
            schema_func=lambda: (
                importlib.resources.files("demo")
                .joinpath("TodoList-schema.json")
                .read_text()
            ),
            context_func=lambda: (
                importlib.resources.files("demo")
                .joinpath("TodoList-ld.json")
                .read_text()
            ),
        ),
    )
)

# An app's own model through ninja, with its workflow: the router is added
# before the API's URL patterns are read, which happens once.
ninja_api = scrud_django.ninja.api(types=[], urls_namespace="demo-ninja")
ninja_api.add_router("articles/", articles_router)

urlpatterns = [
    path("api/", include(api_urlpatterns)),
    # Log in to the browsable API (its "Log in" link) and to the admin.
    path("api-auth/", include("rest_framework.urls")),
    path("admin/", admin.site.urls),
    path("ninja/", ninja_api.urls),
    # The schema and context links of the ninja API name types by IRI.
    path("semantic/", include("scoped_rbac.semantic_urls")),
    # Serves each scrudful view's HTTP Schema Resource (OPTIONS links to it);
    # once, in the root URLconf.
    path("", include(hsr_urls())),
    # The UI app (its pages at /<slug>/, the studio at /studio/), last: it
    # claims only the slugs it serves.
    path("", include("scrud_django.ui.urls")),
]
