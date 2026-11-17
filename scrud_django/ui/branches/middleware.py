"""Published branches at their prefixes: `PublishedBranchMiddleware` routes
``<prefix><type slug>/...`` to the UI app's page of the same URL
(``scrud_django.ui.urls``), which serves it with the same checks, GET on the
prefix's branch among them (`check_mount`); only the presentation differs
(the branch's components and stylesheets). Under a prefix only the UI
answers: the UI does not negotiate, so a request asking for JSON gets the
page's HTML. JSON is at the API's URLs, where the branch is chosen by the
``Scrud-UI-Branch`` header or the ``ui-branch`` parameter.

Install it after ``AuthenticationMiddleware``::

    MIDDLEWARE = [
        ...,
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "scrud_django.ui.branches.middleware.PublishedBranchMiddleware",
        ...,
    ]
"""

import logging

from django.db.utils import OperationalError, ProgrammingError
from django.urls import NoReverseMatch, Resolver404, get_script_prefix, resolve

from scrud_django import ui
from scrud_django.ui import CSS_FILES_SEGMENT, JS_FILES_SEGMENT, UI_NAMESPACE
from scrud_django.ui.branches import PREFIX
from scrud_django.ui.branches.file_urls import (  # noqa: F401
    CSS_FILE_URL_NAME,
    JS_FILE_URL_NAME,
)
from scrud_django.ui.branches.selection import (
    MOUNT_ATTR,
    Mount,
    api_base_info,
    as_path_info,
    check_mount_access,
    mount_of,
    ui_base,
)
from scrud_django.ui.branches.store import stored_branches

logger = logging.getLogger(__name__)

__all__ = ["Mount", "PublishedBranchMiddleware", "check_mount", "mount_of"]

# The first segment of the CSS and JS files' URLs (``<ui>ui-css-files/...``,
# ``<ui>ui-js-files/...``, `scrud_django.ui`'s), which a prefix claims too,
# and the name of each one's route (in the UI's namespace).
FILE_ROUTES = {
    CSS_FILES_SEGMENT: CSS_FILE_URL_NAME,
    JS_FILES_SEGMENT: JS_FILE_URL_NAME,
}


class PublishedBranchMiddleware:
    """Route a request for ``<prefix><slug>/...``, where `prefix` is a
    published branch's (not ``""``, nor the UI's own base) and `slug` a
    type's that the UI app serves pages of (or a CSS or JS files' segment),
    to the UI's page of it: `request.path_info` becomes
    ``<ui_base><slug>/...``, and the request gets a `Mount`
    (``request.scrud_ui_mount``), whatever its method. `request.path`
    stays the URL requested, so that the HTML's links stay in the mount.
    The longest such prefix wins. Any other request is left as it is: a
    prefix claims nothing else, so the project's URLconf answers it (a 404
    when it has nothing there). So is every request when
    ``scrud_django.ui`` is not installed: its views would not
    `check_mount`."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not ui.is_enabled():
            return self.get_response(request)
        from scrud_django.ui.pages import served_memo

        # The page's URL is resolved twice (here, then by Django): its type
        # is read once.
        with served_memo():
            found = find_mount(request.path_info)
            if found is not None:
                mount, path_info = found
                setattr(request, MOUNT_ATTR, mount)
                request.path_info = path_info
            return self.get_response(request)


def find_mount(path_info: str) -> tuple[Mount, str] | None:
    """The `Mount` of a request for `path_info` and the UI path it is
    routed to, or ``None``. A path under the API's base is never mounted,
    and reads no branch (ruling R1); nor is a prefix that is the UI's own
    base, whose pages are ``main``'s (``/`` is the site's normal
    presentation). A database error reading the branches is logged (a
    warning) and mounts nothing, so that the project's other pages still
    answer before ``migrate`` or while the database is unreachable."""
    try:
        base = ui_base()
    except NoReverseMatch:  # the project does not include the UI app
        return None
    script = get_script_prefix()
    if not base.startswith(script):
        return None
    base_info = as_path_info(base, script)
    api_info = api_base_info()
    if api_info is not None and path_info.startswith(api_info):
        # The API's URLs are never mounted, and read no branch (ruling R1).
        return None
    # Every other request reads the stored branches: one query per request,
    # the cost of prefixes (ruling R1). The Mount keeps the branch's content
    # for check_mount, which so reads no branch again.
    try:
        stored = stored_branches()
    except (OperationalError, ProgrammingError) as error:
        logger.warning(
            "scrud-ui: not routing %r to a published branch: reading the "
            "branches failed (%s: %s)",
            path_info,
            type(error).__name__,
            error,
        )
        return None
    published = [
        (branch, content)
        for branch, content in stored.values()
        if branch.prefix and PREFIX.fullmatch(branch.prefix)
    ]
    published.sort(key=lambda found: len(found[0].prefix), reverse=True)
    for branch, content in published:
        if branch.prefix == base_info or not path_info.startswith(branch.prefix):
            continue
        rest = path_info[len(branch.prefix) :]
        target = base_info + rest
        if routes_to_ui(target, rest):
            mount = Mount(branch.prefix, branch.name, base, script, content)
            return mount, target
    return None


def routes_to_ui(path_info: str, rest: str) -> bool:
    """Whether `path_info` (the UI's base and then `rest`) resolves to the UI
    app's page of the type whose slug is `rest`'s first segment (its list or
    one of its items: ``scrud_ui:list``, ``scrud_ui:detail``), or to the CSS
    or JS files' route (``scrud_ui:ui-css-file``, ``scrud_ui:ui-js-file``):
    never anything else the URLconf serves, whatever `rest` holds (``..``
    segments, decoded ``%2F``)."""
    segment, slash, _ = rest.partition("/")
    if not segment or not slash:
        return False
    try:
        match = resolve(path_info)
    except Resolver404:
        return False
    if match.app_names != [UI_NAMESPACE]:
        return False
    if segment in FILE_ROUTES:
        return match.url_name == FILE_ROUTES[segment]
    return match.url_name in ("list", "detail") and match.kwargs.get("slug") == segment


def is_resource_view(view, slug: str) -> bool:
    """Whether `view` is a scrud resource view serving the type `slug`: a
    view a trusted builder marked (`scrud_django.core.types.mark_route`:
    registration's DRF routes, ninja's ``api()``), and, for a DRF view, one
    built for `slug` (its ``resource_type_name``)."""
    from scrud_django.core.types import UNRECORDED_ROUTE, route_authorization

    if route_authorization(view) == UNRECORDED_ROUTE:
        return False
    initkwargs = getattr(view, "initkwargs", None) or {}
    return initkwargs.get("resource_type_name", slug) == slug


def check_mount(request) -> None:
    """Refuse a request under a prefix (`mount_of`) whose user may not GET
    the prefix's branch, as a refused GET is refused: ``access.AccessDenied``
    (`selection.check_mount_access`; 401 for the anonymous user, else 403,
    as `scrud_django.core.errors.error_answer` answers it). The UI's pages
    and files ask it after the request check. There is no fallback to
    another presentation."""
    check_mount_access(request)
