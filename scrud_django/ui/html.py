"""The neutral half of the HTML representation: `render_html` and what it
reads of a plain Django request.

`render_html` answers a request with a page, or with a fragment for an htmx
request (the fragment protocol, `ScrudHTMLRenderer`'s docstring in
`scrud_django.ui.renderer`): a resource, a page of a list or an error as a
notice, through the render tree. It takes plain arguments (the resource type,
the kind of answer, the data, the ETag, the stored content of a refused PUT)
and a Django ``HttpRequest``, and imports no DRF; the DRF renderer derives
its arguments from the view and the response. `html_headers` is what any HTML
response carries: ``Vary``, ``no-store`` and the branch cookie.
"""

import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from urllib.parse import urlsplit

from django.apps import apps
from django.middleware.csrf import get_token
from django.templatetags.static import static
from django.utils.cache import patch_cache_control, patch_vary_headers
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe

from scrud_django.exceptions import UIHeaderInvalidError
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.ui.authz import ALLOWED, DENIED, decisions_for, refusal_messages
from scrud_django.ui.branches.chrome import SAFE_NOTICE, banner, confirm_page
from scrud_django.ui.branches.effective import (
    BRANCH_HEADER,
    SAFE_HEADER,
    query_param,
    safe_mode,
    safe_mode_requested,
    using_safe_mode,
    using_selection,
)
from scrud_django.ui.branches.file_urls import css_urls, js_urls
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
from scrud_django.ui.embed import component_globals
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

# The app whose links (`studio.links`) the pages of the UI's types carry.
STUDIO_APP = "scrud_django.ui.studio"

PAGE_TERM = TypeTerm(iri="scrud:page")
PAGE_BUILTIN = "scrud:page"
STATIC_HTMX = "scrud_ui/htmx.min.js"
STATIC_SCRUD_UI = "scrud_ui/scrud-ui.js"

STALE_MESSAGE = "This item changed since you opened it."
RENDER_FAILED_MESSAGE = "Something went wrong rendering this page."

# What fastjsonschema names a value it refuses: ``data.items[0].text``.
_SCHEMA_NAME_TOKEN = re.compile(r"\.([^.\[]+)|\[(\d+)\]")


@dataclass(frozen=True)
class Stored:
    """The resource a PUT replaces, as the view loaded it before the write:
    a copy of its content, and its ETag (as the response's ``ETag``)."""

    content: dict
    etag: str


# What a computed view's content offers: reading only.
COMPUTED_DECISIONS = {"GET": ALLOWED, "PUT": DENIED, "DELETE": DENIED}


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


def check_ui_headers(request) -> None:
    """Refuse (`UIHeaderInvalidError`, 400) a request whose `POINTER_HEADER`
    is not a JSON Pointer of at most `MAX_POINTER_LENGTH` characters, or
    whose requested mode (`MODE_HEADER`, `MODE_PARAMETER`) is not
    ``"view"``: before anything is read or written, so a PUT carrying one
    writes nothing. An invalid scope chain (`SCOPE_HEADER`,
    `SCOPE_PARAMETER`) is ignored (`scope_chain_of`)."""
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


def html_headers(request, response) -> None:
    """Make `response` an HTML response: vary it on the fragment protocol's
    headers (`VARY`), keep caches from storing it (``Cache-Control: private,
    no-cache, no-store``) and, for a GET whose branch parameter came
    confirmed, keep the selection in the prefix's cookie (`remember_branch`).
    The HTML depends on the stylesheets, components and grants as well as
    the resource, whose ETag it carries; and a page and its htmx fragments
    share that ETag, so a stored copy revalidated with a 304 could show one
    as the other, or stale presentation. So no cache stores it (no-store;
    private and no-cache for caches that ignore no-store). A client that
    resends an old ETag itself still gets a 304 (ui-html-shared-etag in
    docs/follow-ups.md)."""
    patch_vary_headers(response, VARY)
    remember_branch(request, response)
    patch_cache_control(response, private=True, no_cache=True, no_store=True)


def render_html(
    request,
    *,
    resource_type,
    kind: str,
    data,
    status: int,
    etag: str | None,
    href: str,
    location: str | None = None,
    is_record: bool = False,
    stored: Stored | None = None,
    submitted: Mapping | None = None,
) -> str:
    """The HTML answering `request`: a page, or a fragment for an htmx
    request; a confirmation page for a branch link not yet confirmed.

    `kind` says what `data` is: ``"list"`` (a page of a list, or a list of
    envelopes), ``"item"`` (a resource), ``"create"`` (the resource just
    created, at `location`, its URL), ``"computed"`` (a computed view's
    content, view-only) or any other (shown as JSON). `status` of 400 or
    more makes `data` a problem, shown as notices; for a refused PUT the
    `submitted` body comes back as the edit form, when `stored` (the stored
    resource and its ETag) says the user may PUT it. `etag` is the item's,
    `href` the URL the page is at, and `is_record` a read-only record
    (rendered as a requested view).

    Raises what rendering raises: the caller answers a notice, with no
    content."""
    with request_memo():
        chosen = choose(request)
        if _asks_confirmation(chosen, request):
            return str(confirm_page(chosen, request))
        # An embedded request (`embed`) stays in its embedder's safe mode:
        # it is only ever turned on here (and, for the CSS and JS file
        # responses, in `branches.file_views`).
        with (
            using_selection(chosen.selection),
            using_safe_mode(safe_mode_requested(request) or safe_mode()),
        ):
            body, contexts = _body(
                request,
                resource_type=resource_type,
                kind=kind,
                data=data,
                status=status,
                etag=etag,
                href=href,
                location=location,
                is_record=is_record,
                stored=stored,
                submitted=submitted,
            )
            if not _is_htmx(request):
                body = _page(
                    body,
                    request=request,
                    resource_type=resource_type,
                    contexts=contexts,
                    scope_chain=scope_chain_of(request),
                    chosen=chosen,
                )
    return str(body)


def _body(
    request,
    *,
    resource_type,
    kind,
    data,
    status,
    etag,
    href,
    location,
    is_record,
    stored,
    submitted,
):
    """``(html, contexts)``: the fragment answering the request, and the
    contexts whose stylesheets style its page."""
    scope_chain = scope_chain_of(request)
    pointer = pointer_of(request)
    mode = requested_mode_of(request)
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
                problem,
                status,
                request,
                resource_type,
                scope_chain,
                pointer,
                stored,
                submitted,
            ), []
        return _notices(_problem_messages(problem, status)), []
    if resource_type is not None and kind == "list":
        return render_collection(
            _envelope(data),
            resource_type=resource_type,
            href=href,
            request=request,
            scope_chain=scope_chain,
            mode=mode,
        ), []
    if resource_type is not None and isinstance(data, Mapping):
        if kind == "create":
            href = in_mount(request, urlsplit(location).path) if location else href
        elif kind not in ("item", "computed"):
            return _json(data), []
        # A read-only record (an applied push: the API refuses a PUT and a
        # DELETE of it) is rendered as a requested view: no form, no Delete.
        if mode is None and is_record:
            mode = VIEW_MODE
        # A computed view's content is view-only, its contexts the content's
        # own when it names them (never the stored resource's decisions).
        computed = kind == "computed"
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


def _refused_put(
    problem, status, request, resource_type, scope_chain, pointer, stored, submitted
):
    """A refused PUT: the `submitted` body as the edit form, with the problem's
    messages, only when the user may read the `stored` resource the view loaded
    (decision for GET allowed) and may not be denied a PUT of it (at its ETag,
    with its decisions and contexts); else the notices only, with no form and
    no ETag, as JSON would reveal neither."""
    body = submitted
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
    `SAFE_HEADER`. So are the studio links (`studio.links.studio_links`) on
    a page of a UI type or a studio computed type, when the studio app is
    installed. A page component that leaves
    them out is replaced by the built-in. (Branch CSS, or a component that
    wraps the body in a hidden element, can still hide the banner: follow-up
    ``ui-banner-hideable``.)

    The page component's context also has ``css``: the URLs of the CSS files
    effective on the page's branch, in order, and ``scripts``: those of its
    JS files (empty when it has none); a stored Jinja one's also has
    ``embed``, ``in_mount`` and ``api_url`` (`embed.component_globals`)."""
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
    if apps.is_installed(STUDIO_APP):
        from scrud_django.ui.studio.links import has_studio_links, studio_links

        if has_studio_links(resource_type):
            chrome.append(studio_links(request))
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
            context.update(component_globals(request))
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


def _envelope(data):
    """A list's body as `render_collection` reads it: the page envelope, or
    an unpaginated list of envelopes."""
    if isinstance(data, list):
        return {"count": len(data), "next": None, "previous": None, "content": data}
    return data


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
