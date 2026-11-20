"""The branch a request browses: chosen from its `BRANCH_HEADER`, its
`BRANCH_PARAMETER` query parameter or its cookie (`cookie_name`: one per
prefix), in that order, else the published branch of the request's prefix
(`Mount`; ``main`` at the UI's own URLs and the API's).

A value is ``<name>`` or ``<name>@pinned`` (`VALUE`, at most
`MAX_VALUE_BYTES`); any other is ignored, as if absent. An empty header or
parameter names the published branch. A branch that is unknown, whose chain
is broken, that the viewer may not GET (it, or a working ancestor of it:
`may_get_chain`), that is not in the prefix's tree, or whose pinned snapshot
is missing is refused: the published branch is used,
and `Chosen.refused` names it (the banner says so). A request with no
selection, or one naming the published branch, reads no branch row (ruling
R1)."""

import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from django.middleware.csrf import get_token
from django.urls import NoReverseMatch, get_script_prefix, reverse
from django.utils.crypto import constant_time_compare, salted_hmac
from scoped_rbac import access

from scrud_django.core.authz import may_read_in_context, resource_contexts
from scrud_django.core.urls import api_route_name, api_slugs
from scrud_django.models import Resource
from scrud_django.ui import UI_NAMESPACE
from scrud_django.ui.branches import MAIN, UI_BRANCH_TYPE_URI
from scrud_django.ui.branches.effective import (
    BRANCH_HEADER,
    BRANCH_PARAMETER,
    PINNED_SUFFIX,
    Selection,
    _missing_snapshot,
    chain_of,
)
from scrud_django.ui.branches.model import Branch
from scrud_django.ui.branches.store import BranchChainError, branches, published_root
from scrud_django.ui.cascade import _memo
from scrud_django.ui.types import UI_COMPONENT_SLUG

logger = logging.getLogger(__name__)

# The cookie's name at the API's URLs; under a prefix, `cookie_name`.
BRANCH_COOKIE = "scrud_ui_branch"
CONFIRM_PARAMETER = "ui-branch-confirm"
CONFIRM_SALT = "scrud_django.ui.branch-confirm"
VALUE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}(?:@pinned)?")
MAX_VALUE_BYTES = 80

# Where a selection came from.
HEADER, PARAM, COOKIE, NONE = "header", "param", "cookie", "none"

# On the request: its `Mount` (`PublishedBranchMiddleware`).
MOUNT_ATTR = "scrud_ui_mount"
# On the request of a UI page (`scrud_django.ui.pages`): the UI's base path,
# which its API hrefs are mapped under (`in_mount`).
UI_BASE_ATTR = "scrud_ui_base"
# On the request: {use the cookie: Chosen}.
_CHOSEN_ATTR = "_scrud_ui_chosen"
# The key the stored branch contents are memoised under in a `rules_memo`.
_CONTENTS = "branch-contents"


@dataclass(frozen=True)
class Chosen:
    """The `selection` a request renders, the `source` of the value that
    chose it (``"header"``, ``"param"``, ``"cookie"`` or ``"none"``), the
    branch name it `refused` (``None`` when it was used), and whether the
    parameter came `confirmed` (with this viewer's `confirm_token` for it,
    not cross-site)."""

    selection: Selection
    source: str
    refused: str | None
    confirmed: bool


def api_base() -> str:
    """The path the API's type URLs start with (``/api/``): the path of the
    UI components' list without its slug, else (a project whose API does
    not route the UI's types: a ninja ``api(ui_types=False)``) of the first
    type whose list the URLconf routes, among those a front end built
    routes of (`scrud_django.core.urls.api_slugs`), outside any URL
    namespace or in a ninja ``api()``'s
    (`scrud_django.core.urls.api_route_name`). ``NoReverseMatch`` when it
    routes none."""
    # The UI components' first: asking imports the URLconf, whose APIs note
    # their routes, so that the slugs are read once noted (on a process's
    # first call too).
    name = api_route_name(UI_COMPONENT_SLUG, "list")
    if name is not None:
        return _list_base(name, UI_COMPONENT_SLUG)
    for slug in api_slugs():
        name = api_route_name(slug, "list")
        if name is not None:
            return _list_base(name, slug)
    raise NoReverseMatch("The URLconf routes no type's list.")


def _list_base(name, slug) -> str:
    """The path of the list route `name` of the type `slug`, without its
    slug."""
    path = reverse(name)
    suffix = slug + "/"
    return path[: -len(suffix)] if path.endswith(suffix) else path


def ui_base() -> str:
    """The path the UI app's pages start with (``/`` when the project
    includes ``scrud_django.ui.urls`` at its root): the URL of a list page
    in the UI's namespace (``scrud_ui:list``) without its slug.
    ``NoReverseMatch`` when the project does not include the UI app."""
    slug = "x"
    path = reverse(f"{UI_NAMESPACE}:list", kwargs={"slug": slug})
    return path[: -len(slug) - 1]


def as_path_info(path: str, script: str | None = None) -> str:
    """The URL path `path` as a ``path_info`` (as prefixes are matched):
    without the script prefix `script` (default: `get_script_prefix`); a
    path not under it as it is."""
    if script is None:
        script = get_script_prefix()
    return "/" + path[len(script) :] if path.startswith(script) else path


def api_base_info() -> str | None:
    """The API's base (`api_base`) as a ``path_info``; ``None`` when the
    project routes no type's list."""
    try:
        return as_path_info(api_base())
    except NoReverseMatch:
        return None


def ui_base_info() -> str:
    """The UI app's base (`ui_base`) as a ``path_info``, which a prefix's
    URLs are routed under. ``NoReverseMatch`` when the project does not
    include the UI app."""
    return as_path_info(ui_base())


@dataclass(frozen=True)
class Mount:
    """A request under the `prefix` of the published branch `branch`, which
    `PublishedBranchMiddleware` routed to the UI app's page of the same URL
    under `ui_base` (`ui_base()`, a URL path): `prefix` is a path as the
    URLconf matches it (under the script prefix, `script`). `content` is
    the branch as stored when the middleware read it (`may_get_stored`), so
    that `check_mount_access` reads no branch again; ``None``: read it
    then."""

    prefix: str
    branch: str
    ui_base: str
    script: str = "/"
    content: Mapping | None = field(default=None, compare=False, repr=False)

    @property
    def url_prefix(self) -> str:
        """`prefix` as a URL path (with the script prefix)."""
        return self.script + self.prefix[1:]

    def href(self, url: str) -> str:
        """`url` (absolute or a path) under the prefix: an API URL with the
        API's base (`api_base`) swapped for the prefix, a UI page's (one the
        prefix claims: `middleware.routes_to_ui`) with the UI's base; any
        other URL (one already under the prefix too) as it is."""
        path = urlsplit(url).path
        try:
            api = api_base()
        except NoReverseMatch:
            api = None
        if api is not None and path.startswith(api):
            return _moved(url, api, self.url_prefix)
        if path.startswith(self.ui_base) and self._claims_ui_path(path):
            return _moved(url, self.ui_base, self.url_prefix)
        return url

    def canonical(self, href: str) -> str:
        """The UI app's URL of `href`, a URL under the prefix (the page the
        prefix serves it with); any other URL as it is."""
        return _moved(href, self.url_prefix, self.ui_base)

    def _claims_ui_path(self, path) -> bool:
        """Whether the UI path `path` is one of a page a prefix claims."""
        from scrud_django.ui.branches.middleware import routes_to_ui

        if not path.startswith(self.script):
            return False
        # As the server hands the path to Django: decoded.
        path_info = "/" + unquote(path[len(self.script) :])
        rest = unquote(path[len(self.ui_base) :])
        return routes_to_ui(path_info, rest)


def mount_at(branch: Branch) -> Mount | None:
    """The `Mount` of the published `branch`'s prefix, which links into it
    are mapped by (`Mount.href`); ``None`` when it has no prefix, when its
    prefix is the UI's own base (main's pages: the middleware never mounts
    it, so links must not lead there), or when the project does not include
    the UI app."""
    if not branch.prefix:
        return None
    try:
        base = ui_base()
    except NoReverseMatch:
        return None
    script = get_script_prefix()
    if branch.prefix == as_path_info(base, script):
        return None
    return Mount(branch.prefix, branch.name, base, script)


def _moved(url, old, new):
    parts = urlsplit(url)
    if not parts.path.startswith(old):
        return url
    return parts._replace(path=new + parts.path[len(old) :]).geturl()


def mount_of(request) -> Mount | None:
    """The `Mount` of `request` (a Django or DRF request), or ``None`` at
    the API's URLs."""
    mount = getattr(request, MOUNT_ATTR, None)
    return mount if isinstance(mount, Mount) else None


def ui_base_of(request) -> str | None:
    """The UI's base path of `request`, a UI page's (``request.scrud_ui_base``,
    which `scrud_django.ui.pages` sets), or ``None``."""
    base = getattr(request, UI_BASE_ATTR, None)
    return base if isinstance(base, str) else None


def in_mount(request, href: str) -> str:
    """`href`, an API URL, as the request's page serves it: under its prefix
    (`Mount.href`); else, on a UI page (`ui_base_of`), under the UI's base
    instead of the API's (`api_base`); else as it is (the API serves JSON only: a request
    that is no UI page's keeps the API's URLs)."""
    mount = mount_of(request)
    if mount is not None:
        return mount.href(href)
    ui_base = ui_base_of(request)
    if ui_base is None:
        return href
    try:
        base = api_base()
    except NoReverseMatch:
        return href
    return _moved(href, base, ui_base)


def published_branch(request) -> str:
    """The name of the published branch of `request`'s prefix (its
    `Mount`), else ``main``: at the UI's own URLs and the API's."""
    mount = mount_of(request)
    return mount.branch if mount is not None else MAIN


def cookie_name(request) -> str:
    """The name of the cookie that keeps the branch chosen at `request`'s
    URLs (ruling R28): `BRANCH_COOKIE` at the API's, else
    ``scrud_ui_branch_<the first 12 hex digits of the SHA-256 of the
    prefix>``. A cookie scoped to ``/`` is sent to every prefix too: its own
    name keeps it from being read, or shadowing the cookie, there."""
    mount = mount_of(request)
    if mount is None:
        return BRANCH_COOKIE
    digest = hashlib.sha256(mount.prefix.encode("utf-8")).hexdigest()
    return f"{BRANCH_COOKIE}_{digest[:12]}"


def cookie_path(request) -> str:
    """The path the cookie (`cookie_name`) is scoped to: the prefix's, else
    a UI page's base (`ui_base_of`), else the API's."""
    mount = mount_of(request)
    if mount is not None:
        return mount.url_prefix
    return ui_base_of(request) or api_base()


def selection_value(selection: Selection) -> str:
    """`selection` as a header, parameter or cookie value."""
    return selection.branch + (PINNED_SUFFIX if selection.pinned else "")


def parse_value(value) -> Selection | None:
    """The selection `value` names, or ``None`` when it is not one
    (`VALUE`, at most `MAX_VALUE_BYTES`)."""
    if not isinstance(value, str) or not VALUE.fullmatch(value):
        return None
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        return None
    pinned = value.endswith(PINNED_SUFFIX)
    name = value[: -len(PINNED_SUFFIX)] if pinned else value
    return Selection(branch=name, pinned=pinned)


def is_published(chosen: Chosen, request) -> bool:
    return chosen.selection == _published_selection(published_branch(request))


def frame_branch(request) -> str | None:
    """What a frame's ``data-scrud-branch`` says, so that the requests made
    from it (`BRANCH_HEADER`) stay on the page's branch: the selection's
    value off the published branch; ``""`` on the published branch when the
    request named something (so that a cookie does not move its fragments);
    else ``None``, no attribute."""
    if request is None:
        return None
    chosen = choose(request)
    if not is_published(chosen, request):
        return selection_value(chosen.selection)
    return "" if chosen.source != NONE else None


def cookie_branch(request) -> str | None:
    """The branch name the request's cookie (`cookie_name`) holds, when it
    is a selection value."""
    cookies = getattr(request, "COOKIES", None)
    selection = parse_value(cookies.get(cookie_name(request)) if cookies else None)
    return selection.branch if selection is not None else None


def choose(request, *, cookie: bool = True) -> Chosen:
    """The branch `request` browses (`Chosen`). Without `cookie`, the cookie
    is not read: JSON is not affected by browsing. Chosen once per request."""
    if request is None:
        return Chosen(Selection(), NONE, None, False)
    cache = getattr(request, _CHOSEN_ATTR, None)
    if cache is None:
        cache = {}
        try:
            setattr(request, _CHOSEN_ATTR, cache)
        except AttributeError:
            pass
    if cookie not in cache:
        cache[cookie] = _choose(request, cookie)
    return cache[cookie]


def selection_for_request(request) -> Selection:
    """`choose(request).selection`."""
    return choose(request).selection


def _choose(request, cookie):
    published = published_branch(request)
    for source, value in _values(request, cookie):
        if value == "" and source != COOKIE:
            selection = _published_selection(published)
        else:
            selection = parse_value(value)
            if selection is None:
                continue  # ignored
        confirmed = source == PARAM and _confirmed(request, value)
        if selection.branch == published:
            return Chosen(_published_selection(published), source, None, confirmed)
        if _selectable(request, selection, published):
            return Chosen(selection, source, None, confirmed)
        return Chosen(
            _published_selection(published), source, selection.branch, confirmed
        )
    return Chosen(_published_selection(published), NONE, None, False)


def _values(request, cookie):
    headers = getattr(request, "headers", None)
    params = getattr(request, "GET", None)
    yield HEADER, headers.get(BRANCH_HEADER) if headers is not None else None
    yield PARAM, params.get(BRANCH_PARAMETER) if params is not None else None
    if cookie:
        cookies = getattr(request, "COOKIES", None)
        yield COOKIE, cookies.get(cookie_name(request)) if cookies else None


def _published_selection(published):
    return Selection(branch=published)


def confirm_token(request, value: str) -> str:
    """The `CONFIRM_PARAMETER` value that confirms selecting `value` (a
    `BRANCH_PARAMETER` value) for the viewer of `request`: an HMAC of the
    value and the viewer's CSRF secret, so that only links the server
    rendered for this viewer carry it, never one a writer planted in
    content. Makes sure the viewer has a CSRF secret (``get_token``, which
    has the response keep it), so that the link works on the next request."""
    http = getattr(request, "_request", request)
    get_token(http)
    return _token(value, http.META.get("CSRF_COOKIE"))


def _token(value, secret) -> str | None:
    if not secret:
        return None
    return salted_hmac(CONFIRM_SALT, f"{value}|{secret}").hexdigest()


def _csrf_secret(request):
    """The viewer's CSRF secret, as ``CsrfViewMiddleware`` read it from the
    cookie or, with ``CSRF_USE_SESSIONS``, the session (unmasked, in
    ``META["CSRF_COOKIE"]`` either way); ``None`` without one."""
    meta = getattr(request, "META", None)
    return meta.get("CSRF_COOKIE") if meta is not None else None


def _confirmed(request, value) -> bool:
    """Whether the request carries this viewer's `confirm_token` for `value`
    (the server's own links: the confirmation's Continue, the banner's), and
    does not come from another site or the address bar (``Sec-Fetch-Site``,
    as a second defence): any other is asked to confirm."""
    params = getattr(request, "GET", None)
    given = params.get(CONFIRM_PARAMETER) if params is not None else None
    expected = _token(value, _csrf_secret(request))
    if not given or expected is None or not constant_time_compare(given, expected):
        return False
    headers = getattr(request, "headers", None)
    site = headers.get("Sec-Fetch-Site") if headers is not None else None
    return site in (None, "same-origin")


def _selectable(request, selection, published) -> bool:
    """`selectable`, and in the tree of the `published` branch."""
    all_branches = branches()
    name = selection.branch
    if name not in all_branches:
        return False
    try:
        root = published_root(name, all_branches)
    except BranchChainError as error:
        logger.warning("scrud-ui: not rendering branch %r: %s", name, error)
        return False
    return root.name == published and selectable(
        request, selection, all_branches=all_branches
    )


def selectable(request, selection: Selection, *, all_branches=None) -> bool:
    """Whether the user of `request` may render `selection`, wherever its
    published root is: ``main`` (shown to everyone at the API's URLs, where
    `choose` never refuses it); else a known branch whose chain is intact,
    that the user may GET with its working ancestors (`may_get_chain`), and
    whose pinned snapshots are stored. There is no prefix-tree check:
    `choose` adds it. `all_branches` are `branches()` unless given."""
    if selection.branch == MAIN:
        return True
    if all_branches is None:
        all_branches = branches()
    name = selection.branch
    if name not in all_branches:
        return False
    try:
        chain = chain_of(name, all_branches)
    except BranchChainError as error:
        logger.warning("scrud-ui: not rendering branch %r: %s", name, error)
        return False
    if not may_get_chain(request, chain):
        return False
    missing = _missing_snapshot(chain, selection.pinned)
    if missing is not None:
        logger.warning(
            "scrud-ui: not rendering branch %r: version %r of %r, which it is "
            "pinned to, is missing",
            name,
            missing[1],
            missing[0],
        )
        return False
    return True


def may_get(request, branch: Branch) -> bool:
    """Whether the user of `request` may GET `branch`, as stored."""
    return may_get_stored(request, _stored_contents().get(branch.pk))


def may_get_chain(request, chain: list[Branch]) -> bool:
    """Whether the user of `request` may GET the first branch of `chain` (a
    branch, then its ancestors) and every working ancestor in it (ruling
    R36): a branch shows its ancestors' items, so it would otherwise show a
    working branch its viewer may not see. A published ancestor is not
    checked: it is the site's presentation, opened at its own prefix only
    with GET on it (`check_mount_access`)."""
    branch, *ancestors = chain
    return may_get(request, branch) and all(
        may_get(request, ancestor) for ancestor in ancestors if not ancestor.published
    )


def branch_content(branch: Branch, using=None) -> Mapping | None:
    """The content `branch` is stored with, read on `using` (default: the
    router's read database), or, for the virtual ``main``, the content it is
    reserved to have; ``None`` when its row is gone."""
    if branch.pk is None:
        return {"name": branch.name, "rbac_context": branch.context}
    rows = Resource.objects.using(using) if using else Resource.objects
    return rows.filter(pk=branch.pk).values_list("content", flat=True).first()


def may_get_mount(request, mount: Mount) -> bool:
    """Whether the user of `request` may GET `mount`'s branch: as the
    middleware read it (`Mount.content`), else as stored now (and still
    published at the mount's prefix)."""
    if mount.content is not None:
        return may_get_stored(request, mount.content)
    branch = branches().get(mount.branch)
    return (
        branch is not None
        and branch.prefix == mount.prefix
        and may_get(request, branch)
    )


def check_mount_access(request) -> None:
    """Refuse a request under a published branch's prefix (`mount_of`)
    whose user may not GET the prefix's branch (`may_get_mount`), as a
    refused GET is refused: ``access.AccessDenied`` (401 for the anonymous
    user, else 403, as `scrud_django.core.errors.error_answer` answers it).
    The neutral check of the UI app's views."""
    mount = mount_of(request)
    if mount is not None and not may_get_mount(request, mount):
        raise access.AccessDenied()


def may_get_stored(request, content) -> bool:
    """Whether the user of `request` may GET the branch stored with
    `content`."""
    if not isinstance(content, Mapping):
        return False
    return may_read_in_context(
        request, UI_BRANCH_TYPE_URI, content, resource_contexts(content)
    )


def selectable_branches(request) -> list[str]:
    """The names of the working branches in the tree of `request`'s
    published branch that its user may select (GET, with their working
    ancestors: `may_get_chain`), sorted."""
    published = published_branch(request)
    all_branches = branches()
    found = []
    for name, branch in all_branches.items():
        if branch.published:
            continue
        try:
            root = published_root(name, all_branches)
            chain = chain_of(name, all_branches)
        except BranchChainError:
            continue
        if root.name == published and may_get_chain(request, chain):
            found.append(name)
    return sorted(found)


def _stored_contents() -> dict:
    """``{pk: content}`` of every stored branch, read once per `rules_memo`."""
    memo = _memo.get()
    if memo is not None and _CONTENTS in memo:
        return memo[_CONTENTS]
    found = dict(
        Resource.objects.filter(resource_type__type_uri=UI_BRANCH_TYPE_URI).values_list(
            "pk", "content"
        )
    )
    if memo is not None:
        memo[_CONTENTS] = found
    return found
