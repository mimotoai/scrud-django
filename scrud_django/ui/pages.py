"""The UI app's pages (`scrud_django.ui.urls`): plain Django views serving a
type's list (`list_page`) and items (`item_page`, a computed type's
included) as HTML, at the UI's URLs. They read and write through the
framework-neutral core, as the API's routes do (so they refuse what the API
refuses, at the same status), and answer HTML with `render_html`: a page
or an htmx fragment, a write's re-rendered frame, a refused PUT's edit form
with its errors, a 412 (or a 409 DELETE) notice with a frame that reloads
itself, an error's notice.

The user is Django's session user (``request.user``); a write is checked for CSRF
by ``csrf_protect``, whether or not ``CsrfViewMiddleware`` is installed. A page serves GET
and HEAD, and the writes of the type's route: POST on a list, PUT and
DELETE on an item (a computed type's items are read only). Any other
method is a 405 naming the page's in ``Allow``.

Each page sets the UI's base path on the request (`UI_BASE_ATTR`), so
that the API hrefs the core and the render tree build (list items, CSS and
JS files, a create's ``Location``) are mapped to the UI's
(`scrud_django.ui.branches.selection.in_mount`); its own URL (a frame's
``data-scrud-href``, ``hx-put`` and ``hx-delete``) is the URL requested.
A page's URL without its trailing slash is not redirected to it
(``no_append_slash``): the UI claims only its own URLs, never one the
project might serve. It imports no DRF.
"""

import contextvars
import copy
import logging
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied
from django.core.paginator import InvalidPage
from django.db import DatabaseError, transaction
from django.http import Http404, HttpResponse
from django.urls import (
    NoReverseMatch,
    Resolver404,
    get_script_prefix,
    resolve,
    reverse,
)
from django.utils.translation import gettext
from django.views.decorators.common import no_append_slash
from django.views.decorators.csrf import csrf_protect
from scoped_rbac import access
from scrudful._django import aware, precondition_headers
from scrudful.conditional import http_timestamp, quote_etag, validator_headers

from scrud_django.core import lists, resources, writes
from scrud_django.core.authz import content_of
from scrud_django.core.bodies import request_json
from scrud_django.core.computed import (
    check_authenticated,
    computed_answer,
    not_modified,
)
from scrud_django.core.errors import error_answer, precondition_answer
from scrud_django.core.types import (
    drf_only_authorization,
    requirement_for,
    type_options_for_slug,
)
from scrud_django.exceptions import MethodNotAllowedError, ScrudException
from scrud_django.registration import get_resource_type_for, type_db, write_db
from scrud_django.ui import UI_NAMESPACE
from scrud_django.ui.branches import is_record
from scrud_django.ui.branches.effective import stored_state
from scrud_django.ui.branches.selection import (
    MOUNT_ATTR,
    UI_BASE_ATTR,
    check_mount_access,
    in_mount,
)
from scrud_django.ui.html import (
    CONTENT_TYPE,
    RENDER_FAILED_MESSAGE,
    Stored,
    _notices,
    check_ui_headers,
    html_headers,
    pointer_of,
    render_html,
)

logger = logging.getLogger(__name__)

SAFE_METHODS = ("GET", "HEAD")

# What a page answers with, by its kind (`render_html`'s ``kind``) and
# request method: the methods it serves.
PAGE_METHODS = {
    "list": ("GET", "HEAD", "POST"),
    "item": ("GET", "HEAD", "PUT", "DELETE"),
    "computed": ("GET", "HEAD"),
}


class ServedSlug(str):
    """The slug of a type the UI serves (`served`), carrying the
    `resource_type` read for it, so that a page's GET reads it once."""

    resource_type = None

    def __new__(cls, slug: str, resource_type):
        self = super().__new__(cls, slug)
        self.resource_type = resource_type
        return self


# What `served` found for each slug in the block of `served_memo` in
# progress (``None`` outside one).
_served_memo: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "scrud_ui_served", default=None
)


@contextmanager
def served_memo():
    """A block in which `served` reads each slug's type once: a request's
    (`PublishedBranchMiddleware` resolves a prefix's URL to a page before
    Django resolves it again)."""
    token = _served_memo.set({})
    try:
        yield
    finally:
        _served_memo.reset(token)


# The slugs `served` is deciding in this context, each with whether its
# decision was entered again (``None`` outside one).
_resolving: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "scrud_ui_resolving", default=None
)


def served(slug: str) -> ServedSlug | None:
    """`slug` with its type (`ServedSlug`) when the UI serves pages of it: a
    type the API serves (`routed`) whose authorization the core asks in
    full (`refusal`: not one whose route's views are a custom
    ``viewset_class``'s or were routed by hand, unless it declares the
    neutral ``checks`` and ``object_checks``, or ``ui_authorization="core"``:
    the UI fails closed on them) and that is registered (``registration.get_resource_type_for`` finds it); else
    ``None``. A database error reading it is logged and serves
    nothing, so that the project's other URLs still answer
    (``scrud_django.ui.urls``' slug converter asks it). In a `served_memo`
    block, what it found for `slug` before.

    Deciding resolves the API's routes of `slug` (`route_views`). When the
    UI app is routed before them at the same URLs (included first under
    the API's prefix), that resolving reaches the UI's own pattern for
    `slug`, whose converter asks `served(slug)` again: that inner call
    answers ``None`` (the UI's pattern does not match, so the API's route
    resolves), and the outer one answers ``None`` too: the UI does not
    serve a type whose API routes it would shadow, and never recurses."""
    memo = _served_memo.get()
    if memo is not None and slug in memo:
        return memo[slug]
    resolving = _resolving.get()
    if resolving is not None and slug in resolving:
        resolving[slug] = True
        return None
    token = None
    if resolving is None:
        resolving = {}
        token = _resolving.set(resolving)
    resolving[slug] = False
    try:
        found = _served(slug)
        if resolving[slug]:
            # The API's routes of `slug` resolve through the UI's own.
            found = None
    finally:
        del resolving[slug]
        if token is not None:
            _resolving.reset(token)
    if memo is not None:
        memo[slug] = found
    return found


def route_views(slug: str) -> tuple | None:
    """The views the API's routes of the type `slug` resolve to in the
    URLconf serving the request: its list's (``<slug>-list``) and its
    detail's (``<slug>-detail``; ``None`` when it has none, or it resolves to
    nothing), or for a computed type, one with a registered ``compute``, its
    detail's; ``None`` when the type has no list route (no computed detail
    route), which the API does not serve."""
    names = ("detail",) if is_computed(slug) else ("list", "detail")
    views = []
    for kind in names:
        try:
            url = reverse(f"{slug}-{kind}", args=["key"] if kind == "detail" else ())
        except NoReverseMatch:
            if kind == names[0]:
                return None
            views.append(None)
            continue
        views.append(_resolved_view(url))
    return tuple(views)


def _resolved_view(url):
    """The view the URL path `url` (as ``reverse`` gives it, under the
    script prefix) resolves to, or ``None``."""
    script = get_script_prefix()
    path_info = "/" + url[len(script) :] if url.startswith(script) else url
    try:
        return resolve(path_info).func
    except Resolver404:
        return None


def routed(slug: str) -> bool:
    """Whether the API serves the type `slug` (`route_views`)."""
    return route_views(slug) is not None


def refusal(slug: str) -> str | None:
    """Why the UI refuses the type `slug` for the authorization of its API
    routes' views (``drf_only_authorization`` of each of `route_views`: a
    custom ``viewset_class``'s, or a view no trusted builder marked, unless
    the type opts in); ``None`` when it does not, or the API does not serve
    the type."""
    return _refusal_of(slug, route_views(slug))


def _refusal_of(slug, views) -> str | None:
    """`refusal` of `slug`, whose `route_views` are `views`."""
    for view in views or ():
        found = drf_only_authorization(slug, view)
        if found is not None:
            return found
    return None


def _served(slug):
    views = route_views(slug)
    if views is None or _refusal_of(slug, views) is not None:
        return None
    try:
        resource_type = get_resource_type_for(slug)
    except Http404:
        return None
    except DatabaseError as error:
        logger.warning(
            "scrud-ui: not serving pages of %r: reading its type failed (%s: %s)",
            slug,
            type(error).__name__,
            error,
        )
        return None
    return ServedSlug(slug, resource_type)


def is_computed(slug: str) -> bool:
    """Whether `slug` is a computed type's, which registered its
    ``compute`` (``registration.computed_resource_type(..., compute=)``)."""
    return type_options_for_slug(slug).compute is not None


@no_append_slash
@csrf_protect
def list_page(request, slug: str) -> HttpResponse:
    """The page of the list of the type `slug` (a page of it, as the API's
    list pages it): GET and HEAD; POST creates a resource of it and answers
    with its frame (201). A computed type has no list: ``Http404``."""
    if is_computed(slug):
        raise Http404
    return _page(request, slug, None)


@no_append_slash
@csrf_protect
def item_page(request, slug: str, pk: str) -> HttpResponse:
    """The page of the resource `pk` of the type `slug` (of a computed type,
    its content for the key `pk`): GET and HEAD; PUT replaces it and answers
    with its re-rendered frame; DELETE deletes it (204)."""
    return _page(request, slug, pk)


@dataclass
class _Answer:
    """What a page answers: its `status`, the `data` rendered (a resource,
    a list's body, a problem; ``None``: no body) as `kind`, the resource's
    `etag` (quoted) and a create's `location` (the API URL), the response's
    own `headers`, and a refused PUT's `stored` resource and `submitted`
    body (`render_html`)."""

    status: int
    data: object = None
    kind: str = "item"
    etag: str | None = None
    location: str | None = None
    headers: dict = field(default_factory=dict)
    stored: Stored | None = None
    submitted: Mapping | None = None


def _page(request, slug, pk) -> HttpResponse:
    """The answer of the page of `slug`'s list (`pk` ``None``) or item, its
    checks in the order the API's route asks them (DRF's ``initial()``,
    then its handler):

    1. a method the page does not serve for the type (`_allowed`) is a 405
       at once, for anyone, as scoped-rbac leaves a method the API's view
       does not serve to its 405 before any grant;
    2. the handler asks the route's request check first (a write's body
       read and checked before it, as DRF parses it in its permission
       check; a computed type's: an authenticated user), then the prefix's
       check and the fragment protocol's headers (400, `_entered`), then
       reads or writes.

    What it raises is answered as the API answers it. The type is the one
    the slug converter read (`ServedSlug`) for a GET or HEAD; a write reads
    it on the type database, as the API's route does."""
    setattr(request, UI_BASE_ATTR, _ui_base(request, slug))
    kind = _kind(slug, pk)
    method = request.method
    if method in SAFE_METHODS:
        resource_type = _type_of(slug)
    else:
        resource_type = get_resource_type_for(slug, using=type_db())
    allow = _allowed(resource_type, kind)
    if method not in allow:
        answer = _refused(MethodNotAllowedError(method), request, kind, allow)
        return _respond(request, resource_type, answer)
    if method in SAFE_METHODS:
        answer = _read_answer(request, resource_type, slug, pk, kind, allow)
        return _respond(request, resource_type, answer)
    handler = _HANDLERS[kind, method]
    try:
        answer = handler(request, resource_type, slug, pk, allow)
    except Exception as exc:
        answer = _refused(exc, request, _error_kind(kind, method), allow)
    return _respond(request, resource_type, answer)


def render_list(request, slug, *, query, mount=None) -> tuple[int, str]:
    """The status and HTML a GET of the page of the list of `slug` answers
    (`list_page`'s own read and render, without the HTTP response around
    them): for `request`, whose GET is `query`, under the prefix `mount`
    (``None``: as `request` is). A computed type has no list: ``Http404``.
    The user is `request`'s; every check of the page applies (and a refusal
    is its notice, with its status). An embed (`scrud_django.ui.embed`)
    renders through it."""
    if is_computed(slug):
        raise Http404
    return _render_read(request, slug, None, query, mount)


def render_item(request, slug, pk, *, mount=None) -> tuple[int, str]:
    """The status and HTML a GET of the page of the resource `pk` of `slug`
    (of a computed type, its content for the key `pk`) answers, as
    `render_list` does for a list."""
    return _render_read(request, slug, pk, request.GET, mount)


def _render_read(request, slug, pk, query, mount) -> tuple[int, str]:
    if mount is not None:
        setattr(request, MOUNT_ATTR, mount)
    request.GET = query
    kind = _kind(slug, pk)
    resource_type = _type_of(slug)
    allow = _allowed(resource_type, kind)
    answer = _read_answer(request, resource_type, slug, pk, kind, allow)
    return _rendered(request, resource_type, answer)


def _kind(slug, pk) -> str:
    if pk is None:
        return "list"
    return "computed" if is_computed(slug) else "item"


def _type_of(slug):
    """The type of `slug`: the one its `ServedSlug` carries, else read."""
    resource_type = getattr(slug, "resource_type", None)
    return resource_type or get_resource_type_for(slug)


def _read_answer(request, resource_type, slug, pk, kind, allow) -> _Answer:
    """The answer of a GET (or HEAD) of the page `kind` of `slug`: what its
    handler reads, in the stored state (the safe mode and drafts of an
    embedding render are render-only, and its rendering keeps them), a
    refusal answered as the API answers it."""
    try:
        with stored_state():
            return _HANDLERS[kind, "GET"](request, resource_type, slug, pk, allow)
    except Exception as exc:
        return _refused(exc, request, kind, allow)


def _entered(request) -> None:
    """The checks the API's route asks once its request check passed
    (``ResourceViewMixin.initial``): under a published branch's prefix, GET
    on its branch (`check_mount_access`), then the fragment protocol's
    headers (`check_ui_headers`, 400)."""
    check_mount_access(request)
    check_ui_headers(request)


def _ui_base(request, slug) -> str:
    """The UI's base path: the URL of the list of `slug` in the namespace
    the request was resolved in (the UI's), without the slug."""
    match = getattr(request, "resolver_match", None)
    namespace = getattr(match, "namespace", "") or UI_NAMESPACE
    path = reverse(f"{namespace}:list", kwargs={"slug": slug})
    suffix = f"{slug}/"
    return path[: -len(suffix)] if path.endswith(suffix) else path


def _allowed(resource_type, kind) -> tuple[str, ...]:
    """The methods the page of `kind` serves for `resource_type` (its
    ``Allow``): GET and HEAD, then those of the type's route
    (`resources.served_methods`) the page serves."""
    item = kind != "list"
    served = resources.served_methods(resource_type, item=item)
    return tuple(
        method
        for method in PAGE_METHODS[kind]
        if method in SAFE_METHODS or method in served
    )


def _error_kind(kind, method):
    return "create" if kind == "list" and method == "POST" else kind


def _read_list(request, resource_type, slug, pk, allow) -> _Answer:
    """A GET (or HEAD) of the list, as the API's list route reads it: the
    rows its user may list (as HTML: `lists.list_queryset`), the page asked
    for (an invalid page is a 404), its validators and preconditions."""
    requirement = requirement_for(resource_type, slug)
    resources.check_request(request, resource_type, requirement, list_route=True)
    _entered(request)
    # (`list_queryset` asks the list's request check again: as it passed.)
    queryset = lists.list_queryset(
        request, resource_type, request.GET, requirement=requirement, html=True
    )
    page = _requested(lambda: lists.requested_page(queryset, request.GET))
    etag, last_modified = lists.list_validators(
        request, resource_type, queryset, page, requirement
    )
    failed = lists.evaluate_list_read_preconditions(
        request.method,
        precondition_headers(request),
        etag=etag,
        last_modified=last_modified,
    )
    if failed is not None:
        return _precondition(precondition_answer(request, failed), "list")
    return _Answer(
        200,
        lists.list_body(request, resource_type, page),
        "list",
        etag=quote_etag(etag),
        headers=_validators(etag, last_modified),
    )


def _create(request, resource_type, slug, pk, allow) -> _Answer:
    """A POST to the list, as the API's list route creates: the body read
    and checked, the route's request check, then the page's checks
    (`_entered`), then the list's preconditions (a conditional create holds
    the type's lock until it ends) and the create, in one transaction on
    the write database (`writes.create_answer`)."""
    requirement = requirement_for(resource_type, slug)
    using = write_db()
    body, present = request_json(request)
    writes.check_write_request(
        request,
        resource_type,
        body,
        requirement=requirement,
        list_route=True,
        present=present,
    )
    _entered(request)
    with writes.write_transaction(using):
        answer = _requested(
            lambda: writes.create_answer(
                request,
                resource_type,
                body,
                requirement=requirement,
                using=using,
                request_checked=True,
            )
        )
    if answer.problem is not None:
        return _precondition(answer.problem, "create")
    return _Answer(
        201,
        answer.content,
        "create",
        etag=quote_etag(answer.etag),
        location=answer.location,
        headers={
            **_validators(answer.etag, answer.last_modified),
            "Location": in_mount(request, answer.location),
        },
    )


def _read_item(request, resource_type, slug, pk, allow) -> _Answer:
    """A GET (or HEAD) of the item, as the API's detail route reads it:
    looked up and checked (`resources.load_for_read`), as its reader gets it
    (`resources.item_read`), then its preconditions (a 304)."""
    requirement = requirement_for(resource_type, slug)
    resources.check_request(request, resource_type, requirement, list_route=False)
    _entered(request)
    obj = resources.load_for_read(
        request,
        resource_type,
        pk,
        requirement=requirement,
        method=request.method,
        request_checked=True,
    )
    content, etag, last_modified = resources.item_read(request, resource_type, obj)
    failed = resources.item_precondition_answer(
        request, etag=etag, last_modified=last_modified, method=request.method
    )
    if failed is not None:
        return _precondition(failed, "item")
    return _Answer(
        200,
        content,
        "item",
        etag=quote_etag(etag),
        headers=_validators(etag, last_modified),
    )


def _update(request, resource_type, slug, pk, allow) -> _Answer:
    """A PUT of the item, as the API's detail route writes it: the body read
    and checked, the route's request check, then the page's checks
    (`_entered`), then in one transaction on the write database the item
    looked up for the PUT (its row locked) and checked, its preconditions
    (``If-Match``: 428 without one, 412 for a stale one), the update
    (`writes.item_write_answer`).

    A refusal (400, 403) carries what the edit form is rendered from: the
    stored resource as first looked up (else looked up now,
    `_stored_for_refused_put`) and the submitted body."""
    requirement = requirement_for(resource_type, slug)
    using = write_db()
    body = stored = None

    def loaded(obj):
        nonlocal stored
        stored = _stored(obj)

    try:
        body, present = request_json(request)
        writes.check_write_request(
            request,
            resource_type,
            body,
            requirement=requirement,
            list_route=False,
            present=present,
        )
        _entered(request)
        with writes.write_transaction(using):
            answer = writes.item_write_answer(
                request,
                resource_type,
                pk,
                "PUT",
                body,
                requirement=requirement,
                using=using,
                request_checked=True,
                loaded=loaded,
            )
    except Exception as exc:
        answer = _refused(exc, request, "item", allow)
        # Not for invalid UI headers: the answer is then a notice.
        if answer.status in (400, 403) and pointer_of(request) is not None:
            if stored is None:
                stored = _stored_for_refused_put(
                    request, resource_type, pk, requirement
                )
            answer.stored = stored
            answer.submitted = body if isinstance(body, Mapping) else None
        return answer
    if answer.problem is not None:
        return _precondition(answer.problem, "item")
    return _Answer(
        200,
        answer.content,
        "item",
        etag=quote_etag(answer.etag),
        headers=_validators(answer.etag, answer.last_modified),
    )


def _destroy(request, resource_type, slug, pk, allow) -> _Answer:
    """A DELETE of the item, as the API's detail route deletes it: the
    route's request check, then the page's checks (`_entered`), then in one
    transaction on the write database the item looked up for the DELETE
    (its row locked) and checked, its preconditions, the delete (a resource
    in use is a 409; `writes.item_write_answer`)."""
    requirement = requirement_for(resource_type, slug)
    using = write_db()
    writes.check_write_request(
        request, resource_type, None, requirement=requirement, list_route=False
    )
    _entered(request)
    with writes.write_transaction(using):
        answer = writes.item_write_answer(
            request,
            resource_type,
            pk,
            "DELETE",
            None,
            requirement=requirement,
            using=using,
            request_checked=True,
        )
    if answer.problem is not None:
        return _precondition(answer.problem, "item")
    return _Answer(204)


def _read_computed(request, resource_type, slug, key, allow) -> _Answer:
    """A GET (or HEAD) of a computed type's content for `key`, as the API's
    computed route answers it: to an authenticated user only, the type's
    ``compute``, its ETag (a 304 for an ``If-None-Match`` naming it)."""
    check_authenticated(request)
    _entered(request)
    content, etag = computed_answer(request, resource_type, key)
    headers = {"ETag": etag}
    if not_modified(request, etag):
        return _Answer(304, None, "computed", headers=headers)
    return _Answer(200, content, "computed", etag=etag, headers=headers)


_HANDLERS = {
    ("list", "GET"): _read_list,
    ("list", "POST"): _create,
    ("item", "GET"): _read_item,
    ("item", "PUT"): _update,
    ("item", "DELETE"): _destroy,
    ("computed", "GET"): _read_computed,
}


def _requested(read):
    """``read()`` (which reads a page of a list), a page that is not one of
    the list's a 404 ``"Invalid page."``, as the API's paginator answers
    it."""
    try:
        return read()
    except InvalidPage as exc:
        raise Http404(gettext("Invalid page.")) from exc


def _stored(obj) -> Stored:
    """`obj` as the edit form of a refused PUT of it is rendered from: a copy
    of its content (the write changes the instance), and its ETag."""
    return Stored(content=copy.deepcopy(content_of(obj)), etag=quote_etag(obj.etag))


# What a lookup refused to the user raises: 401, 403, 404.
_LOOKUP_REFUSALS = (Http404, PermissionDenied, access.AccessError)


def _stored_for_refused_put(request, resource_type, pk, requirement):
    """The `Stored` resource of a PUT refused before it was looked up (the
    request check refuses on the body): looked up now, with the PUT's checks
    (as ``Allow`` checks a PUT, without the body, then the object check),
    without the row lock: a refused request never waits for it or holds it.
    Refused (401, 403, 404), or not readable (a database error, another
    scrud error, logged), it is ``None``: the answer is a notice."""
    try:
        # A savepoint: a failed lookup leaves the request's transaction (if
        # any) usable.
        with transaction.atomic(using=write_db()):
            obj = resources.load_for_read(
                request, resource_type, pk, requirement=requirement, method="PUT"
            )
    except _LOOKUP_REFUSALS:
        return None
    except (
        ScrudException,
        DatabaseError,
        transaction.TransactionManagementError,
    ):
        logger.warning(
            "scrud-ui: looking up %s for a refused PUT failed",
            request.path,
            exc_info=True,
        )
        return None
    return _stored(obj)


def _precondition(found, kind) -> _Answer:
    """The answer of a failed precondition, ``(status, body, headers)``
    (`scrud_django.core.errors.precondition_answer`'s): a 304 without a
    body, else its problem."""
    status, body, headers = found
    return _Answer(status, body or None, kind, headers=dict(headers))


def _validators(etag, last_modified) -> dict:
    """The ``ETag`` (of the unquoted `etag`) and ``Last-Modified`` headers."""
    return dict(
        validator_headers(quote_etag(etag), http_timestamp(aware(last_modified)))
    )


def _refused(exc, request, kind, allow) -> _Answer:
    """`exc` answered as the API answers it (`error_answer`: its status, its
    problem as the notice's, its own headers; a 405 names the page's
    methods in ``Allow``); any other exception is raised again."""
    answered = error_answer(exc, request)
    if answered is None:
        raise exc
    status, body, headers = answered
    headers = dict(headers)
    if status == 405:
        headers["Allow"] = ", ".join(allow)
    return _Answer(status, body, kind, headers=headers)


def _rendered(request, resource_type, answer: _Answer) -> tuple[int, str]:
    """`answer`'s status and HTML (`render_html`; no body for a 204 or a
    304). A success that fails to render is a 500 with a notice; an error
    that fails keeps its status."""
    status = answer.status
    if status < 400 and (answer.data is None or status in (204, 304)):
        return status, ""
    try:
        body = render_html(
            request,
            resource_type=resource_type,
            kind=answer.kind,
            data=answer.data,
            status=status,
            etag=answer.etag,
            href=request.path,
            location=answer.location,
            is_record=(
                status < 400
                and answer.kind in ("item", "create")
                and is_record(resource_type.type_uri, answer.data)
            ),
            stored=answer.stored,
            submitted=answer.submitted,
        )
    except Exception:
        # The last resort: a notice, with no content (nor a traceback).
        logger.exception(
            "scrud-ui: rendering %s (status %s) failed", request.path, status
        )
        if status < 400:
            status = 500
        body = str(_notices([RENDER_FAILED_MESSAGE]))
    return status, body


def _respond(request, resource_type, answer: _Answer) -> HttpResponse:
    """`answer` as an HTML response (`_rendered`; no body for a HEAD), with
    its headers (but its ``Content-Type``) and those of any HTML response
    (`html_headers`)."""
    status, body = _rendered(request, resource_type, answer)
    response = HttpResponse(
        "" if request.method == "HEAD" else body,
        status=status,
        content_type=CONTENT_TYPE,
    )
    for name, value in answer.headers.items():
        if name.lower() != "content-type":
            response[name] = value
    html_headers(request, response)
    return response
