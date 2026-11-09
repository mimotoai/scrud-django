"""SCRUD DJANGO URL Configuration."""

from django.urls import include, path
from scoped_rbac.models import Role, RoleAssignment

from scrud_django import collection_type_uri_for, registration, services
from scrud_django.views import (
    ResourceCollectionContextView,
    ResourceCollectionSchemaView,
    get_service_list,
)

urlpatterns = [
    path("services/", get_service_list, name="services"),
    path(
        "collections-json-schema/<str:slug>/",
        ResourceCollectionSchemaView.as_view(),
        name="collections-json-schema",
    ),
    path(
        "collections-json-ld/<str:slug>/",
        ResourceCollectionContextView.as_view(),
        name="collections-json-ld",
    ),
    path("", include("scoped_rbac.urls")),
    path("semantic/", include("scoped_rbac.semantic_urls")),
]

# Served even when the database cannot register the types now (at `migrate`,
# or while it is unreachable): each is registered when it is first read
# (`registration.register_or_defer`).
urlpatterns.extend(registration.built_in_resource_type_urls())

services.add_service(collection_type_uri_for(Role.resource_type.iri), "role")
services.add_service(
    collection_type_uri_for(RoleAssignment.resource_type.iri), "roleassignment"
)
