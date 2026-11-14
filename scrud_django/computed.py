"""Computed, read-only resource types: a type whose content a view computes
for one key and the requesting user, rather than a stored resource.

A type registers its ``compute(request, key)``
(`scrud_django.registration.computed_resource_type(..., compute=)`), or a
subclass of `ComputedResourceViewSet` implements it;
`scrud_django.registration.computed_resource_type` registers the type and
routes ``<slug>/<key>/`` (no list route)."""

import hashlib
import json
from collections.abc import Mapping

from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.utils.encoders import JSONEncoder
from scoped_rbac.rest import RbacJSONParser

from scrud_django.core.types import type_options_for_slug
from scrud_django.mixins.view_mixins import ResourceViewMixin, ScrudfulMeta


def computed_etag(content) -> str:
    """The strong ETag of `content`: the SHA-256 of its canonical JSON."""
    canonical = json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        cls=JSONEncoder,
    )
    return '"' + hashlib.sha256(canonical.encode()).hexdigest() + '"'


def _matches(header: str, etag: str) -> bool:
    """Whether an ``If-None-Match`` header names `etag` (weak comparison) or
    is ``*``."""
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == etag:
            return True
    return False


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
        content = self.compute(request, slug)
        if not isinstance(content, Mapping):
            raise TypeError(
                f"{type(self).__name__}.compute returned a "
                f"{type(content).__name__}, not a mapping."
            )
        etag = computed_etag(content)
        headers = {"ETag": etag}
        if _matches(request.headers.get("If-None-Match", ""), etag):
            return Response(status=304, headers=headers)
        links = [
            f'<{url}>; rel="{rel}"; type="{media_type}"'
            for url, rel, media_type in (
                (
                    ScrudfulMeta.schema_link_or_func(self, request),
                    "describedby",
                    "application/schema+json",
                ),
                (
                    ScrudfulMeta.context_link_or_func(self, request),
                    "http://www.w3.org/ns/json-ld#context",
                    "application/ld+json",
                ),
            )
            if url
        ]
        if links:
            headers["Link"] = ", ".join(links)
        return Response(content, headers=headers)
