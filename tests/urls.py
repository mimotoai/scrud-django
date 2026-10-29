from django.urls import include, path
from scoped_rbac.permissions import IsAuthorized

from scrud_django import registration
from scrud_django.views import ResourceViewSet, ResourceViewSetGenericFilter

from .resource_types import json_resource_types

urlpatterns = [path("", include("scrud_django.urls"))]
urlpatterns.extend(registration.resource_types(*json_resource_types()))

# The partner profiles list again, served by ResourceViewSetGenericFilter, whose
# list filters on query parameters.
urlpatterns.append(
    path(
        "filtered/partner-profiles/",
        ResourceViewSetGenericFilter.as_view(
            {"get": "list", "head": "list"}, resource_type_name="partner-profiles"
        ),
        name="filtered-partner-profiles-list",
    )
)


class ContextDenyingIsAuthorized(IsAuthorized):
    """An adopter's ``IsAuthorized`` that never allows anything in the context
    "denied", whatever the user's grants there."""

    DENIED = "denied"

    def has_context_permission(self, request, view, obj, context):
        if context == self.DENIED:
            return False
        return super().has_context_permission(request, view, obj, context)


class ContextDenyingResourceViewSet(ResourceViewSet):
    permission_classes = [ContextDenyingIsAuthorized]


# The partner profiles again, authorized by ContextDenyingIsAuthorized.
urlpatterns.extend(
    [
        path(
            "denying/partner-profiles/",
            ContextDenyingResourceViewSet.as_view(
                {"post": "create"}, resource_type_name="partner-profiles"
            ),
            name="denying-partner-profiles-list",
        ),
        path(
            "denying/partner-profiles/<slug>/",
            ContextDenyingResourceViewSet.as_view(
                {"get": "retrieve", "put": "update"},
                resource_type_name="partner-profiles",
            ),
            name="denying-partner-profiles-detail",
        ),
    ]
)


class LockedDenyingIsAuthorized(IsAuthorized):
    """An adopter's ``IsAuthorized`` that overrides ``has_object_permission``
    (DRF's object-level hook) to refuse a resource whose content has
    ``"locked": true``."""

    def has_object_permission(self, request, view, obj):
        content = getattr(obj, "content", None)
        if isinstance(content, dict) and content.get("locked") is True:
            return False
        return super().has_object_permission(request, view, obj)


class LockedDenyingResourceViewSet(ResourceViewSet):
    permission_classes = [LockedDenyingIsAuthorized]


# The JSON-LD contexts again, authorized by LockedDenyingIsAuthorized.
urlpatterns.extend(
    [
        path(
            "locking/json-ld/",
            LockedDenyingResourceViewSet.as_view(
                {"post": "create"}, resource_type_name="json-ld"
            ),
            name="locking-json-ld-list",
        ),
        path(
            "locking/json-ld/<slug>/",
            LockedDenyingResourceViewSet.as_view(
                {"get": "retrieve", "put": "update"}, resource_type_name="json-ld"
            ),
            name="locking-json-ld-detail",
        ),
    ]
)
