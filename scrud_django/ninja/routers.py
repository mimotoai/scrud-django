"""The ninja front end's per-type routers: each operation a thin adapter over
the core facade (`scrud_django.core`), answering as the DRF route does."""

import functools
from contextlib import contextmanager

from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.utils.cache import patch_vary_headers
from scoped_rbac import access, problems
from scoped_rbac.ninja import request_json
from scrudful.ninja import ScrudfulRouter, scrudful

from scrud_django.core import resources, writes
from scrud_django.core.bodies import check_body
from scrud_django.core.types import SAFE_METHODS, requirement_for
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

#: The request attribute caching the request's resource type.
TYPE_ATTR = "_scrud_ninja_resource_type"

#: The methods whose row an item operation reads locked, in the request's
#: write transaction (`scrud_django.ninja.entry`).
LOCKED_METHODS = frozenset({"PUT", "DELETE"})


def type_router(spec: TypeSpec, *, urls_namespace: str | None = None) -> ScrudfulRouter:
    """A router serving the resources of `spec`'s type: its detail route
    ``"/{slug}/"`` (named ``<slug>-detail``), GET and HEAD, PUT and DELETE,
    as the DRF detail route serves them (PATCH is not routed: 405). The type
    is read per request, by its slug, so a deferred registration is served
    as on DRF. Build one per API: a ninja router is mounted on one API only;
    `urls_namespace` is that API's, which the links its HTTP Schema Resource
    describes (without a request) are reversed in.

    Every operation passes `entry` first. The API varies the route's
    responses on ``Accept`` (`vary_on_accept`) and answers its errors as
    uniform errors are for each request
    (`scrud_django.ninja.errors.answer_natively_when_off`)."""
    router = ScrudfulRouter()
    router.add_decorator(entry, mode="view")
    slug = spec.slug
    url_name = f"{slug}-detail"
    item = _item_operation_options(spec, urls_namespace)

    @router.api_operation(["GET", "HEAD"], "/{slug}/", url_name=url_name)
    @scrudful(**item)
    def retrieve(request, slug: str, obj, response: HttpResponse):
        _as_document(request, response, spec)
        return obj.content

    @router.api_operation(["PUT"], "/{slug}/", url_name=url_name)
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

    @router.api_operation(["DELETE"], "/{slug}/", url_name=url_name)
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
        response = HttpResponse(status=204)
        del response["Content-Type"]
        return response

    return router


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
    get_urls = api._get_urls
    names = frozenset(url_names)

    @functools.wraps(get_urls)
    def _get_urls():
        patterns = get_urls()
        for pattern in patterns:
            if pattern.name in names:
                pattern.callback = _varying_on_accept(pattern.callback)
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
    media_type = spec.document_media_type
    if media_type is None:
        return
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
