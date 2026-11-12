from uuid import uuid4

from django.contrib import admin
from django.urls import include, path
from django.utils.timezone import now
from rest_framework import generics, routers, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from scoped_rbac import access, problems
from scoped_rbac.filters import RbacFilter
from scoped_rbac.permissions import IsAuthorized
from scoped_rbac.rest import AccessControlledAPIView, RbacJSONParser
from scrudful.rest_framework import hsr_urls, scrudful, scrudful_viewset

from scrud_django import registration
from scrud_django.filters import ResourceRbacFilter
from scrud_django.mixins.view_mixins import (
    GenericViewMixin,
    ReadableMixin,
    ResourceViewMixin,
    ScrudfulMeta,
)
from scrud_django.models import ResourceType
from scrud_django.permissions import current_transition
from scrud_django.views import ResourceViewSet, ResourceViewSetGenericFilter

from .models import Note
from .resource_types import json_resource_types

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("scrud_django.urls")),
    # Serves each scrudful view's HTTP Schema Resource, which OPTIONS links to.
    path("", include(hsr_urls())),
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


class OtherIriResourceViewSet(ResourceViewSet):
    """The partner profiles again, named by the view as another type
    (``resource_type_iri_for``), authorized by ContextDenyingIsAuthorized:
    its own ``has_context_permission`` makes the post-save check ask the
    grants beside it, for the type the view names."""

    OTHER_IRI = "tests://PartnerProfilesElsewhere"

    permission_classes = [ContextDenyingIsAuthorized]

    def resource_type_iri_for(self, request):
        return self.OTHER_IRI


class ViewCheckedIsAuthorized(IsAuthorized):
    """Refuses an object its view's ``object_for_checks`` did not mark."""

    def has_object_permission(self, request, view, obj):
        if not obj.get(ObjectForChecksResourceViewSet.MARK):
            return False
        return super().has_object_permission(request, view, obj)


class ObjectForChecksResourceViewSet(ResourceViewSet):
    """The partner profiles again, whose object checks get the content
    marked by the view's own ``object_for_checks``."""

    MARK = "checked_by_view"

    permission_classes = [ViewCheckedIsAuthorized]

    def object_for_checks(self, obj):
        return dict(super().object_for_checks(obj), **{self.MARK: True})


class PostSaveRefusingResourceViewSet(ResourceViewSet):
    """The partner profiles again, whose own post-save check refuses a
    resource saved with the name `REFUSED`."""

    REFUSED = "refused after saving"

    def check_post_save_permissions(self, request, obj):
        if obj.content.get("name") == self.REFUSED:
            raise access.AccessDenied()
        super().check_post_save_permissions(request, obj)


for prefix, viewset in (
    ("other-iri", OtherIriResourceViewSet),
    ("object-for-checks", ObjectForChecksResourceViewSet),
    ("post-save-refusing", PostSaveRefusingResourceViewSet),
):
    urlpatterns.extend(
        [
            path(
                f"{prefix}/partner-profiles/",
                viewset.as_view(
                    {"get": "list", "head": "list", "post": "create"},
                    resource_type_name="partner-profiles",
                ),
                name=f"{prefix}-partner-profiles-list",
            ),
            path(
                f"{prefix}/partner-profiles/<slug>/",
                viewset.as_view(
                    {"get": "retrieve", "put": "update", "delete": "destroy"},
                    resource_type_name="partner-profiles",
                ),
                name=f"{prefix}-partner-profiles-detail",
            ),
        ]
    )


class LockedDenyingIsAuthorized(IsAuthorized):
    """An adopter's ``IsAuthorized`` that overrides ``has_object_permission``
    (DRF's object-level hook) to refuse a resource whose content has
    ``"locked": true``: the scrud views' object checks give it the content
    (a ``dict``)."""

    def has_object_permission(self, request, view, obj):
        if obj.get("locked") is True:
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


class ReadRefusingIsAuthorized(IsAuthorized):
    """An adopter's ``IsAuthorized`` that refuses a read (GET or HEAD) of an
    object named "unreadable" (a resource whose content's ``name`` or
    ``title`` is, a `Note` whose ``text`` is), and decides every other
    request as ``IsAuthorized`` (a write of that object included)."""

    UNREADABLE = "unreadable"

    @staticmethod
    def names_of(obj):
        if isinstance(obj, Note):
            return (obj.text,)
        return (obj.get("name"), obj.get("title"))

    def has_object_permission(self, request, view, obj):
        if request.method in ("GET", "HEAD") and self.UNREADABLE in self.names_of(obj):
            return False
        return super().has_object_permission(request, view, obj)


class ReadRefusingResourceViewSet(ResourceViewSet):
    permission_classes = [ReadRefusingIsAuthorized]


class VndApiRenderer(JSONRenderer):
    """An adopter's JSON renderer with its own media type and format."""

    media_type = "application/vnd.api+json"
    format = "vnd.api+json"


class VndApiResourceViewSet(ResourceViewSet):
    renderer_classes = [VndApiRenderer]


urlpatterns.append(
    path(
        "vnd/partner-profiles/",
        VndApiResourceViewSet.as_view(
            {"get": "list", "post": "create"}, resource_type_name="partner-profiles"
        ),
        name="vnd-partner-profiles-list",
    )
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
            "read-refusing/partner-profiles/",
            ReadRefusingResourceViewSet.as_view(
                {"get": "list", "head": "list"}, resource_type_name="partner-profiles"
            ),
            name="read-refusing-partner-profiles-list",
        ),
        path(
            "read-refusing/partner-profiles/<slug>/",
            ReadRefusingResourceViewSet.as_view(
                {"get": "retrieve", "head": "retrieve", "put": "update"},
                resource_type_name="partner-profiles",
            ),
            name="read-refusing-partner-profiles-detail",
        ),
        path(
            "read-refusing/todos/<slug>/",
            ReadRefusingResourceViewSet.as_view(
                {"get": "retrieve", "put": "update"}, resource_type_name="todos"
            ),
            name="read-refusing-todos-detail",
        ),
    ]
)

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
    resource's content, as object checks give it (a ``dict``)."""

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
    itself: it never allows ``cancel`` (``current_transition()`` in the
    workflow check), whatever the user's grants."""

    def has_context_permission(self, request, view, obj, context):
        if current_transition() == "cancel":
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
class NoteViewSet(
    ReadableMixin, AccessControlledAPIView, GenericViewMixin, viewsets.GenericViewSet
):
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

    def create(self, request):
        # scrudful has evaluated the preconditions against the list, under the
        # collection's lock when they need its validators (GenericViewMixin).
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = serializer.save(etag=uuid4().hex, modified_at=now())
        return Response(self.get_serializer(note).data, status=status.HTTP_201_CREATED)

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
            NoteViewSet.as_view({"get": "list", "head": "list", "post": "create"}),
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


class NoteImportViewSet(NoteViewSet):
    """The notes again, with a POST ``@action`` of the list whose
    preconditions scrudful does not evaluate (its ``@scrudful``, which
    scrudful.E007 asks of an action, gives no list validators): it evaluates
    them itself (`list_precondition_response`), then creates."""

    @action(detail=False, methods=["post"])
    @scrudful(
        error=problems.problem_entity(), error_body_func=problems.conditional_problem
    )
    def import_note(self, request):
        failed = self.list_precondition_response(request)
        if failed is not None:
            return failed
        return self.create(request)


urlpatterns.append(
    path(
        "generic/notes-import/",
        NoteImportViewSet.as_view({"post": "import_note"}),
        name="generic-notes-import",
    )
)


# scrudful's 0.10 decorations: an @action needs @scrudful (scrudful.E007),
# which gives it no list validators; in a POST @action, scrudful evaluates
# nothing of the nested create (only the outer of two nested handlers).
ACTION = dict(
    error=problems.problem_entity(), error_body_func=problems.conditional_problem
)


class NoteUncheckedImportViewSet(NoteViewSet):
    """The notes again, with a POST ``@action`` of the list that calls
    ``create``, which scrudful skips as a nested handler, so nothing
    evaluates the preconditions: its success (201) is refused
    (ImproperlyConfigured) and rolled back for any POST carrying a
    precondition it must evaluate: any but an ``If-Match: *`` without
    ``If-None-Match``."""

    @action(detail=False, methods=["post"])
    @scrudful(**ACTION)
    def import_note(self, request):
        return self.create(request)


class NoteMappedImportViewSet(NoteViewSet):
    """The notes again, with a GET ``@action`` of the list whose POST is a
    handler of its own (``@<action>.mapping.post``), which DRF gives no
    ``detail``, and a POST ``@action`` of an item (``touch``)."""

    @action(detail=False, methods=["get"])
    @scrudful(**ACTION)
    def imports(self, request):
        return Response([])

    @imports.mapping.post
    @scrudful(**ACTION)
    def import_note(self, request):
        failed = self.list_precondition_response(request)
        if failed is not None:
            return failed
        return self.create(request)

    @action(detail=True, methods=["post"])
    @scrudful(**ACTION)
    def touch(self, request, slug):
        """A POST to an item: not the list's, whatever its preconditions."""
        return Response({"touched": slug})


routed_notes = routers.SimpleRouter()
routed_notes.register(
    "generic/routed-notes", NoteMappedImportViewSet, basename="routed-notes"
)
urlpatterns.extend(routed_notes.urls)
urlpatterns.extend(
    [
        path(
            "generic/notes-unchecked-import/",
            NoteUncheckedImportViewSet.as_view({"post": "import_note"}),
            name="generic-notes-unchecked-import",
        ),
        path(
            "generic/notes-touch/<slug>/",
            NoteMappedImportViewSet.as_view({"post": "touch"}),
            name="generic-notes-touch",
        ),
        path(
            "generic/notes-mapped-import/",
            NoteMappedImportViewSet.as_view({"get": "imports", "post": "import_note"}),
            name="generic-notes-mapped-import",
        ),
    ]
)


class ImportingResourceViewSet(ResourceViewSet):
    """Partner profiles, created by a POST ``@action`` of the list that only
    calls ``create``: ResourceViewMixin.create evaluates the preconditions."""

    @action(detail=False, methods=["post"])
    @scrudful(**ACTION)
    def import_resource(self, request):
        return self.create(request)


urlpatterns.append(
    path(
        "importing/partner-profiles/",
        ImportingResourceViewSet.as_view(
            {"post": "import_resource"}, resource_type_name="partner-profiles"
        ),
        name="importing-partner-profiles",
    )
)


class ArchivingToDoViewSet(ResourceViewSet):
    """The ToDos, with a POST ``@action`` of an item (``archive``) that
    writes the resource itself: no ``WorkflowValidator`` runs, so a grant
    conditioned on ``scrud_workflow`` must not authorize it."""

    @action(detail=True, methods=["post"])
    @scrudful(**ACTION)
    def archive(self, request, slug):
        resource = self.get_object()
        resource.content = dict(resource.content, status="done")
        resource.save()
        return Response({"archived": slug})


urlpatterns.append(
    path(
        "archiving/todos/<slug>/archive/",
        ArchivingToDoViewSet.as_view({"post": "archive"}, resource_type_name="todos"),
        name="archiving-todos-archive",
    )
)


class ReopeningToDoViewSet(ResourceViewSet):
    """The ToDos, with a PUT ``@action`` of an item (``reopen``, not the
    viewset's ``update``) that writes the resource itself: a grant
    conditioned on ``scrud_workflow`` must not authorize it either."""

    @action(detail=True, methods=["put"])
    @scrudful(**ACTION)
    def reopen(self, request, slug):
        resource = self.get_object()
        resource.content = dict(resource.content, status="not started")
        resource.save()
        return Response({"reopened": slug})


urlpatterns.append(
    path(
        "reopening/todos/<slug>/reopen/",
        ReopeningToDoViewSet.as_view({"put": "reopen"}, resource_type_name="todos"),
        name="reopening-todos-reopen",
    )
)


class ToDoReopenView(
    ReadableMixin, AccessControlledAPIView, ResourceViewMixin, generics.GenericAPIView
):
    """A PUT of an item served by a view that is not a viewset (DRF's
    ``view.action`` is ``None``), writing the resource itself: a grant
    conditioned on ``scrud_workflow`` must not authorize it."""

    permission_classes = [IsAuthorized]
    parser_classes = [RbacJSONParser]
    filter_backends = [ResourceRbacFilter]
    resource_type_name = "todos"
    resource_type_iri = "tests://ToDo"

    @scrudful(**ACTION)
    def put(self, request, slug):
        resource = self.get_object()
        resource.content = dict(resource.content, status="not started")
        resource.save()
        return Response({"reopened": slug})


urlpatterns.append(
    path(
        "todo-reopen/<slug>/",
        ToDoReopenView.as_view(),
        name="todo-reopen",
    )
)


def custom_list_etag(view_instance, request, **kwargs):
    return "custom"


def no_list_last_modified(view_instance, request, **kwargs):
    return None


# A viewset's own Meta list validators (scrudful evaluates a create's
# preconditions with them, not through create_list_validators).
@scrudful_viewset
class CustomListEtagNoteViewSet(NoteViewSet):
    class Meta(ScrudfulMeta):
        list_etag_func = custom_list_etag
        list_last_modified_func = no_list_last_modified


@scrudful_viewset
class CustomListEtagResourceViewSet(ResourceViewSet):
    class Meta(ScrudfulMeta):
        list_etag_func = custom_list_etag
        list_last_modified_func = no_list_last_modified


class NoteBulkViewSet(NoteViewSet):
    """The notes again, whose POST (``as_view({"post": "bulk"})``) is a
    handler of its own, no @action, decorated as scrudful.E007 asks an
    action: it calls ``create``, which scrudful skips as a nested handler, so
    nothing evaluates."""

    @scrudful(**ACTION)
    def bulk(self, request):
        return self.create(request)


class ResourceBulkViewSet(ResourceViewSet):
    """Partner profiles, whose POST is a handler of its own that calls
    ResourceViewMixin.create, which evaluates the preconditions."""

    @scrudful(**ACTION)
    def bulk(self, request):
        return self.create(request)


urlpatterns.extend(
    [
        path(
            "generic/custom-etag-notes/",
            CustomListEtagNoteViewSet.as_view({"get": "list", "post": "create"}),
            name="generic-custom-etag-notes-list",
        ),
        path(
            "custom-etag/partner-profiles/",
            CustomListEtagResourceViewSet.as_view(
                {"get": "list", "post": "create"}, resource_type_name="partner-profiles"
            ),
            name="custom-etag-partner-profiles-list",
        ),
        path(
            "generic/bulk-notes/",
            NoteBulkViewSet.as_view({"post": "bulk"}),
            name="generic-bulk-notes",
        ),
        path(
            "bulk/partner-profiles/",
            ResourceBulkViewSet.as_view(
                {"post": "bulk"}, resource_type_name="partner-profiles"
            ),
            name="bulk-partner-profiles",
        ),
    ]
)


class ContextDenyingNoteViewSet(NoteViewSet):
    """The notes again, authorized by ContextDenyingIsAuthorized."""

    permission_classes = [ContextDenyingIsAuthorized]


# A subclass overriding a handler is decorated again, so that scrudful
# handles the override (scrudful.E007 otherwise: no HTTP Schema Resource).
@scrudful_viewset
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


class ReadRefusingNoteViewSet(NoteViewSet):
    """The notes again, authorized by ReadRefusingIsAuthorized (a note's
    ``text`` is its name)."""

    permission_classes = [ReadRefusingIsAuthorized]


urlpatterns.extend(
    [
        path(
            "generic/read-refusing-notes/",
            ReadRefusingNoteViewSet.as_view({"get": "list", "head": "list"}),
            name="generic-read-refusing-notes-list",
        ),
        path(
            "generic/read-refusing-notes/<slug>/",
            ReadRefusingNoteViewSet.as_view(
                {"get": "retrieve", "put": "update", "delete": "destroy"}
            ),
            name="generic-read-refusing-notes-detail",
        ),
    ]
)


class OwnerIsAuthorized(IsAuthorized):
    """An adopter's "owners may do anything" shortcut: allows whatever
    ``obj`` has ``owner`` equal to the user's name, else ``IsAuthorized``.
    In the workflow check ``obj`` is the request body, so the shortcut must
    not widen a transition there."""

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
    ``PermissionDenied`` (a DRF idiom) for a resource named "raise", and
    DRF's ``NotFound`` (hiding it) for one named "hide"."""

    def has_object_permission(self, request, view, obj):
        if obj.get("name") == "raise":
            raise PermissionDenied("nope")
        if obj.get("name") == "hide":
            raise NotFound()
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


# Adopters' get_permissions overrides on scrud views, as DRF allows them.


class NoSuperResourceViewSet(ResourceViewSet):
    """A get_permissions that builds its own permissions, without super()."""

    def get_permissions(self):
        return [IsAuthorized()]


class PerActionResourceViewSet(ResourceViewSet):
    """A get_permissions with a branch per action: fresh permissions for a
    write, super()'s otherwise."""

    def get_permissions(self):
        if self.action in ("update", "destroy"):
            return [IsAuthorized(), IsAuthenticated()]
        return super().get_permissions()


class AppendingResourceViewSet(ResourceViewSet):
    """A get_permissions that calls super() and adds another IsAuthorized,
    composed with DRF's ``&``."""

    def get_permissions(self):
        return super().get_permissions() + [(IsAuthorized & IsAuthenticated)()]


# One IsAuthorized instance every request of SharedResourceViewSet reuses.
SHARED_IS_AUTHORIZED = IsAuthorized()


class SharedResourceViewSet(ResourceViewSet):
    """A get_permissions that returns the same instance on every call."""

    def get_permissions(self):
        return [SHARED_IS_AUTHORIZED]


for _prefix, _viewset in (
    ("no-super", NoSuperResourceViewSet),
    ("per-action", PerActionResourceViewSet),
    ("appending", AppendingResourceViewSet),
    ("shared", SharedResourceViewSet),
):
    urlpatterns.extend(
        [
            path(
                f"{_prefix}/partner-profiles/",
                _viewset.as_view(
                    {"get": "list", "post": "create"},
                    resource_type_name="partner-profiles",
                ),
                name=f"{_prefix}-partner-profiles-list",
            ),
            path(
                f"{_prefix}/partner-profiles/<slug>/",
                _viewset.as_view(
                    {"get": "retrieve", "put": "update", "delete": "destroy"},
                    resource_type_name="partner-profiles",
                ),
                name=f"{_prefix}-partner-profiles-detail",
            ),
        ]
    )


class DelegatingIsAuthorized(IsAuthorized):
    """An adopter's ``IsAuthorized`` whose ``has_object_permission`` asks the
    view's plain ``IsAuthorized`` permissions (from ``view.get_permissions()``)
    itself: it allows what each of their object checks allows."""

    def has_object_permission(self, request, view, obj):
        others = [p for p in view.get_permissions() if type(p) is IsAuthorized]
        return bool(others) and all(
            p.has_object_permission(request, view, obj) for p in others
        )


class DelegatingResourceViewSet(ResourceViewSet):
    permission_classes = [IsAuthorized, DelegatingIsAuthorized]


class MixedContextsResourceViewSet(ResourceViewSet):
    """Both must allow: scoped-rbac's IsAuthorized in the resource's contexts
    and TeamIsAuthorized in its team."""

    permission_classes = [IsAuthorized, TeamIsAuthorized]


for _prefix, _viewset in (
    ("delegating", DelegatingResourceViewSet),
    ("mixed-contexts", MixedContextsResourceViewSet),
):
    urlpatterns.extend(
        [
            path(
                f"{_prefix}/partner-profiles/",
                _viewset.as_view(
                    {"get": "list"}, resource_type_name="partner-profiles"
                ),
                name=f"{_prefix}-partner-profiles-list",
            ),
            path(
                f"{_prefix}/partner-profiles/<slug>/",
                _viewset.as_view(
                    {"get": "retrieve"}, resource_type_name="partner-profiles"
                ),
                name=f"{_prefix}-partner-profiles-detail",
            ),
        ]
    )


class ArchivedRefusingIsAuthorized(IsAuthorized):
    """An adopter's ``IsAuthorized`` that refuses, in every context, a
    resource also stored in the context "archived": its
    ``has_context_permission`` reads the resource's contexts from the content
    it gets (the content as stored)."""

    ARCHIVED = "archived"

    def has_context_permission(self, request, view, obj, context):
        from scrud_django.mixins.resource_mixin import rbac_contexts_of

        if self.ARCHIVED in rbac_contexts_of(obj):
            return False
        return super().has_context_permission(request, view, obj, context)


class ArchivedRefusingResourceViewSet(ResourceViewSet):
    permission_classes = [ArchivedRefusingIsAuthorized]


urlpatterns.extend(
    [
        path(
            "archived-refusing/json-ld/",
            ArchivedRefusingResourceViewSet.as_view(
                {"get": "list"}, resource_type_name="json-ld"
            ),
            name="archived-refusing-json-ld-list",
        ),
        path(
            "archived-refusing/json-ld/<slug>/",
            ArchivedRefusingResourceViewSet.as_view(
                {"get": "retrieve", "put": "update", "delete": "destroy"},
                resource_type_name="json-ld",
            ),
            name="archived-refusing-json-ld-detail",
        ),
    ]
)


class ContextDenyingJsonLdViewSet(ResourceViewSet):
    """JSON-LD resources authorized by `ContextDenyingIsAuthorized`, the
    "refuse one context, else super()" shape of an adopter's
    ``has_context_permission``."""

    permission_classes = [ContextDenyingIsAuthorized]


urlpatterns.extend(
    [
        path(
            "denying/json-ld/",
            ContextDenyingJsonLdViewSet.as_view(
                {"get": "list", "post": "create"}, resource_type_name="json-ld"
            ),
            name="denying-json-ld-list",
        ),
        path(
            "denying/json-ld/<slug>/",
            ContextDenyingJsonLdViewSet.as_view(
                {"get": "retrieve", "put": "update", "delete": "destroy"},
                resource_type_name="json-ld",
            ),
            name="denying-json-ld-detail",
        ),
    ]
)
