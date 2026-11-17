"""scrud's errors on DRF: the exception handler (``REST_FRAMEWORK =
{"EXCEPTION_HANDLER": "scrud_django.drf.exceptions.scrud_exception_handler"}``)
and the wrapping of a `ScrudException` as the ``APIException`` DRF's views
and handlers know (`as_drf`, `drf_errors`)."""

import functools

from django.core.exceptions import ImproperlyConfigured
from rest_framework.views import exception_handler as drf_exception_handler
from scoped_rbac.rest import exception_handler as scoped_rbac_exception_handler

from scrud_django import exceptions
from scrud_django.drf.renderers import DOCUMENT_RENDERERS
from scrud_django.exceptions import ScrudAPIException, ScrudException

__all__ = ["ScrudAPIException", "as_drf", "drf_errors", "scrud_exception_handler"]


def scrud_exception_handler(exc, context):
    """Scrud errors keep their own body; everything else gets scoped-rbac's
    RFC 9457 problem details."""
    if isinstance(exc, ScrudAPIException):
        exc = exc.scrud
    if not isinstance(exc, ScrudException):
        return scoped_rbac_exception_handler(exc, context)
    response = drf_exception_handler(as_drf(exc), context)
    response.data = exc.response_data(context)
    response.data["status"] = response.status_code
    try:
        request = context["request"]
        renderer = request.accepted_renderer
        # The "json" format, or a schema or JSON-LD document's renderer
        # (formats schema-json and ld-json). Another renderer (e.g. an
        # adopter's application/vnd.api+json) keeps its media type.
        if renderer.format == "json" or isinstance(renderer, DOCUMENT_RENDERERS):
            response.content_type = "application/problem+json"
            response["Content-Type"] = "application/problem+json"
    except AttributeError:
        pass
    return response


def as_drf(exc):
    """`exc` (a `ScrudException`) as the ``APIException`` DRF's views and
    handlers know: its status code, its detail (an ``ErrorDetail`` of the
    message and the error's ``default_code``), and `exc` itself as ``.scrud``
    (`scrud_django.exceptions.ScrudAPIException`, defined there once, at
    import). Without DRF installed there is none: ``ImproperlyConfigured``."""
    wrapper = exceptions.ScrudAPIException
    if wrapper is None:
        raise ImproperlyConfigured(
            "scrud_django.drf.exceptions.as_drf needs Django REST framework, "
            "which is not installed: a scrud error is a ScrudException without it."
        )
    return wrapper(exc)


def drf_errors(func):
    """Decorate a DRF function view (``@api_view``) so that a `ScrudException`
    it raises reaches DRF's handlers as `as_drf` of it. Apply it below
    ``@api_view``."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ScrudException as exc:
            raise as_drf(exc) from exc

    return wrapper
