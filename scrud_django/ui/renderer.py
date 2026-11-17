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
import logging
from collections.abc import Mapping
from contextlib import nullcontext

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db import DatabaseError, transaction
from django.http import Http404, QueryDict
from django.utils.http import quote_etag
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
from scoped_rbac import access

from scrud_django.exceptions import ScrudException, UIHeaderInvalidError
from scrud_django.permissions import content_of

# What the neutral half defines, still importable from here.
from scrud_django.ui.html import (  # noqa: F401
    CONTENT_TYPE,
    HX_REQUEST_HEADER,
    MAX_POINTER_LENGTH,
    MAX_SCOPES,
    MODE_HEADER,
    MODE_PARAMETER,
    POINTER_HEADER,
    RENDER_FAILED_MESSAGE,
    SCOPE_HEADER,
    SCOPE_PARAMETER,
    STALE_MESSAGE,
    STATIC_HTMX,
    STATIC_SCRUD_UI,
    VARY,
    VIEW_MODE,
    Stored,
    _notices,
    _requested_mode_value,
    html_headers,
    parse_scope_chain,
    pointer_of,
    remember_branch,
    render_html,
    requested_mode_of,
    scope_chain_of,
    scope_names,
)

logger = logging.getLogger(__name__)

# On the request: the ScrudHTMLRenderer content negotiation chose, else None.
_NEGOTIATED_ATTR = "_scrud_ui_html_renderer"
# On the view: the `Stored` resource a PUT replaces, as get_instance() (or
# `load_stored`) first loaded it through the view's checks.
_STORED_ATTR = "_scrud_ui_stored"

# The ScrudHTMLRenderer is the shim of the API's HTML until the API stops
# negotiating it: it derives `render_html`'s arguments from the view, the
# response and the request, and the neutral half does the rest.


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
            body = render_html(
                _django_request(request),
                **_arguments(data, status, request, view, response),
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


def _django_request(request):
    """The Django request under a DRF one, carrying over the effective policy
    scoped-rbac cached on the DRF request (``policy_for`` sets it there, not
    on the Django request), so that rendering asks for the user's grants no
    more often than the view did."""
    http = getattr(request, "_request", request)
    if http is not request:
        cached = getattr(request, access._POLICY_CACHE_ATTR, None)
        if cached is not None:
            setattr(http, access._POLICY_CACHE_ATTR, cached)
    return http


def _arguments(data, status, request, view, response) -> dict:
    """`render_html`'s arguments (but the request) for what the view
    answered."""
    action = getattr(view, "action", None)
    kind = {"retrieve": "item", "update": "item"}.get(action, action or "")
    if getattr(view, "computed", False) and kind == "item":
        kind = "computed"
    arguments = {
        "resource_type": _resource_type(view),
        "kind": kind,
        "data": data,
        "status": status,
        "etag": response.get("ETag") if response is not None else None,
        "href": request.path,
        "location": response.get("Location") if response is not None else None,
    }
    if status < 400 and kind in ("item", "create") and isinstance(data, Mapping):
        is_record = getattr(view, "is_record", None)
        arguments["is_record"] = bool(is_record is not None and is_record(data))
    if status in (400, 403) and request.method == "PUT":
        arguments["stored"] = view.__dict__.get(_STORED_ATTR) if view else None
        arguments["submitted"] = _submitted(request)
    return arguments


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
    with the headers of any HTML response (`html_headers`)."""
    renderer = negotiated_html(request)
    if renderer is None:
        return
    html_headers(_django_request(request), response)
    if not isinstance(response, Response):
        return
    request.accepted_renderer = renderer
    request.accepted_media_type = renderer.media_type
    response.accepted_renderer = renderer
    response.accepted_media_type = renderer.media_type
    response.content_type = CONTENT_TYPE


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
    except (
        APIException,
        ScrudException,
        DatabaseError,
        transaction.TransactionManagementError,
    ):
        logger.warning(
            "scrud-ui: looking up %s for a refused PUT failed",
            getattr(request, "path", "?"),
            exc_info=True,
        )
        return
    remember_stored(view, request, obj)


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
