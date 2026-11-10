"""Published branches at their prefixes: `PublishedBranchMiddleware` routes
``<prefix><type slug>/...`` to the API's URL of the same resource, whose
views serve it with the same checks (and `check_mount`); only the
presentation differs (the branch's components and stylesheets).

Install it after ``AuthenticationMiddleware``::

    MIDDLEWARE = [
        ...,
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "scrud_django.ui.branches.middleware.PublishedBranchMiddleware",
        ...,
    ]
"""

from django.urls import NoReverseMatch, Resolver404, get_script_prefix, resolve
from rest_framework.exceptions import NotAuthenticated, PermissionDenied

from scrud_django.ui.branches import PREFIX
from scrud_django.ui.branches.selection import (
    MOUNT_ATTR,
    Mount,
    api_base,
    may_get,
    mount_of,
)
from scrud_django.ui.branches.store import branches

__all__ = ["Mount", "PublishedBranchMiddleware", "check_mount", "mount_of"]

# The first segment of the CSS files' URLs (``<api>ui-css-files/...``),
# which a prefix claims too, and the name of their route.
CSS_FILES_SEGMENT = "ui-css-files"
CSS_FILE_URL_NAME = "ui-css-file"


class PublishedBranchMiddleware:
    """Route a request for ``<prefix><slug>/...``, where `prefix` is a
    published branch's (not ``""``) and `slug` a type's that the API serves
    with its resource views (or the CSS files' segment), to the API's URL:
    `request.path_info` becomes ``<api_base><slug>/...``, and the request
    gets a `Mount` (``request.scrud_ui_mount``). `request.path` stays the
    URL requested, so that the HTML's links stay in the mount. The longest
    such prefix wins. Any other request is left as it is: a prefix claims
    nothing else, so the project's URLconf answers it (a 404 when it has
    nothing there)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        found = find_mount(request.path_info)
        if found is not None:
            mount, path_info = found
            setattr(request, MOUNT_ATTR, mount)
            request.path_info = path_info
        return self.get_response(request)


def find_mount(path_info: str) -> tuple[Mount, str] | None:
    """The `Mount` of a request for `path_info` and the API path it is
    routed to, or ``None``."""
    try:
        base = api_base()
    except NoReverseMatch:  # the project does not serve the UI's types
        return None
    script = get_script_prefix()
    if not base.startswith(script):
        return None
    base_info = "/" + base[len(script) :]
    if path_info.startswith(base_info):
        # The API's URLs are never mounted, and read no branch (ruling R1).
        return None
    # Every other request reads the branches (one query): the cost of
    # prefixes, which ``docs/ui.md`` tells.
    published = [
        branch
        for branch in branches().values()
        if branch.prefix and PREFIX.fullmatch(branch.prefix)
    ]
    for branch in sorted(published, key=lambda b: len(b.prefix), reverse=True):
        if not path_info.startswith(branch.prefix):
            continue
        rest = path_info[len(branch.prefix) :]
        target = base_info + rest
        if routes_to_api(target, rest):
            return Mount(branch.prefix, branch.name, base, script), target
    return None


def routes_to_api(path_info: str, rest: str) -> bool:
    """Whether `path_info` (the API's base and then `rest`) resolves to a
    resource view of the type whose slug is `rest`'s first segment, by its
    own route (``<slug>-list`` or ``<slug>-detail``), or to the CSS files'
    route: never anything else the URLconf serves, whatever `rest` holds
    (``..`` segments, decoded ``%2F``)."""
    segment, slash, _ = rest.partition("/")
    if not segment or not slash:
        return False
    try:
        match = resolve(path_info)
    except Resolver404:
        return False
    if segment == CSS_FILES_SEGMENT:
        return match.url_name == CSS_FILE_URL_NAME
    return match.url_name in (f"{segment}-list", f"{segment}-detail") and (
        is_resource_view(match.func, segment)
    )


def is_resource_view(view, slug: str) -> bool:
    """Whether `view` is a scrud resource view serving the type `slug`."""
    from scrud_django.mixins.view_mixins import ResourceViewMixin

    cls = getattr(view, "cls", None)
    initkwargs = getattr(view, "initkwargs", None) or {}
    return (
        isinstance(cls, type)
        and issubclass(cls, ResourceViewMixin)
        and initkwargs.get("resource_type_name") == slug
    )


def check_mount(request) -> None:
    """Refuse a request under a prefix (`mount_of`) whose user may not GET
    the prefix's branch, as a refused GET is refused: `NotAuthenticated`
    (a 401, or a 403 without an authentication header) when no
    authenticator authenticated the request, else `PermissionDenied`. Call
    it after the view authenticated and authorized the request. It holds
    for HTML and JSON alike: there is no fallback to another
    presentation."""
    mount = mount_of(request)
    if mount is None:
        return
    branch = branches().get(mount.branch)
    if (
        branch is not None
        and branch.prefix == mount.prefix
        and may_get(request, branch)
    ):
        return
    if getattr(request, "authenticators", None) and not getattr(
        request, "successful_authenticator", None
    ):
        raise NotAuthenticated()
    raise PermissionDenied()
