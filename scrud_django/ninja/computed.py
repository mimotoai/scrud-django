"""The ninja routes of computed types: each a thin adapter over the core's
computed answer (`scrud_django.core.computed`), answering as
``scrud_django.computed.ComputedResourceViewSet`` does."""

import functools

from django.http import HttpResponse
from ninja import Router
from scoped_rbac import access
from scoped_rbac.ninja import resolve_user

from scrud_django.core.computed import (
    check_authenticated,
    computed_answer,
    computed_link,
    not_modified,
    served_content,
)
from scrud_django.ninja.entry import entry
from scrud_django.ninja.routers import (
    DRF_RENDERS,
    _noting_serving_api,
    _resource_type,
    _serving_api,
    _wrap_patterns,
)
from scrud_django.registration import TypeSpec

#: The methods a computed route serves; any other is a 405 (after the
#: requester is authenticated, `refuse_the_anonymous`).
SERVED_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: What ``ComputedResourceViewSet`` answers an OPTIONS with (DRF's
#: ``SimpleMetadata``): its name and description (its docstring, dedented),
#: and the media types of its renderers and its parser. The parity corpus
#: pins it against the DRF view (``tests/test_front_end_parity.py``,
#: ``computed-options``).
COMPUTED_OPTIONS = {
    "name": "Computed Resource",
    "description": """\
A read-only view of computed content. ``compute`` may raise
``Http404`` or ``PermissionDenied``, and must only return what the
request's user may read: it asks the same checks the stored resources'
views do. Any method but GET, HEAD and OPTIONS is a 405.""",
    "renders": list(DRF_RENDERS),
    "parses": ["application/json"],
}


def computed_router(spec: TypeSpec, *, urls_namespace: str | None = None) -> Router:
    """A router serving `spec`'s computed type at ``"/{slug}/"`` (named
    ``<slug>-detail``; no list), as ``ComputedResourceViewSet`` serves it, to
    an authenticated requester only (DRF's ``IsAuthenticated``; the
    anonymous gets 401):

    - GET and HEAD: the content `spec`'s ``compute`` gives for the key and
      the requester (``core.computed.computed_answer``), with its strong
      ETag, a 304 for an ``If-None-Match`` naming it, and the type's schema
      and context ``Link`` (within the serving API's namespace,
      `urls_namespace` without a request);
    - OPTIONS: the DRF view's metadata (`COMPUTED_OPTIONS`), a CORS
      preflight's too, as the DRF view answers it;
    - any other method: 405, once the requester is authenticated (the API
      refuses the anonymous first, `refuse_the_anonymous`).

    A plain ninja router: the DRF view is no scrudful view (no HTTP Schema
    Resource). Every operation passes `entry` first. The API gives every
    answer the DRF view's ``Allow`` (``routers.as_drf_api_views``) and varies
    it on ``Accept`` (``routers.vary_on_accept``)."""
    router = Router()
    router.add_decorator(entry, mode="view")
    router.add_decorator(_noting_serving_api, mode="view")
    name = f"{spec.slug}-detail"

    @router.api_operation(
        ["GET", "HEAD"], "/{slug}/", url_name=name, operation_id=f"{spec.slug}_computed"
    )
    def computed(request, slug: str):
        check_authenticated(request)
        rtype = _resource_type(request, spec)
        content, etag = computed_answer(request, rtype, slug)
        if not_modified(request, etag):
            response = HttpResponse(status=304)
            del response["Content-Type"]
        else:
            # Written as DRF writes it (`served_content`), not as ninja's
            # encoder would.
            response = _serving_api(request).create_response(
                request, served_content(content), status=200
            )
            link = computed_link(request, rtype, namespace=urls_namespace)
            if link:
                response["Link"] = link
        response["ETag"] = etag
        return response

    @router.api_operation(
        ["OPTIONS"], "/{slug}/", url_name=name, include_in_schema=False
    )
    def computed_options(request, slug: str):
        # `entry` leaves a real OPTIONS to the operation: the DRF view
        # authenticates it as any other (a CORS preflight included).
        resolve_user(request)
        check_authenticated(request)
        return _serving_api(request).create_response(
            request, COMPUTED_OPTIONS, status=200
        )

    return router


def refuse_the_anonymous(api, url_names) -> None:
    """Have `api`'s URL patterns named in `url_names` (computed routes)
    refuse a method they do not serve as the DRF view does, before its 405:
    a request rewritten from an OPTIONS (``access.refuse_rewritten_options``),
    then one whose user (``resolve_user``) is not authenticated
    (``core.computed.check_authenticated``: 401), answered by `api`. Call it
    after ``scrud_django.ninja.errors.install`` and before
    ``errors.answer_natively_when_off``, before ``api.urls`` is read."""
    _wrap_patterns(api, url_names, functools.partial(_refusing_the_anonymous, api))


def _refusing_the_anonymous(api, view):
    @functools.wraps(view)
    def refusing(request, *args, **kwargs):
        if request.method not in SERVED_METHODS:
            try:
                access.refuse_rewritten_options(request)
                resolve_user(request)
                check_authenticated(request)
            except Exception as exc:  # answered as the API answers it
                return api.on_exception(request, exc)
        return view(request, *args, **kwargs)

    return refusing
