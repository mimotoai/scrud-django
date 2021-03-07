"""SCRUD DJANGO URL Configuration."""
import logging

from django.db.utils import OperationalError, ProgrammingError
from django.urls import path

from scrud_django import registration
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
]

try:
    registration.register_json_schema_resource_type()
    registration.register_json_ld_resource_type()
    registration.register_indexing_policy_resource_type()
    urlpatterns.extend(registration.JSON_SCHEMA_REGISTRATION_.urls)
    urlpatterns.extend(registration.JSON_LD_REGISTRATION_.urls)
    urlpatterns.extend(registration.INDEXING_POLICY_REGISTRATION_.urls)
except (OperationalError, ProgrammingError) as e:
    logging.error(e)
