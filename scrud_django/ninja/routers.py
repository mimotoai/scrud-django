"""The ninja front end's per-type routers: each operation a thin adapter over
the core facade (`scrud_django.core`), answering as the DRF route does."""

import dataclasses
import functools
import inspect
from contextlib import ExitStack, contextmanager

from django.core.exceptions import PermissionDenied
from django.core.paginator import InvalidPage
from django.http import Http404, HttpResponse, HttpResponseNotAllowed
from django.utils.cache import patch_vary_headers
from django.utils.translation import gettext
from ninja import Router
from ninja.errors import HttpError
from scoped_rbac import access, problems
from scoped_rbac.ninja import request_json, resolve_user
from scrudful._adapter import set_validator_headers
from scrudful._django import aware, conditional_response, precondition_headers
from scrudful._hsr import is_preflight
from scrudful.conditional import http_timestamp, quote_etag
from scrudful.ninja import Entity, ScrudfulRouter, described, scrudful

from scrud_django.core import lists, resources, writes
from scrud_django.core.bodies import check_body
from scrud_django.core.collections import collection_context, collection_schema
from scrud_django.core.types import SAFE_METHODS, requirement_for
from scrud_django.core.urls import absolute_reverse
from scrud_django.ninja.entry import entry
from scrud_django.registration import (
    TypeSpec,
    get_resource_type_for,
    type_db,
    write_db,
)

#: The request attribute holding the resource a PUT wrote, which scrudful's
#: second `load` (after a 2xx PUT, for its validators and Links) returns.
WRITTEN_ATTR = "_scrud_ninja_written"

#: The request attribute holding the API serving the request
#: (`_noting_serving_api`).
API_ATTR = "_scrud_ninja_api"

#: The request attribute caching the request's resource type.
TYPE_ATTR = "_scrud_ninja_resource_type"

#: The methods whose row an item operation reads locked, in the request's
#: write transaction (`scrud_django.ninja.entry`).
LOCKED_METHODS = frozenset({"PUT", "DELETE"})


#: The media types of the collection documents (`collections_router`).
SCHEMA_JSON = "application/schema+json"
LD_JSON = "application/ld+json"

#: The collection documents' route names, as the DRF routes name them.
COLLECTION_ROUTES = {
    "collections-json-schema": (collection_schema, SCHEMA_JSON),
    "collections-json-ld": (collection_context, LD_JSON),
}

#: The ``Allow`` of every answer of the collection documents' routes, as the
#: DRF views (``APIView``s serving GET) give it.
COLLECTION_ALLOW = "GET, HEAD, OPTIONS"

#: What the DRF views of the collection documents answer an OPTIONS with
#: (DRF's ``SimpleMetadata``, by route name): each view's name and
#: description (its class's, its docstring dedented), and the media types
#: of DRF's default renderers and parsers, which those views use. The parity
#: corpus pins them against the DRF views
#: (``tests/test_front_end_parity.py``, ``options-collections-*``).
COLLECTION_OPTIONS = {
    "collections-json-schema": {
        "name": "Resource Collection Schema",
        "description": """\
The JSON Schema of a resource type's list body (the list's
``describedby``): the page envelope of `StandardResultsSetPagination`,
whose ``content`` is the page's resource envelopes, each with the type's
schema embedded as its ``content`` (`embedded_schema`), a ``$ref`` to the
type's ``schema_uri``, or any value for a type without a schema.""",
    },
    "collections-json-ld": {
        "name": "Resource Collection Context",
        "description": """\
The JSON-LD context of a resource type's list body (served as
``application/ld+json``). Its terms are kept as they were (the
``opententeams:`` prefix of first/previous/next/last included): the
``api.openteams.com`` IRIs are part of the API's semantics.""",
    },
}
#: The media types of DRF's default renderers and parsers (`COLLECTION_OPTIONS`).
DRF_RENDERS = ("application/json", "text/html")
DRF_PARSES = (
    "application/json",
    "application/x-www-form-urlencoded",
    "multipart/form-data",
)


def type_router(spec: TypeSpec, *, urls_namespace: str | None = None) -> ScrudfulRouter:
    """A router serving the resources of `spec`'s type: its list route
    ``"/"`` (named ``<slug>-list``), GET and HEAD (a page of the list) and
    POST (a create), and its detail route ``"/{slug}/"`` (named
    ``<slug>-detail``), GET and HEAD, PUT and DELETE, as the DRF routes serve
    them (PATCH is not routed: 405). The type is read per request, by its
    slug, so a deferred registration is served as on DRF. Build one per API:
    a ninja router is mounted on one API only; `urls_namespace` is that
    API's, which the links its HTTP Schema Resource describes (without a
    request) are reversed in.

    OPTIONS of either route is scrud's own operation, not scrudful's
    automatic one (`_options`): its ``Allow`` is the core's
    (``core.resources.allowed_methods_for``), which looks nothing up for a
    requester refused every method; a CORS preflight is a bare 204.

    Every operation passes `entry` first. The API varies the route's
    responses on ``Accept`` (`vary_on_accept`) and answers its errors as
    uniform errors are for each request
    (`scrud_django.ninja.errors.answer_natively_when_off`)."""
    router = ScrudfulRouter()
    router.add_decorator(entry, mode="view")
    router.add_decorator(_noting_serving_api, mode="view")
    slug = spec.slug
    url_name = f"{slug}-detail"
    item = _item_operation_options(spec, urls_namespace)
    list_name = f"{slug}-list"

    @router.api_operation(
        ["GET", "HEAD"], "/", url_name=list_name, operation_id=f"{slug}_list"
    )
    @described(_collection_entity(spec, urls_namespace))
    def list_resources(request, response: HttpResponse):
        rtype = _resource_type(request, spec)
        requirement = requirement_for(rtype)
        queryset = lists.list_queryset(
            request,
            rtype,
            request.GET,
            requirement=requirement,
            ignored=lists.ignored_list_parameters(),
        )
        page = _requested_page(lambda: lists.requested_page(queryset, request.GET))
        etag, last_modified = lists.list_validators(
            request, rtype, queryset, page, requirement
        )
        failed = lists.evaluate_list_read_preconditions(
            request.method,
            precondition_headers(request),
            etag=etag,
            last_modified=last_modified,
        )
        if failed is not None:
            return _precondition_response(request, failed)
        _set_validators(response, etag, last_modified)
        return lists.list_body(request, rtype, page)

    @router.api_operation(
        ["POST"], "/", url_name=list_name, operation_id=f"{slug}_create"
    )
    @described(_item_entity(spec, urls_namespace))
    def create(request):
        rtype = _resource_type(request, spec)
        requirement = requirement_for(rtype)
        body = request_json(request)
        check_body(body, present=_body_present(request))
        resources.check_request(request, rtype, requirement, list_route=True, body=body)
        headers = precondition_headers(request)
        with ExitStack() as locks:
            # Inside the entry's transaction (`entry`), held until it ends:
            # conditional creates of the type run one at a time, each
            # evaluated against the list the previous one left.
            if lists.list_validators_needed(headers):
                locks.enter_context(writes.conditional_create_lock(rtype, write_db()))
            failed = lists.evaluate_list_preconditions(
                request.method,
                headers,
                lambda: _requested_page(
                    lambda: lists.create_list_validators(
                        request,
                        rtype,
                        requirement=requirement,
                        ignored=lists.ignored_list_parameters(),
                        using=write_db(),
                    )
                ),
            )
            if failed is not None:
                return _precondition_response(request, failed)
            obj = writes.create(
                request, rtype, body, requirement=requirement, using=write_db()
            )
        response = _serving_api(request).create_response(
            request, obj.content, status=201
        )
        response["Location"] = absolute_reverse(
            rtype.route_name_detail(), args=[obj.pk], request=request
        )
        _set_validators(response, *resources.item_validators(obj))
        return response

    @router.api_operation(
        ["GET", "HEAD"], "/{slug}/", url_name=url_name, operation_id=f"{slug}_retrieve"
    )
    @scrudful(**item)
    def retrieve(request, slug: str, obj, response: HttpResponse):
        _as_document(request, response, spec)
        return obj.content

    @router.api_operation(
        ["PUT"], "/{slug}/", url_name=url_name, operation_id=f"{slug}_replace"
    )
    @scrudful(**item)
    def replace(request, slug: str, obj, response: HttpResponse):
        rtype = _resource_type(request, spec)
        written = writes.update(
            request,
            rtype,
            obj,
            request_json(request),
            requirement=requirement_for(rtype),
            using=write_db(),
        )
        setattr(request, WRITTEN_ATTR, written)
        _as_document(request, response, spec)
        return written.content

    @router.api_operation(
        ["DELETE"], "/{slug}/", url_name=url_name, operation_id=f"{slug}_destroy"
    )
    @scrudful(**item)
    def destroy(request, slug: str, obj):
        rtype = _resource_type(request, spec)
        writes.destroy(
            request,
            rtype,
            obj,
            requirement=requirement_for(rtype),
            using=write_db(),
        )
        return _no_content()

    # scrud's own OPTIONS, replacing scrudful's automatic one on each path.
    @router.api_operation(["OPTIONS"], "/", url_name=list_name, include_in_schema=False)
    def list_options(request):
        return _options(request, spec, None)

    @router.api_operation(
        ["OPTIONS"], "/{slug}/", url_name=url_name, include_in_schema=False
    )
    def item_options(request, slug: str):
        return _options(request, spec, slug)

    return router


def _options(request, spec, slug):
    """The answer to an OPTIONS of `spec`'s item `slug` (``None``: its
    list), as the DRF route answers it: a 204 with no content, whose
    ``Allow`` is the core's (``core.resources.allowed_methods_for``) for the
    requester, resolved first as for every other method (`resolve_user`,
    which `entry` leaves to this operation); its ``Http404`` (an item
    missing or hidden from a method the requester may use) answered by the
    API. A CORS preflight is a bare 204, before anything else, as
    scrudful's automatic OPTIONS answers it. scrudful's router run gives
    every other answer the HTTP Schema Resource link and exposes ``Link``
    and ``Allow``."""
    if is_preflight(request.method, request.headers.get):
        return _no_content()
    resolve_user(request)
    rtype = _resource_type(request, spec)
    response = _no_content()
    response["Allow"] = ", ".join(
        resources.allowed_methods_for(request, rtype, slug, requirement_for(rtype))
    )
    return response


def _no_content():
    """A 204, with no content and no ``Content-Type``."""
    response = HttpResponse(status=204)
    del response["Content-Type"]
    return response


def collections_router() -> Router:
    """A router serving the documents a list's Links name, as the DRF routes
    of those names serve them: ``"/collections-json-schema/{slug}/"`` (the
    JSON Schema of `slug`'s list body, ``application/schema+json``) and
    ``"/collections-json-ld/{slug}/"`` (its JSON-LD context,
    ``application/ld+json``), GET and HEAD, public; served as
    ``application/json`` to a request preferring it; and OPTIONS, answered
    with the DRF view's metadata (`_document_options`). A plain ninja
    router: the DRF views are no scrudful views (no HTTP Schema Resource).
    Every operation passes `entry` first. The API gives every answer of its
    routes, a 405 included, the DRF views' ``Allow``
    (`as_drf_api_views`)."""
    router = Router()
    router.add_decorator(entry, mode="view")
    router.add_decorator(_noting_serving_api, mode="view")
    for name, (document, media_type) in COLLECTION_ROUTES.items():
        path = f"/{name}/{{slug}}/"
        router.add_api_operation(
            path,
            ["GET", "HEAD"],
            _document_operation(document, media_type),
            url_name=name,
            operation_id=f"{name}_retrieve",
        )
        router.add_api_operation(
            path,
            ["OPTIONS"],
            _document_options(name),
            url_name=name,
            include_in_schema=False,
        )
    return router


def _document_operation(document, media_type):
    def collection_document(request, slug: str, response: HttpResponse):
        try:
            resource_type = get_resource_type_for(slug)
        except Http404 as exc:
            # DRF answers a Http404 with its message.
            raise HttpError(404, str(exc)) from exc
        _as_media_type(request, response, media_type)
        return document(resource_type)

    return collection_document


def _document_options(name):
    """The OPTIONS operation of the collection document `name`: DRF's
    ``SimpleMetadata`` of its view (`COLLECTION_OPTIONS`, with DRF's
    default renderers and parsers), a 200 written as DRF writes it (the
    serving API's renderer, ``application/json``), whatever the slug, a
    CORS preflight's too, as the DRF view answers them, once the requester
    is resolved (`resolve_user`, which `entry` leaves to this operation:
    DRF authenticates every request)."""
    metadata = {
        **COLLECTION_OPTIONS[name],
        "renders": list(DRF_RENDERS),
        "parses": list(DRF_PARSES),
    }

    def collection_document_options(request, slug: str):
        # `entry` leaves a real OPTIONS to the operation: the DRF view
        # authenticates it as any other (a CORS preflight included).
        resolve_user(request)
        return _serving_api(request).create_response(request, metadata, status=200)

    return collection_document_options


def as_drf_api_views(api, url_names) -> None:
    """Have `api`'s URL patterns named in `url_names` (the collection
    documents' routes) answer as a DRF ``APIView`` serving ``GET`` does
    (their DRF views are no scrudful views): every response, ninja's 405
    included, with the view's ``Allow`` (`COLLECTION_ALLOW`), and without
    the ``Access-Control-Expose-Headers`` scrudful and scoped-rbac give the
    API's errors. Call it before ``api.urls`` is read."""
    _wrap_patterns(api, url_names, _as_drf_api_view)


def _as_drf_api_view(view):
    @functools.wraps(view)
    def answered(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        response["Allow"] = COLLECTION_ALLOW
        if response.has_header("Access-Control-Expose-Headers"):
            del response["Access-Control-Expose-Headers"]
        return response

    return answered


def method_not_allowed_as_drf(api, url_names) -> None:
    """Have `api`'s URL patterns named in `url_names` (those of plain ninja
    routers: the collection documents', the computed types') answer ninja's
    own 405 (a method no operation of the path serves) through `api`'s
    handlers, as an ``HttpError(405)``, when scoped-rbac's handlers left it
    as ninja's (they do while uniform errors are off): problem details,
    which `scrud_django.ninja.errors.answer_natively_when_off` answers as
    DRF's native 405 then. Call it before ``answer_natively_when_off``,
    before ``api.urls`` is read."""
    _wrap_patterns(api, url_names, functools.partial(_method_not_allowed, api))


def _method_not_allowed(api, view):
    @functools.wraps(view)
    def answered(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        if isinstance(response, HttpResponseNotAllowed):
            return api.on_exception(request, HttpError(405, "Method not allowed"))
        return response

    return answered


def _collection_entity(spec, urls_namespace):
    """The entity of `spec`'s list body: the collection documents' Links
    (`collections_router`), within the serving API's namespace."""

    def link(name):
        def target(request, obj):
            return absolute_reverse(
                name, args=[spec.slug], request=request, namespace=urls_namespace
            )

        return target

    return Entity(
        schema_link_or_func=link("collections-json-schema"),
        context_link_or_func=link("collections-json-ld"),
    )


def _item_entity(spec, urls_namespace):
    """The entity of a resource of `spec`'s type (a create's answer): its
    type's schema and JSON-LD context Links."""

    def targets(request):
        return resources.item_link_targets(
            request, _resource_type(request, spec), namespace=urls_namespace
        )

    return Entity(
        schema_link_or_func=lambda request, obj: targets(request)[0],
        context_link_or_func=lambda request, obj: targets(request)[1],
    )


def _requested_page(read):
    """``read()`` (which reads a page of a list), a page that is not one of
    the list's answered 404 ``"Invalid page."``, as DRF's paginator answers
    it."""
    try:
        return read()
    except InvalidPage as exc:
        raise HttpError(404, gettext("Invalid page.")) from exc


def _set_validators(response, etag, last_modified):
    """Set `response`'s ``ETag`` (of the unquoted `etag`) and
    ``Last-Modified``, as scrudful sets them."""
    set_validator_headers(
        response, quote_etag(etag), http_timestamp(aware(last_modified))
    )


def _noting_serving_api(run):
    """Decorate a ninja operation's ``run`` (``mode="view"``) to note on the
    request the API serving it (`_serving_api`), whose renderer writes the
    answers the operation builds itself."""
    api = None

    @functools.wraps(run)
    def noting(request, *args, **kwargs):
        nonlocal api
        if api is None:
            api = inspect.unwrap(run).__self__.api
        setattr(request, API_ATTR, api)
        return run(request, *args, **kwargs)

    return noting


def _serving_api(request):
    """The API serving `request` (`_noting_serving_api`)."""
    return getattr(request, API_ATTR)


def _precondition_response(request, result):
    """scrudful's answer to the failed precondition `result`, as the DRF
    routes answer it: a bare 304, else problem details
    (``problems.conditional_problem``) with their Links, written by the
    serving API's renderer (answered natively while uniform errors are off,
    `scrud_django.ninja.errors.answer_natively_when_off`)."""
    if result.body is None:
        return conditional_response(result)
    result = dataclasses.replace(
        result, body=problems.conditional_problem(request, result)
    )

    def render(data, status, content_type):
        response = _serving_api(request).create_response(request, data, status=status)
        response["Content-Type"] = content_type
        return response

    return conditional_response(
        result,
        link=problems.PROBLEM_LINKS,
        content_type=problems.PROBLEM_JSON,
        render=render,
    )


def _item_operation_options(spec, urls_namespace):
    """The ``@scrudful`` arguments of `spec`'s item operations."""

    def before(request, **path_params):
        rtype = _resource_type(request, spec)
        body = None
        if request.method == "PUT" and not access.checks_allow(request):
            body = request_json(request)
            check_body(body, present=_body_present(request))
        with _options_denials(request):
            resources.check_request(
                request,
                rtype,
                requirement_for(rtype),
                list_route=False,
                body=body,
            )

    def load(request, slug, **params):
        written = getattr(request, WRITTEN_ATTR, None)
        if written is not None:
            return written
        rtype = _resource_type(request, spec)
        allow_copy = access.checks_allow(request)
        body = None
        if request.method == "PUT" and not allow_copy:
            body = request_json(request)
        lock = request.method in LOCKED_METHODS and not allow_copy
        with _options_denials(request):
            return resources.load_for_read(
                request,
                rtype,
                slug,
                requirement=requirement_for(rtype),
                method=request.method,
                body=body,
                lock_using=write_db() if lock else None,
            )

    def targets(request):
        return resources.item_link_targets(
            request, _resource_type(request, spec), namespace=urls_namespace
        )

    def schema_link(request, obj):
        return targets(request)[0]

    def context_link(request, obj):
        return targets(request)[1]

    return {
        "load": load,
        "before": before,
        "etag_func": lambda obj: resources.item_validators(obj)[0],
        "last_modified_func": lambda obj: resources.item_validators(obj)[1],
        "schema_link_or_func": schema_link,
        "context_link_or_func": context_link,
        # scrudful's own 400, 412 and 428 are problem details; while uniform
        # errors are off for a request, the API answers them as scrudful's
        # native {"detail": ...} (errors.answer_natively_when_off), as the
        # DRF views do.
        "error": problems.problem_entity(),
        "error_body_func": problems.conditional_problem,
    }


def _resource_type(request, spec):
    """The resource type of `spec` for `request` (read once per request): on
    `type_db` for an unsafe method, as DRF reads it for a write. ``None`` for
    a request without one (scrudful builds its HTTP Schema Resource with
    none)."""
    if request is None:
        return get_resource_type_for(spec.slug)
    rtype = getattr(request, TYPE_ATTR, None)
    if rtype is None:
        using = None if request.method in SAFE_METHODS else type_db()
        rtype = get_resource_type_for(spec.slug, using=using)
        setattr(request, TYPE_ATTR, rtype)
    return rtype


def _body_present(request) -> bool:
    """Whether `request` carries a body, as DRF reads one (``request.stream``):
    a ``Content-Length`` above 0."""
    meta = request.META
    try:
        length = int(meta.get("CONTENT_LENGTH", meta.get("HTTP_CONTENT_LENGTH", 0)))
    except (TypeError, ValueError):
        length = 0
    return length > 0


def vary_on_accept(api, url_names) -> None:
    """Have `api`'s URL patterns named in `url_names` (the type routers'
    routes) vary every response on ``Accept``, errors, 405s and OPTIONS
    included, as the DRF views' do (DRF's ``APIView``, which negotiates
    among several renderers, sends ``Vary: Accept`` on each; a document
    type's representation does depend on it, `_as_document`). Call it before
    ``api.urls`` is read."""
    _wrap_patterns(api, url_names, _varying_on_accept)


def _wrap_patterns(api, url_names, wrap):
    """Have `api`'s URL patterns named in `url_names` answer through
    ``wrap(view)``, once its ``urls`` are read."""
    get_urls = api._get_urls
    names = frozenset(url_names)

    @functools.wraps(get_urls)
    def _get_urls():
        patterns = get_urls()
        for pattern in patterns:
            if pattern.name in names:
                pattern.callback = wrap(pattern.callback)
        return patterns

    api._get_urls = _get_urls


def _varying_on_accept(view):
    @functools.wraps(view)
    def varied(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        patch_vary_headers(response, ["Accept"])
        return response

    return varied


def _as_document(request, response, spec):
    """Serve `response` as `spec`'s document media type, unless the request
    prefers ``application/json`` (``DocumentMediaTypeMixin``)."""
    if spec.document_media_type is not None:
        _as_media_type(request, response, spec.document_media_type)


def _as_media_type(request, response, media_type):
    """Serve `response` as `media_type`, unless the request prefers
    ``application/json`` (``DocumentMediaTypeMixin``)."""
    preferred = request.get_preferred_type([media_type, "application/json"])
    if preferred == media_type:
        response["Content-Type"] = media_type


@contextmanager
def _options_denials(request):
    """On the copy of an OPTIONS on which scrudful checks one method for
    ``Allow`` (``access.checks_allow``), a refusal as scrudful reads one there
    (``PermissionDenied``: the method is left out); elsewhere transparent."""
    try:
        yield
    except access.AccessDenied as exc:
        if not access.checks_allow(request):
            raise
        raise PermissionDenied(exc.message) from exc
