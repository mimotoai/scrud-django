"""DRF's authentication on the ninja front end: `drf_user`, a
``SCOPED_RBAC["NINJA_USER"]`` hook (opt in)::

    SCOPED_RBAC = {..., "NINJA_USER": "scrud_django.drf.auth.drf_user"}

It runs the project's DRF authentication classes on the ninja request, so a
Basic or token client of the DRF routes is the same user on ninja."""

from types import SimpleNamespace

from django.conf import settings
from django.utils.module_loading import import_string
from ninja.errors import AuthenticationError
from rest_framework import exceptions as drf_exceptions
from rest_framework.request import Request
from rest_framework.settings import api_settings

# Private: the problem scoped-rbac's DRF handler answers a DRF exception with
# (follow-up ``ninja-private-upstream-use``).
from scoped_rbac.rest import _problem_for

from scrud_django.core.errors import AUTH_CHALLENGE_ATTR
from scrud_django.exceptions import ScrudException

#: The Django setting naming the authentication classes `drf_user` runs (a
#: list of classes or their dotted paths), when they are not DRF's
#: ``DEFAULT_AUTHENTICATION_CLASSES``.
AUTHENTICATION_SETTING = "SCRUD_DRF_AUTHENTICATION"


def authentication_classes() -> list:
    """The authentication classes `drf_user` runs, in order: the
    ``SCRUD_DRF_AUTHENTICATION`` setting when it is set, else DRF's
    ``api_settings.DEFAULT_AUTHENTICATION_CLASSES`` (each read per call)."""
    configured = getattr(settings, AUTHENTICATION_SETTING, None)
    if configured is None:
        return list(api_settings.DEFAULT_AUTHENTICATION_CLASSES)
    return [
        import_string(entry) if isinstance(entry, str) else entry
        for entry in configured
    ]


class AuthenticatorError(ScrudException):
    """An ``APIException`` a DRF authenticator raised (a ``Throttled``, a
    ``ValidationError``, a ``PermissionDenied`` such as
    ``SessionAuthentication``'s CSRF failure), answered on ninja as the DRF
    routes answer it (scrud's error handler,
    `scrud_django.ninja.errors.install`): its status; with uniform errors on,
    the problem scoped-rbac's DRF handler answers; off, DRF's native body; and
    the headers DRF's handler gives it (``WWW-Authenticate``,
    ``Retry-After``)."""

    problem_links = True

    def __init__(self, exc: drf_exceptions.APIException):
        self.status_code = exc.status_code
        codes = exc.get_codes()
        self.default_code = codes if isinstance(codes, str) else "error"
        detail = exc.detail
        super().__init__(detail if isinstance(detail, str) else str(detail))
        # DRF's exception_handler: the native body and headers.
        self.native = detail if isinstance(detail, list | dict) else {"detail": detail}
        headers = {}
        if getattr(exc, "auth_header", None):
            headers["WWW-Authenticate"] = exc.auth_header
        if getattr(exc, "wait", None):
            headers["Retry-After"] = str(int(exc.wait))  # DRF's "%d"
        self.answer_headers = headers
        _status, self.problem = _problem_for(
            exc, SimpleNamespace(data=self.native, status_code=exc.status_code)
        )

    def response_data(self, context):
        return dict(self.problem)

    def native_response_data(self):
        return self.native


class CredentialsRefused(AuthenticatorError, AuthenticationError):
    """Credentials a DRF authenticator refused (its ``AuthenticationFailed``
    or ``NotAuthenticated``): 401 as on the DRF routes (scoped-rbac's
    ``not_authenticated`` problem, or DRF's native body of the
    authenticator's message), with the ``WWW-Authenticate`` challenge of the
    first authenticator when it has one. An API without scrud's handler
    answers it as ninja's ``AuthenticationError``."""

    def __init__(self, exc: drf_exceptions.APIException):
        super().__init__(exc)
        # A DRF view answers it 401 whatever its status (scoped-rbac's
        # handler); ninja's HttpError: its status and message.
        self.status_code = 401
        self.message = str(self.detail)
        Exception.__init__(self, self.message)


def drf_user(request):
    """The user DRF's authentication classes (`authentication_classes`) give
    the Django request `request`: the first that authenticates it, as a DRF
    view's ``request.user``; ``None`` (the anonymous user) when none does.

    As on a DRF route: the first authenticator's ``authenticate_header`` is
    the ``WWW-Authenticate`` challenge of the request's 401s (noted on the
    request, `scrud_django.core.errors.AUTH_CHALLENGE_ATTR`); credentials an
    authenticator refuses are a 401 (`CredentialsRefused`), and any other
    ``APIException`` it raises is answered as DRF answers it
    (`AuthenticatorError`): ``SessionAuthentication``'s CSRF check of an
    unsafe method is a 403 (``"CSRF Failed: <reason>"``), and Basic and
    token credentials need no token. scoped-rbac's ``resolve_user``, which
    calls the hook once per request, CSRF-checks the session's user again,
    whatever authenticated it (stricter than DRF for Basic or token
    credentials of the session's own user sent with its cookie: see the
    README). ``request.user`` is left as it was: ``resolve_user`` sets it to
    what this returns."""
    authenticators = [authenticator() for authenticator in authentication_classes()]
    drf_request = Request(request, authenticators=authenticators)
    challenge = (
        authenticators[0].authenticate_header(drf_request) if authenticators else None
    )
    setattr(request, AUTH_CHALLENGE_ATTR, challenge)
    missing = object()
    previous = request.__dict__.get("user", missing)
    try:
        user = drf_request.user
    except (
        drf_exceptions.AuthenticationFailed,
        drf_exceptions.NotAuthenticated,
    ) as exc:
        if challenge:
            exc.auth_header = challenge
        raise CredentialsRefused(exc) from exc
    except drf_exceptions.APIException as exc:
        raise AuthenticatorError(exc) from exc
    finally:
        # DRF's request sets the Django request's user as it authenticates.
        if previous is missing:
            request.__dict__.pop("user", None)
        else:
            request.user = previous
    if drf_request.successful_authenticator is None:
        return None
    return user
