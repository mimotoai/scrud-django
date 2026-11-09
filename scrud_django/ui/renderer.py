"""The ``text/html`` representation of resources, at their own URLs.

`ScrudHTMLRenderer` renders what the view answered, as the JSON renderer
would: a success's body (a resource, a page of a list) through the render
tree, and an error as a notice, never with resource content. It is chosen by
content negotiation (``Accept: text/html``), ahead of DRF's browsable API,
which ``?format=api`` still reaches (`ui_renderers`).

A request with ``HX-Request`` gets a fragment; any other gets a page, the
fragment wrapped in the page component (the term ``scrud:page``, resolved in
the root scope). The fragment protocol's request headers (`SCOPE_HEADER`,
`POINTER_HEADER`) say which node to render and in which scope chain; they
select presentation only, never what is authorized.

The one response that renders something other than the view's data is a
refused PUT (400, 403): the body the user submitted comes back as the edit
form, with the messages, provided the user may PUT the stored resource as the
view loaded it (`remember_stored`, `load_stored`). A 412 answers a stale form
with a notice and a frame that reloads the current state.
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
from rest_framework.exceptions import APIException
from rest_framework.renderers import BaseRenderer, BrowsableAPIRenderer
from rest_framework.response import Response

from scrud_django.exceptions import UIHeaderInvalidError
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.permissions import content_of
from scrud_django.ui.authz import DENIED, decisions_for, refusal_messages
from scrud_django.ui.cascade import (
    BUILTIN_PREFIX,
    builtin_component,
    effective_rules,
    request_memo,
    resolve_component,
)
from scrud_django.ui.render import render_collection, render_resource
from scrud_django.ui.selectors import TypeTerm

logger = logging.getLogger(__name__)

CONTENT_TYPE = "text/html; charset=utf-8"

# The fragment protocol's request headers.
HX_REQUEST_HEADER = "HX-Request"
SCOPE_HEADER = "Scrud-UI-Scope"
POINTER_HEADER = "Scrud-UI-Pointer"
VARY = ("Accept", HX_REQUEST_HEADER, SCOPE_HEADER, POINTER_HEADER)

MAX_SCOPES = 16
MAX_POINTER_LENGTH = 1024
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
                body, contexts = _body(data, status, request, view, response)
                if not _is_htmx(request):
                    body = _page(
                        body,
                        request=request,
                        resource_type=_resource_type(view),
                        contexts=contexts,
                        scope_chain=scope_chain_of(request),
                    )
        except Exception:
            # The last resort: the response keeps its status, and shows no
            # content (nor a traceback).
            logger.exception(
                "scrud-ui: rendering %s failed", getattr(request, "path", "?")
            )
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
    and vary it on the fragment protocol's headers."""
    renderer = negotiated_html(request)
    if renderer is None:
        return
    patch_vary_headers(response, VARY)
    # The HTML depends on the stylesheets, components and grants as well as
    # the resource (whose ETag it carries): always revalidated, never shared.
    # A page and its htmx fragments share that ETag, so neither is stored
    # (a revalidated 304 could otherwise show one as the other).
    patch_cache_control(response, private=True, no_cache=True, no_store=True)
    if not isinstance(response, Response):
        return
    request.accepted_renderer = renderer
    request.accepted_media_type = renderer.media_type
    response.accepted_renderer = renderer
    response.accepted_media_type = renderer.media_type
    response.content_type = CONTENT_TYPE


def check_ui_headers(request) -> None:
    """Refuse (400) an HTML request whose `POINTER_HEADER` is not a JSON
    Pointer of at most `MAX_POINTER_LENGTH` characters: before the view
    acts, so a PUT carrying one writes nothing. An invalid `SCOPE_HEADER`
    is ignored (`scope_chain_of`)."""
    if negotiated_html(request) is None:
        return
    if pointer_of(request) is None:
        raise UIHeaderInvalidError(
            POINTER_HEADER,
            f"a JSON Pointer (RFC 6901) of at most {MAX_POINTER_LENGTH} characters"
            " is expected.",
        )


def scope_chain_of(request) -> tuple[str, ...]:
    """The root scope chain `SCOPE_HEADER` names: up to `MAX_SCOPES`
    space-separated names, each a letter then letters, digits, ``_`` or
    ``-``. Anything else is ignored: ``()``."""
    names = (_header(request, SCOPE_HEADER) or "").split()
    if len(names) > MAX_SCOPES or not all(_SCOPE_NAME.fullmatch(n) for n in names):
        return ()
    return tuple(names)


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


def load_stored(view, request, response) -> None:
    """For a refused PUT (400, 403) that negotiated HTML, refused before the
    view looked the resource up (scoped-rbac's request check refuses on the
    body): look it up now, through the view's own ``get_object()``, with its
    filter and object permission checks for the PUT, but without the row
    lock (``unlocked_lookups``): a refused request never waits for it or
    holds it. Refused, missing or not readable (a database error), nothing
    is remembered and the response is a notice."""
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
        # A savepoint: a failed lookup leaves the request's transaction
        # usable.
        with (
            transaction.atomic(using=db) if db else nullcontext(),
            unlocked() if unlocked is not None else nullcontext(),
        ):
            obj = view.get_object()
    except (Http404, DjangoPermissionDenied, APIException):
        return
    except (DatabaseError, transaction.TransactionManagementError):
        logger.warning(
            "scrud-ui: looking up %s for a refused PUT failed",
            getattr(request, "path", "?"),
            exc_info=True,
        )
        return
    remember_stored(view, request, obj)


def _body(data, status, request, view, response):
    """``(html, contexts)``: the fragment answering the request, and the
    contexts whose stylesheets style its page."""
    scope_chain = scope_chain_of(request)
    pointer = pointer_of(request)
    resource_type = _resource_type(view)
    method = getattr(request, "method", None)
    if status >= 400:
        problem = data if isinstance(data, Mapping) else {}
        if status == 412 and method == "PUT":
            return _stale(request, scope_chain, pointer or ""), []
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
        ), []
    if resource_type is not None and isinstance(data, Mapping):
        href = request.path
        if action == "create":
            location = response.get("Location") if response is not None else None
            href = urlsplit(location).path if location else href
        elif action not in ("retrieve", "update"):
            return _json(data), []
        etag = response.get("ETag") if response is not None else None
        return render_resource(
            data,
            resource_type=resource_type,
            href=href,
            etag=etag,
            request=request,
            scope_chain=scope_chain,
            pointer=pointer or "",
        ), rbac_contexts_of(data)
    return _json(data), []


def _refused_put(problem, status, request, view, resource_type, scope_chain, pointer):
    """A refused PUT: the submitted body as the edit form, with the problem's
    messages, when the user may PUT the stored resource the view loaded (at
    its ETag, with its decisions and contexts); else the notices only."""
    stored = view.__dict__.get(_STORED_ATTR) if view is not None else None
    body = _submitted(request)
    if resource_type is None or not isinstance(stored, Stored):
        return _notices(_problem_messages(problem, status))
    type_uri = resource_type.type_uri
    messages = []
    if status == 403 and body is not None:
        messages = refusal_messages(request, type_uri, body, stored.content)
    decisions = decisions_for(request, type_uri, stored.content)
    if body is None or decisions["PUT"] == DENIED:
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


def _stale(request, scope_chain, pointer):
    """A 412's notice, and a frame that replaces itself with the current
    state of the node, fetched in its scope chain."""
    headers = json.dumps({SCOPE_HEADER: " ".join(scope_chain), POINTER_HEADER: pointer})
    return mark_safe(
        _notices([STALE_MESSAGE])
        + format_html(
            '<div class="scrud-reload" hx-get="{}" hx-trigger="load"'
            ' hx-swap="outerHTML" hx-headers="{}"></div>',
            request.path,
            headers,
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


def _page(body, *, request, resource_type, contexts, scope_chain) -> SafeString:
    """`body` wrapped in the page component: the one `PAGE_TERM` resolves to
    in `scope_chain` for `contexts`, else the built-in. A stored page
    component that fails is replaced by the built-in."""

    def page_context():
        return {
            "title": _title(resource_type),
            "csrf_token": get_token(getattr(request, "_request", request)),
            "static": {
                "htmx": static(STATIC_HTMX),
                "scrud_ui": static(STATIC_SCRUD_UI),
            },
            "body": body,
        }

    component = resolve_component(
        effective_rules(list(contexts)), scope_chain, [PAGE_TERM], "view"
    )
    if component.name.startswith(BUILTIN_PREFIX) and component.name != PAGE_BUILTIN:
        component = builtin_component(PAGE_BUILTIN)
    if component.name == PAGE_BUILTIN:
        return component.compiled().render(page_context())
    try:
        return component.compiled().render(page_context())
    except Exception:
        logger.warning(
            "scrud-ui: page component %r failed to render",
            component.name,
            exc_info=True,
        )
    return format_html(
        '<!-- scrud-ui: component "{}" failed -->{}',
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
    """The view's resource type, or ``None`` (a view without one, or one
    whose type cannot be read)."""
    get_resource_type = getattr(view, "get_resource_type", None)
    if get_resource_type is None:
        return None
    try:
        return get_resource_type()
    except Exception:
        return None


def _envelope(data):
    """A list's body as `render_collection` reads it: the page envelope, or
    an unpaginated list of envelopes."""
    if isinstance(data, list):
        return {"count": len(data), "next": None, "previous": None, "content": data}
    return data


def _submitted(request):
    """The JSON object the request sent, or ``None``."""
    try:
        body = request.data
    except Exception:
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
    Pointer (``/items/0/text``), or ``None``."""
    if not isinstance(name, str) or not name.startswith("data"):
        return None
    rest, segments = name[len("data") :], []
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
