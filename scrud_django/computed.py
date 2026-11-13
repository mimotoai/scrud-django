"""Computed, read-only resource types: a type whose content a view computes
for one key and the requesting user, rather than a stored resource.

A type registers its ``compute(request, key)``
(`scrud_django.registration.computed_resource_type(..., compute=)`), or a
subclass of `ComputedResourceViewSet` implements it;
`scrud_django.registration.computed_resource_type` registers the type and
routes ``<slug>/<key>/`` (no list route)."""

from collections.abc import Mapping

from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from scoped_rbac.rest import RbacJSONParser

from scrud_django.core.computed import (  # noqa: F401 (computed_etag moved there)
    computed_answer,
    computed_etag,
    computed_link,
    not_modified,
)
from scrud_django.core.types import type_options_for_slug
from scrud_django.mixins.view_mixins import ResourceViewMixin


class ComputedResourceViewSet(ResourceViewMixin, viewsets.ViewSet):
    """A read-only view of computed content. ``compute`` may raise
    ``Http404`` or ``PermissionDenied``, and must only return what the
    request's user may read: it asks the same checks the stored resources'
    views do. Any method but GET, HEAD and OPTIONS is a 405."""

    computed = True
    http_method_names = ["get", "head", "options"]
    permission_classes = [IsAuthenticated]
    parser_classes = [RbacJSONParser]

    def compute(self, request, key: str) -> Mapping:
        """The content for `key`, a mapping (anything else is a
        ``TypeError`` in `retrieve`): by default the type's registered
        ``compute`` (``computed_resource_type(..., compute=)``,
        `scrud_django.core.types.type_options`), which a subclass may
        override."""
        compute = type_options_for_slug(self.resource_type_name).compute
        if compute is None:
            raise NotImplementedError
        return compute(request, key)

    def retrieve(self, request, slug: str) -> Response:
        # The answer is the core's (`scrud_django.core.computed`), which the
        # ninja computed route gives too.
        content, etag = computed_answer(request, None, slug, compute=self.compute)
        headers = {"ETag": etag}
        if not_modified(request, etag):
            return Response(status=304, headers=headers)
        resource_type = self.get_resource_type() if self.resource_type_name else None
        link = computed_link(request, resource_type)
        if link:
            headers["Link"] = link
        return Response(content, headers=headers)
