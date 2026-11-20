"""The URLconf of a project staying on DRF (`tests.test_default_urls`):
``scrud_django.drf.urls`` under ``/api/``, with scrudful's HTTP Schema
Resource routes, which its views link to."""

from django.urls import include, path
from scrudful.rest_framework import hsr_urls

urlpatterns = [
    path("api/", include("scrud_django.drf.urls")),
    path("", include(hsr_urls())),
]
