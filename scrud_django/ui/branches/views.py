"""The viewsets of the UI branch resource type, of the read-only UI branch
versions, and of UI pushes: their write behaviour is their hooks
(`scrud_django.ui.hooks`)."""

import hashlib
from collections.abc import Mapping

from django.db import transaction
from scrudful.rest_framework import scrudful_viewset

from scrud_django.permissions import may_read_in_context, resource_contexts
from scrud_django.ui import hooks
from scrud_django.ui.branches import ITEM_KINDS
from scrud_django.ui.views import UIContentValidationMixin
from scrud_django.views import ResourceViewSet


@scrudful_viewset
class UIBranchViewSet(UIContentValidationMixin, ResourceViewSet):
    prepare_write = staticmethod(hooks.prepare_branch)


def readable_version(request, version):
    """`version` (a version `Resource`, read for `request`) as `request` may
    see it, in memory only: its ``items`` without those the requester may
    not GET live (``http.GET`` on the item's type in the item's contexts,
    as the list filter checks a stored item; ruling R9), and ``etags``
    without theirs. When any is left out, its ``etag`` becomes one for what
    is left (the stored ETag and the positions left out), so that two
    requesters seeing different items get different ETags. Done once per
    instance."""
    if getattr(version, "_scrud_readable", False):
        return version
    version._scrud_readable = True
    content = version.content
    items = content.get("items") if isinstance(content, Mapping) else None
    if not isinstance(items, Mapping):
        return version
    etags = content.get("etags")
    etags = etags if isinstance(etags, Mapping) else None
    shown, shown_etags, hidden = {}, {}, []
    for kind, listed in items.items():
        type_uri = ITEM_KINDS.get(kind)
        tags = etags.get(kind) if etags is not None else None
        tags = tags if isinstance(tags, list) else []
        shown[kind], shown_etags[kind] = [], []
        for index, item in enumerate(listed if isinstance(listed, list) else []):
            if (
                type_uri is not None
                and isinstance(item, Mapping)
                and may_read_in_context(
                    request, type_uri, item, resource_contexts(item)
                )
            ):
                shown[kind].append(item)
                shown_etags[kind].append(tags[index] if index < len(tags) else None)
            else:
                hidden.append(f"{kind}:{index}")
    if hidden:
        digest = hashlib.sha256("\n".join(hidden).encode("utf-8")).hexdigest()
        version.content = {**content, "items": shown}
        if etags is not None:
            version.content["etags"] = shown_etags
        version.etag = f"{version.etag}-{digest[:16]}"
    return version


@scrudful_viewset
class UIBranchVersionViewSet(ResourceViewSet):
    """Branch versions are recorded by the server
    (`scrud_django.ui.branches.versions`) and never changed: a POST, PUT or
    DELETE is 405. A version is read in its branch's context, and shows only
    the items its reader may read (`readable_version`): its ETag, and the
    list's, follow what the reader gets."""

    # The type's ``methods`` (`scrud_django.ui.hooks.READ_ONLY_METHODS`).
    http_method_names = sorted(method.lower() for method in hooks.READ_ONLY_METHODS)

    def get_object(self):
        return readable_version(self.request, super().get_object())

    def listed(self):
        """Every version the list shows (its page, else all), readable."""
        page = self.list_page()
        for version in page if page is not None else self.list_queryset():
            readable_version(self.request, version)

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
    (`apply_restore`), in the transaction of the write that moves it into
    ``applied`` (a create in ``applied`` included); its ``result`` is then
    the target's new version (`scrud_django.ui.hooks.after_push_write`). A
    refused push writes nothing, the push included. Workflow policies on
    the type gate the move as on any resource."""

    prepare_write = staticmethod(hooks.prepare_push)
    # An applied push is a record: it accepts neither a PUT nor a DELETE, so
    # its HTML page is a view (no form, no Delete button).
    is_record = staticmethod(hooks.applied_content)

    def create_resource(self, request):
        """Create the push and, created in ``applied``, apply it, in one
        transaction (a POST runs in none of its own)."""
        with transaction.atomic(using=self.write_database()):
            return super().create_resource(request)

    def update(self, request, slug: str):
        """Update the push and, moved into ``applied``, apply it, in the
        request's transaction."""
        with transaction.atomic(using=self.write_database()):
            return super().update(request, slug)
