"""SCRUD DJANGO URL Configuration."""

from django.apps import apps
from django.urls import include, path

from scrud_django import registration, ui
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

if ui.is_enabled():
    from scrud_django.ui.branches.css import CSS_FILE_URL_NAME, UICssFileView
    from scrud_django.ui.branches.js import JS_FILE_URL_NAME, UIJsFileView
    from scrud_django.ui.types import ui_resource_type_urls

    urlpatterns.extend(ui_resource_type_urls())
    urlpatterns.append(
        path(
            "ui-css-files/<str:name>/<str:etag>.css",
            UICssFileView.as_view(),
            name=CSS_FILE_URL_NAME,
        )
    )
    urlpatterns.append(
        path(
            "ui-js-files/<str:name>/<str:etag>.js",
            UIJsFileView.as_view(),
            name=JS_FILE_URL_NAME,
        )
    )
    if apps.is_installed("scrud_django.ui.studio"):
        # The studio's data, as computed resources (spec E2).
        from scrud_django.ui.studio.computed import studio_computed_type_urls

        urlpatterns.extend(studio_computed_type_urls())

registration.register_role_services()
