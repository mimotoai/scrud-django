"""In-process requests from the studio to the resource views: every read and
write the studio makes goes through the views, with the studio user's own
rights, so that every check the API makes applies (RBAC, branch visibility,
``base``, versions)."""

import json

from django.test.client import RequestFactory
from django.urls import get_script_prefix, resolve, reverse
from rest_framework.response import Response


def call_view(
    request,
    slug: str,
    method: str,
    *,
    pk=None,
    data=None,
    if_match=None,
    headers=None,
) -> Response:
    """The rendered response of the resource view of type `slug` (its list
    route, or with `pk` its detail route) to a `method` request made in
    process for the user of `request` (DRF authenticates it with
    ``ForcedAuthentication``, from ``_force_auth_user``). `data`, unless
    ``None``, is sent as the JSON body; `if_match` is the ``If-Match``
    header as sent (an ETag in quotes); `headers` are more headers by name.
    It accepts JSON only. It is exempt from CSRF checks: the studio request
    that makes it passed them. It is sent to `request`'s host and scheme."""
    if pk is None:
        url = reverse(f"{slug}-list")
    else:
        url = reverse(f"{slug}-detail", args=[pk])
    extra = {"HTTP_ACCEPT": "application/json"}
    if if_match is not None:
        extra["HTTP_IF_MATCH"] = if_match
    for name, value in (headers or {}).items():
        extra["HTTP_" + name.upper().replace("-", "_")] = value
    meta = getattr(request, "META", {})
    for key in ("HTTP_HOST", "SERVER_NAME", "SERVER_PORT"):
        if key in meta:
            extra[key] = meta[key]
    body = {}
    if data is not None:
        body = {"data": json.dumps(data), "content_type": "application/json"}
    internal = RequestFactory().generic(
        method.upper(), url, secure=request.is_secure(), **body, **extra
    )
    internal._force_auth_user = request.user
    internal._dont_enforce_csrf_checks = True
    script = get_script_prefix()
    match = resolve("/" + url[len(script) :] if url.startswith(script) else url)
    response = match.func(internal, *match.args, **match.kwargs)
    return response.render()
