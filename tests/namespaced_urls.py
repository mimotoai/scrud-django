"""The test URLconf with a second, namespaced set of routes (`ns`), as a
ninja API under its ``urls_namespace`` has: `tests/test_core_urls.py`."""

from django.http import HttpResponse
from django.urls import include, path

from . import urls


def profile(request, slug):
    return HttpResponse(slug)


ns_patterns = [
    path("partner-profiles/<slug>/", profile, name="partner-profiles-detail"),
]

urlpatterns = [
    path("", include(urls.urlpatterns)),
    path("ns/", include((ns_patterns, "ns"), namespace="ns")),
]
