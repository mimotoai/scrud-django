"""DRF's authentication on the ninja front end: `drf_user`, a
``SCOPED_RBAC["NINJA_USER"]`` hook (opt in)::

    SCOPED_RBAC = {..., "NINJA_USER": "scrud_django.drf.auth.drf_user"}

It runs the project's DRF authentication classes on the ninja request, so a
Basic or token client of the DRF routes is the same user on ninja."""

from django.conf import settings
from django.utils.module_loading import import_string
from ninja.errors import AuthenticationError
from rest_framework import exceptions as drf_exceptions
from rest_framework.request import Request
from rest_framework.settings import api_settings
from scoped_rbac import access, problems

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


class CredentialsRefused(ScrudException, AuthenticationError):
    """Credentials a DRF authenticator refused (its ``AuthenticationFailed``
    or ``NotAuthenticated``), answered on ninja as the DRF routes answer
    them: 401, scoped-rbac's ``not_authenticated`` problem, or, with uniform
    errors off, DRF's native body of the authenticator's message (scrud's
    error handler, `scrud_django.ninja.errors.install`). An API without
    scrud's handler answers it as ninja's ``AuthenticationError``."""

    status_code = 401
    default_code = "not_authenticated"
    default_detail = "Incorrect authentication credentials."
    problem_links = True

    def __init__(self, message=None):
        ScrudException.__init__(self, message)
        # ninja's HttpError: its status and message.
        self.message = str(self.detail)
        Exception.__init__(self, self.message)

    def response_data(self, context):
        return problems.error_problem(401)

    def native_response_data(self):
        return {"detail": self.detail}


def drf_user(request):
    """The user DRF's authentication classes (`authentication_classes`) give
    the Django request `request`: the first that authenticates it, as a DRF
    view's ``request.user``; ``None`` (the anonymous user) when none does.

    As on a DRF route: credentials an authenticator refuses (a wrong Basic
    password, an unknown token) are a 401 (`CredentialsRefused`);
    ``SessionAuthentication`` enforces CSRF on an unsafe method (a session
    user's write without a valid token is a 403, ``"CSRF Failed: <reason>"``),
    and Basic and token credentials need none. scoped-rbac's
    ``resolve_user``, which calls the hook once per request, CSRF-checks the
    session's user again, whatever authenticated it. ``request.user`` is left
    as it was: ``resolve_user`` sets it to what this returns."""
    authenticators = [authenticator() for authenticator in authentication_classes()]
    drf_request = Request(request, authenticators=authenticators)
    missing = object()
    previous = request.__dict__.get("user", missing)
    try:
        user = drf_request.user
    except (
        drf_exceptions.AuthenticationFailed,
        drf_exceptions.NotAuthenticated,
    ) as exc:
        raise CredentialsRefused(exc.detail) from exc
    except drf_exceptions.PermissionDenied as exc:
        raise access.AccessDenied(str(exc.detail)) from exc
    finally:
        # DRF's request sets the Django request's user as it authenticates.
        if previous is missing:
            request.__dict__.pop("user", None)
        else:
            request.user = previous
    if drf_request.successful_authenticator is None:
        return None
    return user
