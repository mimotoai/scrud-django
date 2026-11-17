"""Server-side embeds: ``embed(path, scope, mode)`` in a stored Jinja
component's context (node, list and page) renders the resource at `path` as the
fragment its own page answers, in process, as the same user: the UI pages'
render functions (`pages.render_list`, `pages.render_item`), so that the
page's checks (the route's request check, the prefix's `check_mount_access`,
hidden is missing) apply unchanged: an embed never shows what the API
would not.

The embedded request is a GET with ``HX-Request: true`` (a fragment), the
scope chain (`SCOPE_HEADER`) when one is given (else the path's own
``?ui-scope=`` applies), ``Scrud-UI-Mode: view`` when `mode` is
``"view"``, and the embedding render's branch (`BRANCH_HEADER`, the active
selection's value). A response of status 400 or more is embedded as it is:
the notice the resource's own URL answers with.

What the embed carries over in process, by context variable rather than by
header: safe mode (`branches.effective.using_safe_mode`, which the
renderer keeps), preview (`render.using_preview`, which `render_resource`
keeps; a preview requested in view mode, `render.using_view_preview`, sends
``Scrud-UI-Mode: view`` with every embed in it), the embed stack and the
response's embed count (`_state`). Safe mode and a live preview's drafts
are render-only: the page reads in `effective.stored_state`, so that what
it reads as data is the stored state, and renders outside that block.

`path` is split once (`urllib.parse.urlsplit`; its fragment is dropped),
and its path and query are what is checked and rendered. An ``http`` or
``https`` URL of the request's own host (``request.get_host()``) is taken
as its path, so that a list item's absolute ``href`` embeds. The path is an
API path (``/api/<slug>/...``), a UI path (``/<slug>/...``) or a path under
a published branch's prefix; the embed inherits the embedding request's
mode, as links do (`selection.in_mount`): inside a UI page its fragments
carry the UI's hrefs, and under a prefix the prefix's (the prefix path's
own, when it is one).

Refused, with the notice `REFUSED_MESSAGE` (and a warning logged): a path
that is not a string, not absolute, or has another scheme or host; one that
is no page of a type the UI serves (stored and computed types), or asks for
another format (``?format=json``); a path and scope chain already on the
embed stack (a cycle; the top-level request is its bottom); an embed deeper
than `MAX_EMBED_DEPTH`; and any embed past the first `MAX_EMBEDS` of a
top-level response. A page that raises renders `FAILED_MESSAGE`: logged at
ERROR, with ``got_request_exception`` sent, and rolled back to a savepoint
of its own on each database in an atomic block, so that the embedding
render's transaction stays usable.

`component_globals` gives a stored Jinja component ``embed`` and two more
globals: ``in_mount(href)``, an API URL as the request's prefix serves it
(`selection.in_mount`), and ``api_url(slug, key=None)``, the API path of a
registered type's list or detail (`api_url`)."""

import contextvars
import logging
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import DisallowedHost
from django.core.signals import got_request_exception
from django.db import connections, transaction
from django.http import QueryDict
from django.test.client import RequestFactory
from django.urls import (
    NoReverseMatch,
    Resolver404,
    get_script_prefix,
    resolve,
    reverse,
)
from markupsafe import Markup

from scrud_django.ui import UI_NAMESPACE
from scrud_django.ui.branches.effective import BRANCH_HEADER, active_selection
from scrud_django.ui.branches.selection import (
    MOUNT_ATTR,
    UI_BASE_ATTR,
    in_mount,
    mount_of,
    selection_value,
)
from scrud_django.ui.cascade import builtin_component, rules_memo

logger = logging.getLogger(__name__)

# How deep embeds nest below the top-level response (an embed of an embed of
# an embed), and how many one top-level response makes, nested ones
# included.
MAX_EMBED_DEPTH = 3
MAX_EMBEDS = 20

REFUSED_MESSAGE = "This embed was refused."
FAILED_MESSAGE = "This embed failed."

# On the top-level request: its `_EmbedState`.
_REQUEST_ATTR = "_scrud_ui_embeds"


@dataclass
class _Count:
    embeds: int = 0


@dataclass(frozen=True)
class _EmbedState:
    """The embed `stack` (``(API path, scope chain)`` of the top-level
    request, then of each embed it is inside, outermost first), and the
    top-level response's embed `count`, shared by every embed in it."""

    stack: tuple[tuple[str, tuple[str, ...]], ...]
    count: _Count = field(default_factory=_Count)


# The state of the embed whose request is in progress (``None`` outside
# one: the top-level request's is on it, `_REQUEST_ATTR`).
_state: contextvars.ContextVar[_EmbedState | None] = contextvars.ContextVar(
    "scrud_ui_embed_state", default=None
)


def embed_for(request):
    """The ``embed`` callable of a component rendered for `request`:
    ``embed(path, scope=(), mode=None)`` returns the HTML (`Markup`) of the
    resource at `path` in the scope chain `scope` (a string of names, or a
    list of them), in view mode when `mode` is ``"view"``, or a notice."""

    def embed(path, scope=(), mode=None) -> Markup:
        return Markup(_embed(request, path, scope, mode))

    return embed


def component_globals(request) -> dict:
    """The globals a stored Jinja component rendered for `request` gets in
    its context (node, list and page): ``embed`` (`embed_for`),
    ``in_mount(href)``, `href` mapped into the request's prefix as
    `selection.in_mount` maps links (a value that is not a string is
    returned as it is), and ``api_url`` (`api_url`)."""

    def in_mount_global(href):
        return in_mount(request, href) if isinstance(href, str) else href

    return {
        "embed": embed_for(request),
        "in_mount": in_mount_global,
        "api_url": api_url,
    }


def api_url(slug, key=None) -> str:
    """The API path of the list of the registered resource type `slug` (its
    route ``<slug>-list``), or of its detail for `key` (``<slug>-detail``;
    a computed type has only that one), as `reverse` gives it: under the
    API's base, never a published branch's prefix (`embed` maps an API path
    into the page's prefix). `ValueError` (or `NoReverseMatch`) for a `slug`
    that is not a string, that has no such route, or whose route is not a
    resource view of the type `slug`, so that a component calling it fails
    and falls back."""
    from scrud_django.ui.branches.middleware import is_resource_view

    if not isinstance(slug, str) or not slug:
        raise ValueError(f"api_url: {slug!r} is not a type's slug")
    if key is None:
        path = reverse(f"{slug}-list")
    else:
        path = reverse(f"{slug}-detail", args=[key])
    script = get_script_prefix()
    if not path.startswith(script):
        raise ValueError(f"api_url: {path!r} is not under {script!r}")
    try:
        # As the server hands the path to Django: decoded.
        match = resolve("/" + unquote(path[len(script) :]))
    except Resolver404:
        raise ValueError(f"api_url: {path!r} is not served") from None
    if not is_resource_view(match.func, slug):
        raise ValueError(f"api_url: {slug!r} is not a registered resource type")
    return path


def _embed(request, path, scope, mode) -> str:
    # Not at the top: `pages` reaches `render_html`, which reaches this
    # module through `component_globals`.
    from scrud_django.ui import html, pages
    from scrud_django.ui.render import in_view_preview

    state = _state.get() or _top_state(request)
    state.count.embeds += 1
    if state.count.embeds > MAX_EMBEDS:
        return _refused(path, f"over the count limit ({MAX_EMBEDS} per response)")
    if not isinstance(path, str):
        return _refused(path, "not a URL path")
    try:
        # Split once: what is checked below is what is rendered.
        parts = urlsplit(path)
    except ValueError:
        return _refused(path, "not a URL path")
    if (parts.scheme or parts.netloc) and not _same_site(request, parts):
        return _refused(path, "not an absolute path on this site")
    if not parts.path.startswith("/"):
        return _refused(path, "not an absolute path on this site")
    if len(state.stack) > MAX_EMBED_DEPTH:
        return _refused(path, f"over the depth limit ({MAX_EMBED_DEPTH})")
    # An API path is embedded as the page's links are (`selection.in_mount`):
    # under the request's prefix, or the UI's base; a prefix path or a UI
    # path is as it is.
    target = _resolve(in_mount(request, parts.path))
    if target is None:
        return _refused(path, "not a resource view")
    query = QueryDict(parts.query)
    if "format" in query:
        # ``?format=json`` and the browsable API's: not the UI's HTML.
        return _refused(path, f"asked for {query['format']!r}, not UI HTML")
    chain = _scope_chain(scope)
    if chain is None:
        return _refused(path, f"scope {scope!r} is not a scope chain")
    if mode is not None and mode != html.VIEW_MODE:
        return _refused(path, f'mode {mode!r} is not "{html.VIEW_MODE}"')
    headers = {
        html.HX_REQUEST_HEADER: "true",
        BRANCH_HEADER: selection_value(active_selection()),
    }
    if chain:
        headers[html.SCOPE_HEADER] = " ".join(chain)
    else:
        # No scope given: the path's own ``?ui-scope=`` applies, as at its
        # URL (an empty header would override it).
        chain = html.parse_scope_chain(query.get(html.SCOPE_PARAMETER))
    key = (target.served, chain)
    if key in state.stack:
        return _refused(path, f"a cycle (in scope {' '.join(chain)!r})")
    if mode == html.VIEW_MODE or in_view_preview():
        # A view-mode preview shows what a viewer sees: an embed in it keeps
        # no edit control of a user who may PUT the embedded resource.
        headers[html.MODE_HEADER] = html.VIEW_MODE
    mount = target.mount or mount_of(request)
    url = _url_of(request, target, mount)
    internal = _request_for(request, url, parts.query, headers, mount)
    token = _state.set(_EmbedState(state.stack + (key,), state.count))
    try:
        # A memo of its own: the embedded request chooses its branch again
        # (it may be refused), and the memo is keyed without the branch.
        with rules_memo(), _savepoints():
            if target.pk is None:
                status, body = pages.render_list(
                    internal, target.slug, query=internal.GET, mount=mount
                )
            else:
                status, body = pages.render_item(
                    internal, target.slug, target.pk, mount=mount
                )
    except Exception:
        # As Django reports a view that raises at its URL: an error, with
        # its traceback, and the signal error reporters listen to.
        logger.error("scrud-ui: embed of %r failed", path, exc_info=True)
        got_request_exception.send(sender=None, request=request)
        return _notice(FAILED_MESSAGE)
    finally:
        _state.reset(token)
    return body


@dataclass(frozen=True)
class _Target:
    """What an embedded path resolves to: the `slug` of a type the UI
    serves (a `pages.ServedSlug`), the resource's `pk` (``None``: the
    type's list), the `mount` of the prefix it is under (``None``: none),
    and `served`, the API path (never in a prefix) that names it on the
    embed stack."""

    slug: str
    pk: str | None
    mount: object
    served: str


def _resolve(path) -> _Target | None:
    """The `_Target` of the absolute URL path `path` (no query: a path as
    an HTML link has it, under the script prefix) as a request for it would
    be routed: a path under a published branch's prefix to the UI's page
    the prefix serves it with (`find_mount`), the UI's list and detail
    pages (`UI_NAMESPACE`) and the API's of a registered type
    (``<slug>-list``, ``<slug>-detail``) as they are. ``None`` for any other
    path, a type the UI does not serve, and a computed type's list path
    (it has none)."""
    from scrud_django import ui
    from scrud_django.ui import pages
    from scrud_django.ui.branches.middleware import find_mount

    script = get_script_prefix()
    if not path.startswith(script):
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
    slug = pk = None
    if match.namespaces == [UI_NAMESPACE] and match.url_name in ("list", "detail"):
        slug, pk = match.kwargs["slug"], match.kwargs.get("pk")
    elif mount is not None:
        return None  # a prefix serves the UI's pages only
    else:
        name = match.url_name or ""
        for suffix in ("-list", "-detail"):
            if name.endswith(suffix) and not match.namespaces:
                slug = name[: -len(suffix)]
                # The detail's one kwarg is the resource's key (the API's
                # lookup: ``slug`` for DRF's routes, ``pk`` for others).
                keys = list(match.kwargs.values())
                pk = keys[0] if suffix == "-detail" and len(keys) == 1 else None
                if (pk is None) != (suffix == "-list"):
                    return None
                break
    if not slug or (pk is None and pages.is_computed(slug)):
        return None  # (a computed type has no list)
    served = pages.served(slug) if not hasattr(slug, "resource_type") else slug
    if served is None:
        return None
    if pk is not None:
        pk = str(pk)
    return _Target(served, pk, mount, _api_path(served, pk))


def _api_path(slug, pk) -> str:
    if pk is None:
        return reverse(f"{slug}-list")
    return reverse(f"{slug}-detail", args=[pk])


def _url_of(request, target, mount) -> str:
    """The URL the embedded page is at: the API path of `target` as
    `selection.in_mount` maps it for the embedding request (under `mount`'s
    prefix, else the UI's base on a UI page, else as it is)."""
    if mount is not None:
        return mount.href(target.served)
    return in_mount(request, target.served)


def _request_for(request, url, query, headers, mount):
    """The GET the embedded page is rendered for: `url` and `query`, with
    `headers` (by name) and `request`'s host, as the user of `request`; on
    the UI's base `request` has (`UI_BASE_ATTR`). Under a prefix, `mount`
    is its mount and its path_info the UI page's, as
    `PublishedBranchMiddleware` leaves it."""
    script = get_script_prefix()
    path_info = "/" + url[len(script) :] if url.startswith(script) else url
    extra = {"HTTP_ACCEPT": "text/html"}
    for name, value in headers.items():
        extra["HTTP_" + name.upper().replace("-", "_")] = value
    meta = getattr(request, "META", {})
    for name in ("HTTP_HOST", "SERVER_NAME", "SERVER_PORT"):
        if name in meta:
            extra[name] = meta[name]
    internal = RequestFactory().get(
        path_info,
        secure=request.is_secure(),
        QUERY_STRING=query,
        SCRIPT_NAME=script.rstrip("/"),
        **extra,
    )
    internal.user = getattr(request, "user", None) or AnonymousUser()
    ui_base = getattr(request, UI_BASE_ATTR, None)
    if ui_base is not None:
        setattr(internal, UI_BASE_ATTR, ui_base)
    if mount is not None:
        setattr(internal, MOUNT_ATTR, mount)
        internal.path_info = "/" + mount.canonical(url)[len(script) :]
    return internal


@contextmanager
def _savepoints():
    """A savepoint on each database connection in an atomic block (the
    embedding request's, under ``ATOMIC_REQUESTS``, or a studio preview's),
    so that an embedded view that fails on the database (on PostgreSQL, a
    failed query aborts the transaction) is rolled back alone: the
    embedding render's later queries still run (as the refused PUT's stored lookup in
    `scrud_django.ui.pages` does)."""
    with ExitStack() as stack:
        for alias in connections:
            if connections[alias].in_atomic_block:
                stack.enter_context(transaction.atomic(using=alias))
        yield


def _top_state(request) -> _EmbedState:
    """The embed state of the top-level request `request`, kept on it."""
    from scrud_django.ui import html

    http = getattr(request, "_request", request)
    state = getattr(http, _REQUEST_ATTR, None)
    if state is None:
        top = (_served_by(request), html.scope_chain_of(request))
        state = _EmbedState(stack=(top,))
        setattr(http, _REQUEST_ATTR, state)
    return state


def _served_by(request) -> str:
    """What names `request`'s page on the embed stack: the API path of its
    resource (`_Target.served`), else its path."""
    path_info = getattr(request, "path_info", "")
    try:
        match = resolve(path_info)
    except Resolver404:
        return path_info
    slug = match.kwargs.get("slug")
    if match.namespaces == [UI_NAMESPACE] and slug is not None:
        try:
            return _api_path(slug, match.kwargs.get("pk"))
        except NoReverseMatch:
            pass
    return path_info


def _same_site(request, parts) -> bool:
    """Whether `parts` (a split URL with a scheme or a host) is an ``http``
    or ``https`` URL of `request`'s own host (``get_host()``), so that its
    path may be embedded: a list item's ``href`` is absolute."""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    try:
        host = request.get_host()
    except DisallowedHost:
        return False
    return parts.netloc.lower() == host.lower()


def _scope_chain(scope) -> tuple[str, ...] | None:
    """The scope chain `scope` names (a string of names, or a list or tuple
    of names, as `html.parse_scope_chain` reads a header's), ``()`` for
    none (``None`` too, as Jinja's ``none`` is no scope); ``None`` when it is
    not one: not a string or a list of strings, more than ``MAX_SCOPES``
    names, or a name that is not one. An undefined Jinja variable is not
    ``None``, so a misspelt variable is refused, not read as no scope."""
    from scrud_django.ui import html
    from scrud_django.ui.html import scope_names

    if scope is None:
        return ()
    if isinstance(scope, (list, tuple)):
        if not all(isinstance(s, str) for s in scope):
            return None
        scope = " ".join(scope)
    if not isinstance(scope, str):
        return None
    chain = html.parse_scope_chain(scope)
    if not chain and scope_names(scope):
        return None  # names that are not a chain (``()`` is also no names)
    return chain


def _refused(path, reason) -> str:
    logger.warning("scrud-ui: embed of %r refused: %s", path, reason)
    return _notice(REFUSED_MESSAGE)


def _notice(message) -> str:
    return str(
        builtin_component("scrud:notice")
        .compiled()
        .render({"message": message, "level": "error"})
    )
