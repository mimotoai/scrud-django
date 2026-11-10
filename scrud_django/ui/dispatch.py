"""In-process requests to the resource views, made as the user of the
request in progress: every read and write the studio makes (`call_view`),
and every embed a component renders (`dispatch_path`), goes through the
views with that user's own rights, so that every check the API makes
applies (RBAC, branch visibility, ``check_mount``, ``base``, versions)."""

import json
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.test.client import RequestFactory
from django.urls import (
    NoReverseMatch,
    Resolver404,
    ResolverMatch,
    get_script_prefix,
    resolve,
    reverse,
)
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
    extra = _extra(request, "application/json", headers, if_match=if_match)
    body = {}
    if data is not None:
        body = {"data": json.dumps(data), "content_type": "application/json"}
    internal = RequestFactory().generic(
        method, url or request.path, secure=request.is_secure(), **body, **extra
    )
    _as_user_of(request, internal)
    if url is None:
        response = _not_found(internal)
    else:
        script = get_script_prefix()
        match = resolve("/" + url[len(script) :] if url.startswith(script) else url)
        response = match.func(internal, *match.args, **match.kwargs)
    return _rendered(response)


def view_for(request, slug: str, method: str, *, pk=None, data=None):
    """``(view, request)``: the resource viewset of type `slug` (its list
    route, or with `pk` its detail route) set up, as DRF's ``as_view`` sets
    one up, for a `method` request made in process for the user of `request`
    (as `call_view` makes one, `data` its JSON body), but not dispatched:
    nothing runs (no check, no handler, no transaction) until the caller
    asks the view itself, so that it can ask the view's own checks of a
    write without making it. `pk` must name a detail URL (no ``/``)."""
    method = method.upper()
    url = (
        reverse(f"{slug}-list") if pk is None else reverse(f"{slug}-detail", args=[pk])
    )
    extra = _extra(request, "application/json", None)
    body = {}
    if data is not None:
        body = {"data": json.dumps(data), "content_type": "application/json"}
    internal = RequestFactory().generic(
        method, url, secure=request.is_secure(), **body, **extra
    )
    _as_user_of(request, internal)
    script = get_script_prefix()
    match = resolve("/" + url[len(script) :] if url.startswith(script) else url)
    func = match.func
    view = func.cls(**func.initkwargs)
    view.action_map = dict(func.actions)
    for name, action in view.action_map.items():
        setattr(view, name, getattr(view, action))
    view.args, view.kwargs = match.args, match.kwargs
    view.request = internal
    drf_request = view.initialize_request(internal, *match.args, **match.kwargs)
    view.request = drf_request
    view.headers = view.default_response_headers
    view.format_kwarg = view.get_format_suffix(**match.kwargs)
    return view, drf_request


@dataclass(frozen=True)
class Resolved:
    """What an absolute URL path resolves to: the view's `match`, the
    `path_info` it is served at (the API's, for a path under a published
    branch's prefix), and that prefix's `mount` (``None`` at the API's own
    URLs and the project's others)."""

    match: ResolverMatch
    path_info: str
    mount: object = None


def resolve_path(path: str) -> Resolved | None:
    """What the absolute URL path `path` (no query: a path as an HTML link
    has it, under the script prefix) resolves to, as a request for it would
    be routed: a path under a published branch's prefix to the API's URL
    (`PublishedBranchMiddleware`'s `find_mount`), any other as it is.
    ``None`` when nothing in the URLconf serves it."""
    from scrud_django import ui
    from scrud_django.ui.branches.middleware import find_mount

    script = get_script_prefix()
    if not isinstance(path, str) or not path.startswith(script):
        return None
    # As the server hands it to Django: decoded.
    path_info = "/" + unquote(path[len(script) :])
    found = find_mount(path_info) if ui.is_enabled() else None
    mount = None
    if found is not None:
        mount, path_info = found
    try:
        match = resolve(path_info)
    except Resolver404:
        return None
    return Resolved(match=match, path_info=path_info, mount=mount)


def dispatch_path(
    request, path: str, *, headers: dict, query: str | None = None, resolved=None
) -> Response:
    """The rendered response of the view at `path` (an absolute URL path,
    with an optional query) to a GET made in process for the user of
    `request`, as `call_view` makes one, with ``Accept: text/html`` and
    `headers` (by name). A path under a published branch's prefix is served
    as `PublishedBranchMiddleware` serves it: at the API's URL, with the
    prefix's `Mount` (so the view's `check_mount` applies, and links stay
    in the prefix). A path nothing serves is the API's 404.

    `path` is split once, with `urllib.parse.urlsplit` (its fragment
    dropped), unless `query` is given: `path` is then the path alone, as
    the caller split it, so that what is dispatched is what the caller
    checked. `resolved` is `resolve_path`'s answer for the path, when the
    caller has it already."""
    from scrud_django.ui.branches.selection import MOUNT_ATTR

    path_only = path
    if query is None:
        parts = urlsplit(path)
        path_only, query = parts.path, parts.query
    if resolved is None:
        resolved = resolve_path(path_only)
    script = get_script_prefix()
    path_info = (
        "/" + path_only[len(script) :] if path_only.startswith(script) else path_only
    )
    internal = RequestFactory().get(
        path_info,
        secure=request.is_secure(),
        QUERY_STRING=query,
        SCRIPT_NAME=script.rstrip("/"),
        **_extra(request, "text/html", headers),
    )
    _as_user_of(request, internal)
    if resolved is None:
        return _rendered(_not_found(internal))
    if resolved.mount is not None:
        setattr(internal, MOUNT_ATTR, resolved.mount)
        internal.path_info = resolved.path_info
    match = resolved.match
    return _rendered(match.func(internal, *match.args, **match.kwargs))


def _extra(request, accept, headers, if_match=None):
    """The environ of an in-process request: `accept`, `if_match`,
    `headers` (by name) and `request`'s host."""
    extra = {"HTTP_ACCEPT": accept}
    if if_match is not None:
        extra["HTTP_IF_MATCH"] = if_match
    for name, value in (headers or {}).items():
        extra["HTTP_" + name.upper().replace("-", "_")] = value
    meta = getattr(request, "META", {})
    for key in ("HTTP_HOST", "SERVER_NAME", "SERVER_PORT"):
        if key in meta:
            extra[key] = meta[key]
    return extra


def _as_user_of(request, internal):
    """Have DRF authenticate `internal` as `request`'s user (unless
    anonymous), and skip the CSRF check its caller passed."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        internal._force_auth_user = user
    internal._dont_enforce_csrf_checks = True


def _rendered(response):
    render = getattr(response, "render", None)
    return render() if callable(render) else response


@api_view(["GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"])
@permission_classes([AllowAny])
@renderer_classes([JSONRenderer])
def _not_found(request):
    """The 404 problem of a URL the API has no route for."""
    raise NotFound()
