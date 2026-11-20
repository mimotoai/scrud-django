"""The UI test URLconf (``tests.mounted_urls``: the API under ``/api/``, the
UI at ``/``) with note types whose two API routes are built differently
(``tests.test_ui_routes``), each served by ``ResourceViewSet``:

- ``marked-list-notes``: its list built by registration
  (``resource_type_urls``, marked), its detail routed by hand;
- ``marked-detail-notes``: the reverse;
- ``unresolved-detail-notes``: both built by registration, its detail
  routed at a path whose converter (`KeylessConverter`) refuses every key,
  so that ``<slug>-detail`` reverses but resolves to nothing;
- ``unregistered-notes``: routed by hand, and never registered in this
  process (a test stores its type's row).

`tests.resource_types.register_mixed_types` registers the first three (their rows, which a
test's rollback removes, are registered again by the tests)."""

from django.urls import include, path, register_converter

from scrud_django import registration
from scrud_django.drf.views import ResourceViewSet

from . import mounted_urls
from .resource_types import (
    MARKED_DETAIL_SLUG,
    MARKED_LIST_SLUG,
    UNREGISTERED_SLUG,
    UNRESOLVED_DETAIL_SLUG,
    register_mixed_types,
)


class KeylessConverter:
    """A path segment that reverses but never resolves."""

    regex = "[^/]+"

    def to_python(self, value):
        raise ValueError(value)

    def to_url(self, value):
        return str(value)


register_converter(KeylessConverter, "scrud_test_keyless")


def hand_list(slug):
    return path(
        f"{slug}/",
        ResourceViewSet.as_view(
            {"get": "list", "head": "list", "post": "create"},
            resource_type_name=slug,
        ),
        name=f"{slug}-list",
    )


def hand_detail(slug):
    return path(
        f"{slug}/<str:slug>/",
        ResourceViewSet.as_view(
            {"get": "retrieve", "put": "update", "delete": "destroy"},
            resource_type_name=slug,
        ),
        name=f"{slug}-detail",
    )


def routes():
    register_mixed_types()
    marked_list, _ = registration.resource_type_urls(MARKED_LIST_SLUG, ResourceViewSet)
    _, marked_detail = registration.resource_type_urls(
        MARKED_DETAIL_SLUG, ResourceViewSet
    )
    unresolved_list, unresolved_detail = registration.resource_type_urls(
        UNRESOLVED_DETAIL_SLUG, ResourceViewSet
    )
    return [
        marked_list,
        hand_detail(MARKED_LIST_SLUG),
        hand_list(MARKED_DETAIL_SLUG),
        marked_detail,
        unresolved_list,
        path(
            f"{UNRESOLVED_DETAIL_SLUG}/<scrud_test_keyless:slug>/",
            unresolved_detail.callback,
            name=unresolved_detail.name,
        ),
        hand_list(UNREGISTERED_SLUG),
        hand_detail(UNREGISTERED_SLUG),
    ]


urlpatterns = [
    path("api/", include(routes())),
    *mounted_urls.urlpatterns,
]
