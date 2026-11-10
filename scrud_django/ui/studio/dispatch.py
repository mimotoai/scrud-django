"""In-process requests from the studio to the resource views: every read and
write the studio makes goes through the views, with the studio user's own
rights, so that every check the API makes applies (RBAC, branch visibility,
``base``, versions)."""

import json

from django.core.exceptions import ImproperlyConfigured
from django.test.client import RequestFactory
from django.urls import NoReverseMatch, get_script_prefix, resolve, reverse
from rest_framework.decorators import api_view, permission_classes, renderer_classes
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


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
    It accepts JSON only. It is sent to `request`'s host and scheme.

    It is exempt from CSRF checks, because the studio request that makes it
    passed them: so an unsafe `method` (not GET, HEAD or OPTIONS) is refused
    (``ImproperlyConfigured``, a programming error) when `request`'s own
    method is safe, which Django's CSRF check lets through (ruling S8).

    An anonymous user is not forced: the view answers as it answers an
    unauthenticated request. A `pk` no detail URL has (``""``, or one with
    a ``/``) is the 404 the API answers for its URL."""
    method = method.upper()
    if method not in SAFE_METHODS and request.method.upper() in SAFE_METHODS:
        raise ImproperlyConfigured(
            f"call_view: a {method} from a {request.method} request would "
            "skip the CSRF check; make it from the studio's POST."
        )
    try:
        if pk is None:
            url = reverse(f"{slug}-list")
        else:
            url = reverse(f"{slug}-detail", args=[pk])
    except NoReverseMatch:
        url = None
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
        method, url or request.path, secure=request.is_secure(), **body, **extra
    )
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        internal._force_auth_user = user
    internal._dont_enforce_csrf_checks = True
    if url is None:
        response = _not_found(internal)
    else:
        script = get_script_prefix()
        match = resolve("/" + url[len(script) :] if url.startswith(script) else url)
        response = match.func(internal, *match.args, **match.kwargs)
    render = getattr(response, "render", None)
    return render() if callable(render) else response


@api_view(["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"])
@permission_classes([AllowAny])
@renderer_classes([JSONRenderer])
def _not_found(request):
    """The 404 problem of a URL the API has no route for."""
    raise NotFound()
