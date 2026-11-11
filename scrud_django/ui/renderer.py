"""The ``text/html`` representation of resources, at their own URLs.

`ScrudHTMLRenderer` renders what the view answered, as the JSON renderer
would: a success's body (a resource, a page of a list) through the render
tree, and an error as a notice, never with stored resource content (but see
the refused PUT and the 412, below). It is chosen by content negotiation
(``Accept: text/html``), ahead of DRF's browsable API, which ``?format=api``
still reaches (`ui_renderers`).

A request with ``HX-Request`` gets a fragment; any other gets a page, the
fragment wrapped in the page component (the term ``scrud:page``, resolved in
the root scope). The fragment protocol's request headers (`SCOPE_HEADER`,
`POINTER_HEADER`) say which node to render and in which scope chain; they
select presentation only, never what is authorized.

The errors that render more than a notice: a refused PUT (400, 403), where
the body the user submitted (never the stored content) comes back as the edit
form, with the messages, provided the user may PUT the stored resource as the
view loaded it (`remember_stored`, `load_stored`); and a 412 of a PUT or
DELETE, or a 409 DELETE (in use), which answer with a notice and a frame that
reloads the current state (a GET with the viewer's own rights), so that the
item never looks deleted. A success that fails to render is a 500 with a
notice.
"""

import copy
import json
import logging
import re
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import urlsplit

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db import DatabaseError, transaction
from django.http import Http404, QueryDict
from django.middleware.csrf import get_token
from django.templatetags.static import static
from django.utils.cache import patch_cache_control, patch_vary_headers
from django.utils.html import format_html
from django.utils.http import quote_etag
from django.utils.safestring import SafeString, mark_safe
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    NotAuthenticated,
    NotFound,
    ParseError,
    PermissionDenied,
    UnsupportedMediaType,
)
from rest_framework.renderers import BaseRenderer, BrowsableAPIRenderer
from rest_framework.response import Response

from scrud_django.exceptions import UIHeaderInvalidError
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.permissions import content_of
from scrud_django.ui.authz import ALLOWED, DENIED, decisions_for, refusal_messages
from scrud_django.ui.branches.chrome import SAFE_NOTICE, banner, confirm_page
from scrud_django.ui.branches.css import css_urls
from scrud_django.ui.branches.effective import (
    BRANCH_HEADER,
    SAFE_HEADER,
    query_param,
    safe_mode,
    safe_mode_requested,
    using_safe_mode,
    using_selection,
)
from scrud_django.ui.branches.js import js_urls
from scrud_django.ui.branches.selection import (
    CONFIRM_PARAMETER,
    PARAM,
    Chosen,
    choose,
    cookie_branch,
    cookie_name,
    cookie_path,
    frame_branch,
    in_mount,
    is_published,
    selection_value,
)
from scrud_django.ui.cascade import (
    BUILTIN_PREFIX,
    builtin_component,
    effective_rules,
    request_memo,
    resolve_component,
)
from scrud_django.ui.embed import embed_for
from scrud_django.ui.render import JINJA_ENGINE, render_collection, render_resource
from scrud_django.ui.selectors import TypeTerm

logger = logging.getLogger(__name__)

CONTENT_TYPE = "text/html; charset=utf-8"

# The fragment protocol's request headers.
HX_REQUEST_HEADER = "HX-Request"
SCOPE_HEADER = "Scrud-UI-Scope"
POINTER_HEADER = "Scrud-UI-Pointer"
MODE_HEADER = "Scrud-UI-Mode"
# The scope chain and the mode may also come in the URL (a header wins).
SCOPE_PARAMETER = "ui-scope"
MODE_PARAMETER = "ui-mode"
VIEW_MODE = "view"
# The branch a page browses comes from a header, else a parameter (in the
# URL), else a cookie.
VARY = (
    "Accept",
    HX_REQUEST_HEADER,
    SCOPE_HEADER,
    POINTER_HEADER,
    MODE_HEADER,
    BRANCH_HEADER,
    SAFE_HEADER,
    "Cookie",
)

MAX_SCOPES = 16
MAX_POINTER_LENGTH = 1024
_SCOPE_SEPARATORS = re.compile(r"[\s,]+")
_SCOPE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
# RFC 6901: "" or "/"-prefixed reference tokens, "~" only as "~0" or "~1".
_POINTER = re.compile(r"(?:/(?:[^~/]|~[01])*)*")

PAGE_TERM = TypeTerm(iri="scrud:page")
PAGE_BUILTIN = "scrud:page"
STATIC_HTMX = "scrud_ui/htmx.min.js"
STATIC_SCRUD_UI = "scrud_ui/scrud-ui.js"

STALE_MESSAGE = "This item changed since you opened it."
RENDER_FAILED_MESSAGE = "Something went wrong rendering this page."

# On the request: the ScrudHTMLRenderer content negotiation chose, else None.
_NEGOTIATED_ATTR = "_scrud_ui_html_renderer"
# On the view: the `Stored` resource a PUT replaces, as get_instance() (or
# `load_stored`) first loaded it through the view's checks.
_STORED_ATTR = "_scrud_ui_stored"

# What fastjsonschema names a value it refuses: ``data.items[0].text``.
_SCHEMA_NAME_TOKEN = re.compile(r"\.([^.\[]+)|\[(\d+)\]")


@dataclass(frozen=True)
class Stored:
    """The resource a PUT replaces, as the view loaded it before the write:
    a copy of its content, and its ETag (as the response's ``ETag``)."""

    content: dict
    etag: str


class ScrudHTMLRenderer(BaseRenderer):
    """Resources as HTML: pages, htmx fragments and error notices."""

    media_type = "text/html"
    format = "html"
    charset = "utf-8"

    def render(self, data, accepted_media_type=None, renderer_context=None):
        context = renderer_context or {}
        request = context.get("request")
        view = context.get("view")
        response = context.get("response")
        status = getattr(response, "status_code", 200)
        if status < 400 and (data is None or status in (204, 304)):
            return b""
        try:
            with request_memo():
                chosen = choose(request)
                if _asks_confirmation(chosen, request):
                    return str(confirm_page(chosen, request)).encode(self.charset)
                # An embedded request (`embed`) stays in its embedder's safe
                # mode: it is only ever turned on here (and, for the CSS
                # and JS file responses, in `branches.files`).
                with (
                    using_selection(chosen.selection),
                    using_safe_mode(safe_mode_requested(request) or safe_mode()),
                ):
                    body, contexts = _body(data, status, request, view, response)
                    if not _is_htmx(request):
                        body = _page(
                            body,
                            request=request,
                            resource_type=_resource_type(view),
                            contexts=contexts,
                            scope_chain=scope_chain_of(request),
                            chosen=chosen,
                        )
        except Exception:
            # The last resort: a notice, with no content (nor a traceback). A
            # success becomes a 500, so that htmx treats it as an error; an
            # error keeps its status.
            logger.exception(
                "scrud-ui: rendering %s (%s, status %s) failed",
                getattr(request, "path", "?"),
                type(view).__name__,
                status,
            )
            if response is not None and status < 400:
                response.status_code = 500
            body = _notices([RENDER_FAILED_MESSAGE])
        return str(body).encode(self.charset)


def ui_renderers(renderers: list) -> list:
    """`renderers` with a `ScrudHTMLRenderer` immediately before DRF's
    ``BrowsableAPIRenderer``, else last (unless one is there already)."""
    renderers = list(renderers)
    if any(isinstance(r, ScrudHTMLRenderer) for r in renderers):
        return renderers
    for index, renderer in enumerate(renderers):
        if isinstance(renderer, BrowsableAPIRenderer):
            renderers.insert(index, ScrudHTMLRenderer())
            return renderers
    renderers.append(ScrudHTMLRenderer())
    return renderers


def note_negotiation(request, renderer) -> None:
    """Record whether content negotiation chose HTML for `request`: an
    exception handler may swap the accepted renderer for a JSON one
    afterwards (scoped-rbac's and scrudful's problem details do)."""
    setattr(
        request,
        _NEGOTIATED_ATTR,
        renderer if isinstance(renderer, ScrudHTMLRenderer) else None,
    )


def negotiated_html(request):
    """The `ScrudHTMLRenderer` content negotiation chose for `request`, or
    ``None``."""
    return getattr(request, _NEGOTIATED_ATTR, None)


def finalize_html(request, response) -> None:
    """For a request that negotiated HTML: render `response` with the
    `ScrudHTMLRenderer`, as ``text/html; charset=utf-8``, whatever renderer
    and media type an exception handler set (``application/problem+json``),
    vary it on the fragment protocol's headers, and keep caches from storing
    it (``Cache-Control: private, no-cache, no-store``). A GET whose
    branch parameter came confirmed keeps the selection in the prefix's
    cookie (`remember_branch`)."""
    renderer = negotiated_html(request)
    if renderer is None:
        return
    patch_vary_headers(response, VARY)
    remember_branch(request, response)
    # The HTML depends on the stylesheets, components and grants as well as
    # the resource, whose ETag it carries; and a page and its htmx fragments
    # share that ETag, so a stored copy revalidated with a 304 could show one
    # as the other, or stale presentation. So no cache stores it (no-store;
    # private and no-cache for caches that ignore no-store). A client that
    # resends an old ETag itself still gets a 304 (ui-html-shared-etag in
    # docs/follow-ups.md).
    patch_cache_control(response, private=True, no_cache=True, no_store=True)
    if not isinstance(response, Response):
        return
    request.accepted_renderer = renderer
    request.accepted_media_type = renderer.media_type
    response.accepted_renderer = renderer
    response.accepted_media_type = renderer.media_type
    response.content_type = CONTENT_TYPE


def remember_branch(request, response) -> None:
    """For a GET whose branch parameter came confirmed (with the viewer's
    confirmation token: the banner's links and the confirmation's): set
    the prefix's cookie (`selection.cookie_name`, scoped to the prefix by
    `selection.cookie_path`; ``HttpOnly``, ``SameSite=Lax``) to the working
    branch it selected, or delete it for the published branch.
    A refused branch leaves the cookie as it was. The cookie is only a
    preference: every request checks it again (`choose`)."""
    params = getattr(request, "GET", None)
    if (
        getattr(request, "method", None) not in ("GET", "HEAD")
        or params is None
        or CONFIRM_PARAMETER not in params
    ):
        return
    chosen = choose(request)
    if chosen.source != PARAM or not chosen.confirmed or chosen.refused:
        return
    name, path = cookie_name(request), cookie_path(request)
    if is_published(chosen, request):
        response.delete_cookie(name, path=path, samesite="Lax")
        return
    response.set_cookie(
        name,
        selection_value(chosen.selection),
        path=path,
        secure=request.is_secure(),
        httponly=True,
        samesite="Lax",
    )


def check_ui_headers(request) -> None:
    """Refuse (400) an HTML request whose `POINTER_HEADER` is not a JSON
    Pointer of at most `MAX_POINTER_LENGTH` characters, or whose requested
    mode (`MODE_HEADER`, `MODE_PARAMETER`) is not ``"view"``: before the view
    acts, so a PUT carrying one writes nothing. An invalid scope chain
    (`SCOPE_HEADER`, `SCOPE_PARAMETER`) is ignored (`scope_chain_of`)."""
    if negotiated_html(request) is None:
        return
    if pointer_of(request) is None:
        raise UIHeaderInvalidError(
            POINTER_HEADER,
            f"a JSON Pointer (RFC 6901) of at most {MAX_POINTER_LENGTH} characters"
            " is expected.",
        )
    mode = _requested_mode_value(request)
    if mode is not None and mode != VIEW_MODE:
        raise UIHeaderInvalidError(
            MODE_HEADER, f'"{VIEW_MODE}" is the only mode that can be requested.'
        )


def scope_chain_of(request) -> tuple[str, ...]:
    """The root scope chain `SCOPE_HEADER` names, else `SCOPE_PARAMETER`:
    up to `MAX_SCOPES` space- or comma-separated names, each a letter then
    letters, digits, ``_`` or ``-``. Anything else is ignored: ``()``."""
    value = _header(request, SCOPE_HEADER)
    if value is None:
        value = query_param(request, SCOPE_PARAMETER)
    return parse_scope_chain(value)


def parse_scope_chain(value: str | None) -> tuple[str, ...]:
    """The scope chain `value` names, as `scope_chain_of` reads it; ``()``
    when it is not one."""
    names = scope_names(value)
    if len(names) > MAX_SCOPES or not all(_SCOPE_NAME.fullmatch(n) for n in names):
        return ()
    return tuple(names)


def scope_names(value: str | None) -> list[str]:
    """The names in `value`, split as a scope chain is (on whitespace and
    commas), valid or not."""
    return [n for n in _SCOPE_SEPARATORS.split(value or "") if n]


def requested_mode_of(request) -> str | None:
    """The mode the request asks for (`MODE_HEADER`, else `MODE_PARAMETER`):
    ``"view"``, or ``None`` when absent. It can only lower what the user may
    do. Any other value is refused by `check_ui_headers`, so it reads as
    ``None`` here."""
    value = _requested_mode_value(request)
    return VIEW_MODE if value == VIEW_MODE else None


def _requested_mode_value(request) -> str | None:
    value = _header(request, MODE_HEADER)
    if value is None:
        value = query_param(request, MODE_PARAMETER)
    return value


def pointer_of(request) -> str | None:
    """The JSON Pointer `POINTER_HEADER` names (``""``, the resource, without
    one), or ``None`` when it is invalid."""
    value = _header(request, POINTER_HEADER)
    if value is None:
        return ""
    if len(value) > MAX_POINTER_LENGTH or not _POINTER.fullmatch(value):
        return None
    return value


def remember_stored(view, request, obj) -> None:
    """Keep `obj`, the resource a PUT that negotiated HTML replaces, as the
    view's checked lookup first returned it (its ``get_instance``), as
    `Stored`: if the write is refused, whether the submitted body comes back
    as the edit form, and how, is decided on it. Kept once per request, and
    a copy, since the write changes the instance."""
    if (
        request.method == "PUT"
        and negotiated_html(request) is not None
        and _STORED_ATTR not in view.__dict__
    ):
        view.__dict__[_STORED_ATTR] = Stored(
            content=copy.deepcopy(content_of(obj)), etag=quote_etag(obj.etag)
        )


# What a lookup refused to the user raises: 401, 403, 404.
_LOOKUP_REFUSALS = (
    Http404,
    DjangoPermissionDenied,
    NotAuthenticated,
    AuthenticationFailed,
    PermissionDenied,
    NotFound,
)


def load_stored(view, request, response) -> None:
    """For a refused PUT (400, 403) that negotiated HTML, refused before the
    view looked the resource up (scoped-rbac's request check refuses on the
    body): look it up now, through the view's own ``get_object()``, with its
    filter and object permission checks for the PUT, but without the row
    lock (``unlocked_lookups``): a refused request never waits for it or
    holds it. Refused (401, 403, 404), or not readable (a database error,
    another API error, logged), nothing is remembered and the response is
    a notice."""
    if (
        request.method != "PUT"
        or negotiated_html(request) is None
        or getattr(response, "status_code", None) not in (400, 403)
        or _STORED_ATTR in view.__dict__
        or pointer_of(request) is None
    ):
        return
    db = getattr(view, "locked_write_db", None)
    unlocked = getattr(view, "unlocked_lookups", None)
    try:
        # A savepoint only when the view names a write database (`db`): a
        # failed lookup then leaves the request's transaction usable. Without
        # one the lookup runs in the request's own transaction.
        with (
            transaction.atomic(using=db) if db else nullcontext(),
            unlocked() if unlocked is not None else nullcontext(),
        ):
            obj = view.get_object()
    except _LOOKUP_REFUSALS:
        return
    except (APIException, DatabaseError, transaction.TransactionManagementError):
        logger.warning(
            "scrud-ui: looking up %s for a refused PUT failed",
            getattr(request, "path", "?"),
            exc_info=True,
        )
        return
    remember_stored(view, request, obj)


# What a computed view's content offers: reading only.
COMPUTED_DECISIONS = {"GET": ALLOWED, "PUT": DENIED, "DELETE": DENIED}


def _body(data, status, request, view, response):
    """``(html, contexts)``: the fragment answering the request, and the
    contexts whose stylesheets style its page."""
    scope_chain = scope_chain_of(request)
    pointer = pointer_of(request)
    mode = requested_mode_of(request)
    resource_type = _resource_type(view)
    method = getattr(request, "method", None)
    if status >= 400:
        problem = data if isinstance(data, Mapping) else {}
        if status == 412 and method in ("PUT", "DELETE"):
            return _reloading(request, scope_chain, pointer or "", [STALE_MESSAGE]), []
        if status == 409 and method == "DELETE":
            # The resource is still there: never a bare notice in place of its
            # frame, which would make it look deleted.
            messages = _problem_messages(problem, status)
            return _reloading(request, scope_chain, pointer or "", messages), []
        if status in (400, 403) and method == "PUT" and pointer is not None:
            return _refused_put(
                problem, status, request, view, resource_type, scope_chain, pointer
            ), []
        return _notices(_problem_messages(problem, status)), []
    action = getattr(view, "action", None)
    if resource_type is not None and action == "list":
        return render_collection(
            _envelope(data),
            resource_type=resource_type,
            href=request.path,
            request=request,
            scope_chain=scope_chain,
            mode=mode,
        ), []
    if resource_type is not None and isinstance(data, Mapping):
        href = request.path
        if action == "create":
            location = response.get("Location") if response is not None else None
            href = in_mount(request, urlsplit(location).path) if location else href
        elif action not in ("retrieve", "update"):
            return _json(data), []
        etag = response.get("ETag") if response is not None else None
        # A computed view's content is view-only, its contexts the content's
        # own when it names them (never the stored resource's decisions).
        computed = getattr(view, "computed", False)
        contexts = (
            (rbac_contexts_of(data) if "rbac_context" in data else [])
            if computed
            else rbac_contexts_of(data)
        )
        return render_resource(
            data,
            resource_type=resource_type,
            href=href,
            etag=etag,
            request=request,
            scope_chain=scope_chain,
            pointer=pointer or "",
            mode=mode,
            **(
                {"decisions": COMPUTED_DECISIONS, "contexts": contexts}
                if computed
                else {}
            ),
        ), contexts
    return _json(data), []


def _refused_put(problem, status, request, view, resource_type, scope_chain, pointer):
    """A refused PUT: the submitted body as the edit form, with the problem's
    messages, only when the user may read the stored resource the view loaded
    (decision for GET allowed) and may not be denied a PUT of it (at its ETag,
    with its decisions and contexts); else the notices only, with no form and
    no ETag, as JSON would reveal neither."""
    stored = view.__dict__.get(_STORED_ATTR) if view is not None else None
    body = _submitted(request)
    if resource_type is None or not isinstance(stored, Stored):
        return _notices(_problem_messages(problem, status))
    type_uri = resource_type.type_uri
    messages = []
    if status == 403 and body is not None:
        messages = refusal_messages(request, type_uri, body, stored.content)
    decisions = decisions_for(request, type_uri, stored.content)
    if body is None or decisions["GET"] != ALLOWED or decisions["PUT"] == DENIED:
        return _notices(_problem_messages(problem, status) + messages)
    errors = _field_errors(problem)
    errors[""] = [_problem_message(problem, status), *errors.get("", []), *messages]
    # Only the body's values come from the request: what the user may do
    # (the Delete button), the stylesheets' contexts and the ETag the form
    # writes at (never an If-Match of "*") are the stored resource's.
    return render_resource(
        body,
        resource_type=resource_type,
        href=request.path,
        etag=stored.etag,
        request=request,
        scope_chain=scope_chain,
        pointer=pointer,
        errors=errors,
        mode="edit",
        decisions=decisions,
        contexts=rbac_contexts_of(stored.content),
    )


def _reloading(request, scope_chain, pointer, messages):
    """The notices of a write that left the resource as it was (a 412, or a
    409 DELETE), and a frame that replaces itself with the current state of the
    node, fetched in its scope chain."""
    headers = {SCOPE_HEADER: " ".join(scope_chain), POINTER_HEADER: pointer}
    branch = frame_branch(request)
    if branch is not None:
        headers[BRANCH_HEADER] = branch
    return mark_safe(
        _notices(messages)
        + format_html(
            '<div class="scrud-reload" hx-get="{}" hx-trigger="load"'
            ' hx-swap="outerHTML" hx-headers="{}"></div>',
            request.path,
            json.dumps(headers),
        )
    )


def _notices(messages) -> SafeString:
    notice = builtin_component("scrud:notice").compiled()
    return format_html(
        '<div class="scrud-notices">{}</div>',
        mark_safe(
            "".join(notice.render({"message": m, "level": "error"}) for m in messages)
        ),
    )


def _json(data) -> SafeString:
    """`data` that is no resource and no list, as JSON (``scrud:json``)."""
    value_json = json.dumps(
        data, indent=2, sort_keys=True, ensure_ascii=False, default=str
    )
    json_component = builtin_component("scrud:json").compiled()
    return json_component.render({"value_json": value_json, "errors": []})


def _asks_confirmation(chosen: Chosen, request) -> bool:
    """Whether a full-page GET asks before showing `chosen`'s branch: its
    parameter (a link) selected a working branch other than the cookie's,
    without the viewer's confirmation token. A fragment request never
    asks."""
    return (
        chosen.source == PARAM
        and not chosen.confirmed
        and getattr(request, "method", None) in ("GET", "HEAD")
        and not _is_htmx(request)
        and not is_published(chosen, request)
        and chosen.selection.branch != cookie_branch(request)
    )


def _page(
    body, *, request, resource_type, contexts, scope_chain, chosen=None
) -> SafeString:
    """`body` wrapped in the page component: the one `PAGE_TERM` resolves to
    in `scope_chain` for `contexts`, else the built-in. A stored page
    component that fails is replaced by the built-in.

    Off the published branch, or when it refused a branch, `chosen`'s banner
    is put at the top of the body, and in safe mode its notice (`SAFE_NOTICE`).
    So is the page's branch (`frame_branch`)
    when it has one, on a hidden ``.scrud-branch-page`` element, for the
    requests of elements outside any frame; in safe mode the element is
    there with ``data-scrud-safe="1"``, so that those requests send
    `SAFE_HEADER`. A page component that leaves
    them out is replaced by the built-in. (Branch CSS, or a component that
    wraps the body in a hidden element, can still hide the banner: follow-up
    ``ui-banner-hideable``.)

    The page component's context also has ``css``: the URLs of the CSS files
    effective on the page's branch, in order, and ``scripts``: those of its
    JS files (empty when it has none); a stored Jinja one's also has
    ``embed`` (`embed.embed_for`)."""
    chrome = []
    branch = frame_branch(request) if chosen is not None else None
    safe = safe_mode()
    if branch is not None or safe:
        chrome.append(
            format_html(
                '<div class="scrud-branch-page"{}{} hidden></div>',
                format_html(' data-scrud-branch="{}"', branch)
                if branch is not None
                else "",
                mark_safe(' data-scrud-safe="1"') if safe else "",
            )
        )
    if chosen is not None and (chosen.refused or not is_published(chosen, request)):
        chrome.append(banner(chosen, request))
    if safe:
        chrome.append(SAFE_NOTICE)
    chrome = mark_safe("".join(chrome)) if chrome else None
    if chrome is not None:
        body = mark_safe(chrome + body)

    css = css_urls(request)
    scripts = js_urls(request)

    def page_context(component=None):
        context = {
            "title": _title(resource_type),
            "css": css,
            "scripts": scripts,
            "csrf_token": get_token(getattr(request, "_request", request)),
            "static": {
                "htmx": static(STATIC_HTMX),
                "scrud_ui": static(STATIC_SCRUD_UI),
            },
            "body": body,
        }
        if getattr(component, "engine", None) == JINJA_ENGINE:
            context["embed"] = embed_for(request)
        return context

    component = resolve_component(
        effective_rules(list(contexts)), scope_chain, [PAGE_TERM], "view"
    )
    if component.name.startswith(BUILTIN_PREFIX) and component.name != PAGE_BUILTIN:
        component = builtin_component(PAGE_BUILTIN)
    if component.name == PAGE_BUILTIN:
        return component.compiled().render(page_context())
    comment = '<!-- scrud-ui: component "{}" failed -->{}'
    try:
        page = component.compiled().render(page_context(component))
    except Exception:
        logger.warning(
            "scrud-ui: page component %r failed to render",
            component.name,
            exc_info=True,
        )
    else:
        if chrome is None or chrome in page:
            return page
        logger.warning(
            "scrud-ui: page component %r leaves out the branch banner or the"
            " safe mode notice; the built-in page is used",
            component.name,
        )
        # It rendered: said as what it is, not as a failure.
        comment = (
            '<!-- scrud-ui: page component "{}" leaves out the branch banner'
            " or the safe mode notice -->{}"
        )
    return format_html(
        comment,
        component.name,
        builtin_component(PAGE_BUILTIN).compiled().render(page_context()),
    )


def _title(resource_type) -> str:
    """The page title: the type's schema ``title``, else its slug."""
    if resource_type is None:
        return ""
    schema = getattr(resource_type.schema, "content", None)
    title = schema.get("title") if isinstance(schema, Mapping) else None
    if isinstance(title, str) and title:
        return title
    return resource_type.slug or ""


def _resource_type(view):
    """The view's resource type, or ``None`` (a view without one, or whose
    type is not registered: a 404). Any other error reading it propagates
    (to `ScrudHTMLRenderer.render`'s last resort, which logs it)."""
    get_resource_type = getattr(view, "get_resource_type", None)
    if get_resource_type is None:
        return None
    try:
        return get_resource_type()
    except Http404:
        return None


def _envelope(data):
    """A list's body as `render_collection` reads it: the page envelope, or
    an unpaginated list of envelopes."""
    if isinstance(data, list):
        return {"count": len(data), "next": None, "previous": None, "content": data}
    return data


def _submitted(request):
    """The JSON object the request sent, or ``None`` (none, or one its
    parser refused)."""
    try:
        body = request.data
    except (ParseError, UnsupportedMediaType):
        return None
    # DRF leaves an empty QueryDict where a body failed to parse.
    if isinstance(body, QueryDict) or not isinstance(body, Mapping):
        return None
    return body


def _is_htmx(request) -> bool:
    return _header(request, HX_REQUEST_HEADER) == "true"


def _header(request, name):
    """Header `name` of `request` (``None`` if absent), read as UTF-8: the
    server decodes header bytes as ISO-8859-1."""
    headers = getattr(request, "headers", None)
    value = headers.get(name) if headers is not None else None
    if value is None:
        return None
    try:
        return value.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return value


def _problem_message(problem, status) -> str:
    """The problem's ``title``, with its ``detail`` when that says more."""
    title = problem.get("title")
    if not isinstance(title, str) or not title:
        try:
            title = HTTPStatus(status).phrase
        except ValueError:
            title = f"Error {status}"
    detail = problem.get("detail")
    if not isinstance(detail, str) or not detail:
        return title
    if detail.rstrip(".").casefold() == title.casefold():
        return title
    return f"{title}: {detail}"


def _problem_messages(problem, status) -> list[str]:
    """The problem's message, then each of its field errors."""
    messages = [_problem_message(problem, status)]
    for pointer, found in _field_errors(problem).items():
        messages.extend(f"{pointer}: {m}" if pointer else m for m in found)
    return messages


def _field_errors(problem) -> dict[str, list[str]]:
    """``{JSON Pointer: [message]}`` of the problem's ``errors`` (keyed by
    dotted path, scoped-rbac's validation problem), and of the value a JSON
    Schema refused (scrud's ``name``, fastjsonschema's ``data.items[0]``)."""
    errors: dict[str, list[str]] = {}
    raw = problem.get("errors")
    if isinstance(raw, Mapping):
        for path, found in raw.items():
            pointer = _dotted_pointer(str(path))
            for item in found if isinstance(found, list) else [found]:
                message = item.get("message") if isinstance(item, Mapping) else item
                if message:
                    errors.setdefault(pointer, []).append(str(message))
    pointer = _schema_name_pointer(problem.get("name"))
    detail = problem.get("detail")
    if pointer and isinstance(detail, str) and detail:
        name = problem["name"]
        message = detail[len(name) :].strip() if detail.startswith(name) else detail
        errors.setdefault(pointer, []).append(message or detail)
    return errors


def _dotted_pointer(path: str) -> str:
    """Dotted path ``items.0.text`` as the JSON Pointer ``/items/0/text``."""
    if not path:
        return ""
    return "".join("/" + _escape(segment) for segment in path.split("."))


def _schema_name_pointer(name) -> str | None:
    """fastjsonschema's name of a value (``data.items[0].text``) as a JSON
    Pointer (``/items/0/text``), or ``None``. A name without the leading
    ``data`` (``rules[1].select``, a selector error's) is accepted too."""
    if not isinstance(name, str) or not name:
        return None
    segments = []
    if re.match(r"data(?=[.\[]|$)", name):
        rest = name[len("data") :]
    else:
        rest = "." + name
    while rest:
        match = _SCHEMA_NAME_TOKEN.match(rest)
        if match is None:
            return None
        segments.append(
            match.group(1) if match.group(1) is not None else match.group(2)
        )
        rest = rest[match.end() :]
    return "".join("/" + _escape(s) for s in segments) or None


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")
