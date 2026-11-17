"""In-process requests to the resource views, made as the user of the
request in progress: every embed a component renders (`dispatch_path`) goes
through the views with that user's own rights, so that every check the API
makes applies (RBAC, branch visibility, ``check_mount``, ``base``,
versions)."""

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from django.test.client import RequestFactory
from django.urls import (
    Resolver404,
    ResolverMatch,
    get_script_prefix,
    resolve,
)
from rest_framework.decorators import api_view, permission_classes, renderer_classes
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response


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
    `request`, with ``Accept: text/html`` and
    `headers` (by name). A path under a published branch's prefix is served
    as `PublishedBranchMiddleware` serves it: at the API's URL, with the
    prefix's `Mount` (so the view's `check_mount` applies, and links stay
    in the prefix). A path nothing serves is the API's 404.

    The view runs in `effective.stored_state`: what it reads as data is
    the stored state, never the embedding render's safe mode or drafts;
    its response is rendered after, outside that block, so that its
    presentation keeps both.

    `path` is split once, with `urllib.parse.urlsplit` (its fragment
    dropped), unless `query` is given: `path` is then the path alone, as
    the caller split it, so that what is dispatched is what the caller
    checked. `resolved` is `resolve_path`'s answer for the path, when the
    caller has it already."""
    from scrud_django.ui.branches.effective import stored_state
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
    # The view reads the stored state: safe mode and drafts are the
    # embedding render's, render-only, so they are cut here, around the
    # view alone. The response is rendered (DRF renders lazily) outside the
    # block, so that its presentation keeps them.
    with stored_state():
        response = match.func(internal, *match.args, **match.kwargs)
    return _rendered(response)


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
