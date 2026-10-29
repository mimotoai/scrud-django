from uuid import uuid4

from django.contrib import admin
from django.urls import include, path
from django.utils.timezone import now
from rest_framework import serializers, status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from scoped_rbac import access
from scoped_rbac.filters import RbacFilter
from scoped_rbac.permissions import IsAuthorized
from scoped_rbac.rest import AccessControlledAPIView, RbacJSONParser
from scrudful.rest_framework import scrudful_viewset

from scrud_django import registration
from scrud_django.mixins.view_mixins import GenericViewMixin
from scrud_django.models import ResourceType
from scrud_django.permissions import TransitionCheck
from scrud_django.views import ResourceViewSet, ResourceViewSetGenericFilter

from .models import Note
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
                {"get": "list", "head": "list", "post": "create"},
                resource_type_name="partner-profiles",
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
                {"get": "list", "head": "list", "post": "create"},
                resource_type_name="json-ld",
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
                {"get": "list", "post": "create"}, resource_type_name="partner-profiles"
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


class NoCancelIsAuthorized(IsAuthorized):
    """An adopter's ``IsAuthorized`` that decides by the workflow transition
    itself: it never allows ``cancel`` (the ``TransitionCheck`` the workflow
    check gives it), whatever the user's grants."""

    def has_context_permission(self, request, view, obj, context):
        if isinstance(obj, TransitionCheck) and obj.transition == "cancel":
            return False
        return super().has_context_permission(request, view, obj, context)


class NoCancelResourceViewSet(ResourceViewSet):
    permission_classes = [NoCancelIsAuthorized]


class ContextDenyingToDoViewSet(ResourceViewSet):
    permission_classes = [ContextDenyingIsAuthorized]


# The ToDos again, authorized by NoCancelIsAuthorized and by
# ContextDenyingIsAuthorized.
urlpatterns.extend(
    [
        path(
            "no-cancel/todos/<slug>/",
            NoCancelResourceViewSet.as_view(
                {"put": "update"}, resource_type_name="todos"
            ),
            name="no-cancel-todos-detail",
        ),
        path(
            "denying/todos/",
            ContextDenyingToDoViewSet.as_view(
                {"post": "create"}, resource_type_name="todos"
            ),
            name="denying-todos-list",
        ),
    ]
)


NOTE = "tests://Note"


class NoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Note
        fields = ["id", "text", "rbac_context"]

    def __init__(self, *args, **kwargs):
        kwargs.pop("resource_type", None)  # GenericViewMixin passes it
        super().__init__(*args, **kwargs)


@scrudful_viewset
class NoteViewSet(AccessControlledAPIView, GenericViewMixin, viewsets.GenericViewSet):
    """An adopter's view of a model of their own (`Note`) on GenericViewMixin."""

    queryset = Note.objects.all()
    serializer_class = NoteSerializer
    permission_classes = [IsAuthorized]
    parser_classes = [RbacJSONParser]
    filter_backends = [RbacFilter]
    resource_type_iri = NOTE

    def get_resource_type(self):
        resource_type, _ = ResourceType.objects.get_or_create(
            type_uri=NOTE,
            defaults={
                "slug": "notes",
                "revision": "1",
                "etag": uuid4().hex,
                "modified_at": now(),
            },
        )
        return resource_type

    def list(self, request):
        queryset = self.filter_queryset(self.get_queryset())
        return Response(self.get_serializer(queryset, many=True).data)

    def retrieve(self, request, slug: str):
        return Response(self.get_serializer(self.get_object()).data)

    def update(self, request, slug: str):
        note = self.get_object()
        note.text = request.data.get("text", "")
        note.etag = uuid4().hex
        note.modified_at = now()
        note.save()
        return Response(self.get_serializer(note).data)

    def destroy(self, request, slug: str):
        self.get_object().delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


urlpatterns.extend(
    [
        path(
            "generic/notes/",
            NoteViewSet.as_view({"get": "list", "head": "list"}),
            name="generic-notes-list",
        ),
        path(
            "generic/notes/<slug>/",
            NoteViewSet.as_view(
                {"get": "retrieve", "put": "update", "delete": "destroy"}
            ),
            name="generic-notes-detail",
        ),
    ]
)


class ContextDenyingNoteViewSet(NoteViewSet):
    """The notes again, authorized by ContextDenyingIsAuthorized."""

    permission_classes = [ContextDenyingIsAuthorized]


class PagedContextDenyingNoteViewSet(ContextDenyingNoteViewSet):
    """The notes again, authorized by ContextDenyingIsAuthorized, listed from
    `list_queryset` (as a GenericViewMixin list should serve it)."""

    def list(self, request):
        return Response(self.get_serializer(self.list_queryset(), many=True).data)


urlpatterns.extend(
    [
        path(
            "generic/denying-notes/",
            ContextDenyingNoteViewSet.as_view({"get": "list", "head": "list"}),
            name="generic-denying-notes-list",
        ),
        path(
            "generic/denying-notes/<slug>/",
            ContextDenyingNoteViewSet.as_view({"get": "retrieve"}),
            name="generic-denying-notes-detail",
        ),
        path(
            "generic/denying-notes-from-list-queryset/",
            PagedContextDenyingNoteViewSet.as_view({"get": "list", "head": "list"}),
            name="generic-denying-notes-from-list-queryset",
        ),
    ]
)


class OwnerIsAuthorized(IsAuthorized):
    """An adopter's "owners may do anything" shortcut: allows whatever
    ``obj`` has ``owner`` equal to the user's name, else ``IsAuthorized``.
    In the workflow check ``obj`` is a ``TransitionCheck`` over the request
    body, so the shortcut must not widen a transition there."""

    def has_context_permission(self, request, view, obj, context):
        if hasattr(obj, "get") and obj.get("owner") == request.user.username:
            return True
        return super().has_context_permission(request, view, obj, context)


class OwnerResourceViewSet(ResourceViewSet):
    permission_classes = [OwnerIsAuthorized]


urlpatterns.append(
    path(
        "owner/todos/<slug>/",
        OwnerResourceViewSet.as_view({"put": "update"}, resource_type_name="todos"),
        name="owner-todos-detail",
    )
)


class BodyContextsIsAuthorized(IsAuthorized):
    """An adopter's ``IsAuthorized`` whose ``contexts_from`` delegates to
    scoped-rbac's own building block, ``access.body_contexts``."""

    def contexts_from(self, view, content):
        return access.body_contexts(content)


class BodyContextsResourceViewSet(ResourceViewSet):
    permission_classes = [BodyContextsIsAuthorized]


class RaisingIsAuthorized(IsAuthorized):
    """An adopter's ``IsAuthorized`` whose ``has_object_permission`` raises
    ``PermissionDenied`` (a DRF idiom) for a resource named "raise"."""

    def has_object_permission(self, request, view, obj):
        if obj.get("name") == "raise":
            raise PermissionDenied("nope")
        return super().has_object_permission(request, view, obj)


class RaisingResourceViewSet(ResourceViewSet):
    permission_classes = [RaisingIsAuthorized]


urlpatterns.extend(
    [
        path(
            "body-contexts/json-ld/",
            BodyContextsResourceViewSet.as_view(
                {"post": "create"}, resource_type_name="json-ld"
            ),
            name="body-contexts-json-ld-list",
        ),
        path(
            "raising/partner-profiles/",
            RaisingResourceViewSet.as_view(
                {"get": "list"}, resource_type_name="partner-profiles"
            ),
            name="raising-partner-profiles-list",
        ),
        path(
            "raising/partner-profiles/<slug>/",
            RaisingResourceViewSet.as_view(
                {"get": "retrieve"}, resource_type_name="partner-profiles"
            ),
            name="raising-partner-profiles-detail",
        ),
    ]
)
