"""Errors answered without a front end: the status, body and headers the
DRF routes answer an error with (`error_answer`), for a view that calls the
core alone, uniform errors on or off as ``SCOPED_RBAC["UNIFORM_ERRORS"]``
says for ``"drf"``. A scrud error is answered as
`scrud_django.exceptions.scrud_exception_handler` answers it
(`scrud_error_answer`, which the ninja front end's handler calls too,
`scrud_django.ninja.errors.install`), an access error, a ``Http404`` and
Django's ``PermissionDenied`` as scoped-rbac's DRF handler answers the DRF
exception each stands for."""

import json

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.http import Http404
from django.utils.encoding import force_str
from django.utils.translation import gettext
from scoped_rbac import access, problems

from scrud_django.exceptions import ScrudException

PROBLEM_JSON = problems.PROBLEM_JSON
JSON = "application/json"

#: The headers of a problem scoped-rbac's handler answers (uniform errors
#: on).
PROBLEM_HEADERS = {"Content-Type": PROBLEM_JSON, "Link": problems.PROBLEM_LINKS}

#: The headers of DRF's native error body (uniform errors off).
NATIVE_HEADERS = {"Content-Type": JSON}


def uniform_errors() -> bool:
    """Whether the DRF routes answer errors as problem details now
    (``SCOPED_RBAC["UNIFORM_ERRORS"]`` for ``"drf"``, read per call)."""
    return problems.uniform_errors("drf")


def error_answer(
    exc: BaseException, request=None, *, challenge: str | None = None
) -> tuple[int, dict | list, dict[str, str]] | None:
    """``(status, body, headers)`` of the answer the DRF routes give `exc`,
    raised serving `request`, uniform errors as they are now
    (`uniform_errors`); ``None`` for any other exception, which the caller
    re-raises:

    - a `ScrudException`: its own answer (`scrud_error_answer`), a
      malformed body's (`scrud_django.exceptions.RequestBodyMalformedError`)
      included;
    - a ``scoped_rbac.access`` error (`access_error_answer`): an
      ``AccessDenied`` of the anonymous user is 401 (with `challenge` as its
      ``WWW-Authenticate``, the challenge of the route's first
      authenticator, which DRF sends; none for a session), any other its
      status (403 for ``AccessDenied``);
    - a ``Http404`` (of its message, `drf_detail`) or an
      ``ObjectDoesNotExist`` (as a ``Http404`` without one: a missing row
      is the route's 404): 404;
    - Django's ``PermissionDenied``: 403, of its message.

    The body is a problem with its ``status`` (``application/problem+json``,
    with scoped-rbac's problem-details ``Link`` for the errors its handler
    answers); with uniform errors off, DRF's native body for those
    (``application/json``)."""
    uniform = uniform_errors()
    if isinstance(exc, ScrudException):
        return scrud_error_answer(exc, request, uniform=uniform)
    if isinstance(exc, access.AccessError):
        return access_error_answer(exc, request, uniform=uniform, challenge=challenge)
    if isinstance(exc, Http404 | ObjectDoesNotExist):
        detail = drf_detail(exc) if isinstance(exc, Http404) else None
        default = gettext("Not found.")
        return _framework_answer(404, "not_found", detail, default, uniform)
    if isinstance(exc, PermissionDenied):
        default = gettext("You do not have permission to perform this action.")
        return _framework_answer(
            403, "permission_denied", drf_detail(exc), default, uniform
        )
    return None


def scrud_error_answer(
    exc: ScrudException, request=None, *, uniform: bool
) -> tuple[int, dict | list, dict[str, str]]:
    """``(status, body, headers)`` of `exc` as
    `scrud_django.exceptions.scrud_exception_handler` answers it: its
    status, its `ScrudException.response_data` (given `request`) with that
    ``status``, as ``application/problem+json``. One standing for an error
    DRF answers through scoped-rbac's handler (`ScrudException.problem_links`)
    is answered as that one: with scoped-rbac's problem-details ``Link``
    while `uniform` (errors are on), else as DRF's native body
    (`ScrudException.native_response_data`, ``application/json``)."""
    if exc.problem_links and not uniform:
        return exc.status_code, exc.native_response_data(), dict(NATIVE_HEADERS)
    data = exc.response_data({"request": request})
    data["status"] = exc.status_code
    headers = (
        dict(PROBLEM_HEADERS) if exc.problem_links else {"Content-Type": PROBLEM_JSON}
    )
    return exc.status_code, data, headers


def access_error_answer(
    exc: access.AccessError,
    request=None,
    *,
    uniform: bool,
    challenge: str | None = None,
) -> tuple[int, dict | list, dict[str, str]]:
    """``(status, body, headers)`` of the ``scoped_rbac.access`` error `exc`
    as scoped-rbac's DRF handler answers the DRF exception it stands for
    (``scoped_rbac.permissions.drf_error``): an ``AccessDenied`` of the
    anonymous user of `request` (no `request`: not anonymous) is DRF's
    ``NotAuthenticated``, a 401 carrying `challenge` as its
    ``WWW-Authenticate`` when given; any other is its status
    (``problems.from_access_error``). With `uniform` errors, scoped-rbac's
    problem; else DRF's native body: ``{"detail": message}``, a
    ``ReservedContext``'s or ``PrimaryKeyChanged``'s ``[message]`` (DRF's
    ``ValidationError`` of it), an ``InvalidContext``'s
    ``{field: [message]}``."""
    if isinstance(exc, access.AccessDenied) and _anonymous(request):
        if uniform:
            body = problems.error_problem(401)
        else:
            body = {"detail": gettext("Authentication credentials were not provided.")}
        headers = dict(PROBLEM_HEADERS if uniform else NATIVE_HEADERS)
        if challenge:
            headers["WWW-Authenticate"] = challenge
        return 401, body, headers
    status, problem = problems.from_access_error(exc)
    if uniform:
        return status, problem, dict(PROBLEM_HEADERS)
    if isinstance(exc, access.InvalidContext):
        native = {exc.field: [exc.message]}
    elif isinstance(exc, access.ReservedContext | access.PrimaryKeyChanged):
        native = [exc.message]
    else:
        native = {"detail": exc.message}
    return status, native, dict(NATIVE_HEADERS)


def _anonymous(request) -> bool:
    """Whether the user of `request` is the anonymous user (a request
    without one is: ``access.request_user``); ``False`` without a
    `request`."""
    if request is None:
        return False
    user = access.request_user(request)
    return user is None or not user.is_authenticated


def _framework_answer(status, code, detail, default, uniform):
    """The answer to DRF's own exception of `status` and `code` (``NotFound``,
    ``PermissionDenied``) made of `detail` (`drf_detail`; ``None``: its
    `default` message), as scoped-rbac's handler answers it: a problem whose
    ``detail`` is the message when it is text (else the status title), or
    with uniform errors off DRF's native body (``{"detail": message}``, a
    list's or a dict's own)."""
    message = default if detail is None else detail
    if uniform:
        text = message if isinstance(message, str) else None
        return (
            status,
            problems.error_problem(status, code=code, detail=text),
            dict(PROBLEM_HEADERS),
        )
    native = message if isinstance(message, list | dict) else {"detail": message}
    return status, native, dict(NATIVE_HEADERS)


def drf_detail(exc):
    """The ``detail`` DRF gives a Django exception `exc` (``Http404``,
    ``PermissionDenied``) it answers as its own (``APIException(*exc.args)``):
    ``None`` without a message (the exception's default then), else the
    message with each leaf as text (``force_str``), a list (or tuple) and a
    dict kept as such, as DRF's ``_get_error_details`` makes it."""
    message = exc.args[0] if exc.args else None
    return None if message is None else _as_text(message)


def _as_text(value):
    if isinstance(value, list | tuple):
        return [_as_text(item) for item in value]
    if isinstance(value, dict):
        return {key: _as_text(item) for key, item in value.items()}
    return force_str(value)


def parse_error_detail(request) -> str:
    """DRF's ``JSONParser`` message for `request`'s body, which is not JSON:
    the error of decoding it with its charset (else ``DEFAULT_CHARSET``) and
    parsing it strictly (no ``NaN`` or infinities); DRF's ``ParseError``
    default for a body that parses."""
    try:
        text = request.body.decode(request.encoding or settings.DEFAULT_CHARSET)
        json.loads(text, parse_constant=_strict_constant)
    except ValueError as exc:
        return f"JSON parse error - {exc}"
    return gettext("Malformed request.")


def _strict_constant(value):
    raise ValueError(f"Out of range float values are not JSON compliant: {value!r}")
