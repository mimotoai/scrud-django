"""The viewsets of the UI branch resource type, of the read-only UI branch
versions, and of UI pushes: their write behaviour is their hooks
(`scrud_django.ui.hooks`)."""

from scrudful.rest_framework import scrudful_viewset

from scrud_django.ui import hooks
from scrud_django.ui.views import UIContentValidationMixin
from scrud_django.views import ResourceViewSet


@scrudful_viewset
class UIBranchViewSet(UIContentValidationMixin, ResourceViewSet):
    prepare_write = staticmethod(hooks.prepare_branch)


@scrudful_viewset
class UIBranchVersionViewSet(ResourceViewSet):
    """Branch versions are recorded by the server
    (`scrud_django.ui.branches.versions`) and never changed: a POST, PUT or
    DELETE is 405. A version is read in its branch's context, and shows only
    the items its reader may read (the type's ``read_view``,
    `scrud_django.ui.hooks.read_version`): its ETag, and the list's, follow
    what the reader gets."""

    # The type's ``methods`` (`scrud_django.ui.hooks.READ_ONLY_METHODS`).
    http_method_names = sorted(method.lower() for method in hooks.READ_ONLY_METHODS)

    def get_object(self):
        version = super().get_object()
        hooks.read_version(self.request, self.get_resource_type(), version)
        return version

    def listed(self):
        """Every version the list shows (its page, else all), as its reader
        gets it."""
        page = self.list_page()
        resource_type = self.get_resource_type()
        for version in page if page is not None else self.list_queryset():
            hooks.read_version(self.request, resource_type, version)

    def list_etag(self, request):
        self.listed()
        return super().list_etag(request)

    def list(self, request):
        self.listed()
        return super().list(request)


@scrudful_viewset
class UIPushViewSet(UIContentValidationMixin, ResourceViewSet):
    """A push writes its source branch's overrides into its target branch
    (`apply_push`), or restores version ``restore`` of its target
    (`apply_restore`), in the save's transaction of the write that moves it
    into ``applied`` (a create in ``applied`` included); its ``result`` is
    then the target's new version (`scrud_django.ui.hooks.after_push_write`,
    run by the core's ``save_checked``). A
    refused push writes nothing, the push included. Workflow policies on
    the type gate the move as on any resource."""

    prepare_write = staticmethod(hooks.prepare_push)
    # An applied push is a record: it accepts neither a PUT nor a DELETE, so
    # its HTML page is a view (no form, no Delete button).
    is_record = staticmethod(hooks.applied_content)
