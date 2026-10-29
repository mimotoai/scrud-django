from django.contrib import admin
from django.urls import include, path
from scoped_rbac.permissions import IsAuthorized

from scrud_django import registration
from scrud_django.views import ResourceViewSet, ResourceViewSetGenericFilter

from .resource_types import json_resource_types

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("scrud_django.urls")),
]
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


class RecordingIsAuthorized(IsAuthorized):
    """An adopter's ``IsAuthorized`` that records the ``obj`` each of its
    ``has_object_permission`` and ``has_context_permission`` calls gets, in
    `SEEN`, and decides as ``IsAuthorized``."""

    SEEN = []

    def has_object_permission(self, request, view, obj):
        self.SEEN.append(("has_object_permission", obj))
        return super().has_object_permission(request, view, obj)

    def has_context_permission(self, request, view, obj, context):
        self.SEEN.append(("has_context_permission", obj))
        return super().has_context_permission(request, view, obj, context)


class RecordingResourceViewSet(ResourceViewSet):
    permission_classes = [RecordingIsAuthorized]


# The partner profiles again, authorized by RecordingIsAuthorized.
urlpatterns.extend(
    [
        path(
            "recording/partner-profiles/",
            RecordingResourceViewSet.as_view(
                {"post": "create"}, resource_type_name="partner-profiles"
            ),
            name="recording-partner-profiles-list",
        ),
        path(
            "recording/partner-profiles/<slug>/",
            RecordingResourceViewSet.as_view(
                {"get": "retrieve", "put": "update"},
                resource_type_name="partner-profiles",
            ),
            name="recording-partner-profiles-detail",
        ),
    ]
)


class TeamIsAuthorized(IsAuthorized):
    """An adopter's ``IsAuthorized`` whose ``contexts_for`` is a resource's
    ``team`` (``""`` without one), not its ``rbac_context``: it reads the
    resource as a mapping, as object checks give it (a ``ResourceCheck``)."""

    def contexts_for(self, obj):
        return [obj.get("team", "")]


class TeamResourceViewSet(ResourceViewSet):
    permission_classes = [TeamIsAuthorized]


# The partner profiles again, authorized by team (TeamIsAuthorized).
urlpatterns.extend(
    [
        path(
            "teams/partner-profiles/",
            TeamResourceViewSet.as_view(
                {"get": "list"}, resource_type_name="partner-profiles"
            ),
            name="teams-partner-profiles-list",
        ),
        path(
            "teams/partner-profiles/<slug>/",
            TeamResourceViewSet.as_view(
                {"get": "retrieve"}, resource_type_name="partner-profiles"
            ),
            name="teams-partner-profiles-detail",
        ),
    ]
)
