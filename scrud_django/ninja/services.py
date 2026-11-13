"""The ninja route of the service catalog (``services/``), answering as the DRF
view (``scrud_django.views.get_service_list``) does."""

import functools

from scoped_rbac import problems
from scoped_rbac.ninja import resolve_user
from scrudful._hsr import is_preflight
from scrudful.ninja import ScrudfulRouter, scrudful

from scrud_django import services
from scrud_django.ninja.entry import entry
from scrud_django.ninja.routers import _no_content, _wrap_patterns

#: The route's name, as the DRF route names it.
SERVICES_ROUTE = "services"

#: The methods the DRF view serves (``scrudful_api_view``'s default).
SERVICES_METHODS = ["GET"]

#: What scrudful's OPTIONS of the DRF view gives as ``Allow``.
SERVICES_OPTIONS_ALLOW = "GET, OPTIONS"


def drf_api_view_allow(methods) -> str:
    """The ``Allow`` DRF's ``api_view(methods)`` gives every answer but
    scrudful's OPTIONS: its ``allowed_methods``, in the order of the set
    ``api_view`` builds of them (``set(methods) | {"options"}``), built here
    the same way, so in the same order within a process (string hashes, and
    so a set's order, vary from one process to the next)."""
    allowed_methods = set(methods) | {"options"}
    return ", ".join(method.upper() for method in allowed_methods)


def services_router() -> ScrudfulRouter:
    """A router serving the service catalog at ``"/"`` (named ``services``),
    as the DRF view serves it, to everyone:

    - GET: the catalog (``services.catalog``, each type's list on the
      serving API first, ``scrud_django.ScrudServices``) with its ETag
      (``ScrudServices.etag_of``), scrudful's 304, 400 and 412 (problem
      details, answered natively while uniform errors are off);
    - OPTIONS: scrud's own (`_options`), whose ``Allow`` is that of
      scrudful's OPTIONS of the DRF view;
    - any other method, HEAD included (DRF's ``api_view`` serves only the
      methods it lists): 405.

    Every operation passes `entry` first. The API gives every answer but
    OPTIONS the DRF view's ``Allow`` (`as_drf_api_view_function`)."""
    router = ScrudfulRouter()
    router.add_decorator(entry, mode="view")

    @router.api_operation(["GET"], "/", url_name=SERVICES_ROUTE)
    @scrudful(
        load=lambda request, **params: services.catalog(request),
        load_as="catalog",
        etag_func=services.etag_of,
        error=problems.problem_entity(),
        error_body_func=problems.conditional_problem,
    )
    def service_list(request, catalog):
        return catalog

    @router.api_operation(
        ["OPTIONS"], "/", url_name=SERVICES_ROUTE, include_in_schema=False
    )
    def service_list_options(request):
        return _options(request)

    return router


def _options(request):
    """The answer to an OPTIONS of the catalog, as scrudful answers it on the
    DRF view: a 204 with no content, whose ``Allow`` is
    `SERVICES_OPTIONS_ALLOW`, the requester resolved first as DRF
    authenticates every request (`resolve_user`, which `entry` leaves to
    this operation); a CORS preflight a bare 204, before anything else.
    scrudful's router run gives every other answer the HTTP Schema Resource
    link and exposes ``Link`` and ``Allow``."""
    if is_preflight(request.method, request.headers.get):
        return _no_content()
    resolve_user(request)
    response = _no_content()
    response["Allow"] = SERVICES_OPTIONS_ALLOW
    return response


def as_drf_api_view_function(api, url_names, methods) -> None:
    """Have `api`'s URL patterns named in `url_names` (the catalog's) give
    every answer but an OPTIONS's (a refusal of one included) the ``Allow``
    of the DRF view, a function
    view of DRF's ``api_view(methods)`` (`drf_api_view_allow`), and expose
    it as scrudful's DRF views do. Call it after
    ``scrud_django.ninja.errors.answer_natively_when_off``, which orders a
    405's ``Allow`` otherwise, before ``api.urls`` is read."""
    allow = drf_api_view_allow(methods)
    _wrap_patterns(api, url_names, functools.partial(_as_drf_api_view_function, allow))


def _as_drf_api_view_function(allow, view):
    @functools.wraps(view)
    def answered(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        # scrudful's OPTIONS of the DRF view gives its own (none to a CORS
        # preflight); the view's refusal of an OPTIONS, DRF's.
        if request.method != "OPTIONS" or response.status_code >= 400:
            response["Allow"] = allow
        return response

    return answered
