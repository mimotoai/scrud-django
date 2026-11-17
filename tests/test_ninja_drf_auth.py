"""scrud's own ninja user hook, `scrud_django.drf.auth.drf_user`: DRF's
authentication classes run on the ninja request (the ``drf`` extra's; the
hook-less and README-hook cases are `tests.test_ninja_auth`'s, which run
without DRF)."""

import contextlib
import functools
import json

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.utils.http import quote_etag
from ninja.errors import AuthenticationError
from rest_framework.authentication import (
    BaseAuthentication,
    BasicAuthentication,
    SessionAuthentication,
    TokenAuthentication,
)
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import PermissionDenied, Throttled, ValidationError
from rest_framework.views import APIView

from scrud_django.models import Resource

from . import front_ends
from .front_ends import Case, parity
from .test_front_end_parity import PROFILE
from .test_ninja_auth import NINJA, _basic, _token, item  # noqa: F401  (a fixture)

pytestmark = [pytest.mark.urls("tests.parity_urls"), pytest.mark.django_db]


def test_drf_authenticates_the_basic_credentials_ninja_takes_as_anonymous(item):
    # tests.test_ninja_auth: ninja answers these credentials as the anonymous
    # user's; DRF authenticates the same header (its default Basic
    # authentication).
    client = Client()
    read = client.get(f"{NINJA}partner-profiles/{item.pk}/", headers=_basic())
    assert read.status_code == 401, read.content
    drf = client.get(f"/drf/partner-profiles/{item.pk}/", headers=_basic())
    assert drf.status_code == 200, drf.content


# scrud's own hook, `scrud_django.drf.auth.drf_user`: DRF's authentication
# classes run on the ninja request, so each client is the same user on both
# front ends, refused alike, and CSRF-checked alike. Each case runs on both
# (`front_ends.parity`, fresh data each), with a client enforcing CSRF.

DRF_HOOKED = {**settings.SCOPED_RBAC, "NINJA_USER": "scrud_django.drf.auth.drf_user"}
SESSION_FIRST = [SessionAuthentication, BasicAuthentication, TokenAuthentication]
BASIC_FIRST = [BasicAuthentication, SessionAuthentication, TokenAuthentication]
TOKEN_FIRST = [TokenAuthentication, SessionAuthentication, BasicAuthentication]

#: A CSRF secret a case's client sends as its cookie and its header.
CSRF_SECRET = "s3cr3t" * 5 + "ok"


def _owner(client):
    """A superuser with a password and a token, not logged in on `client`, and
    a partner profile (``item``)."""
    user = User.objects.create_superuser("owner", "owner@example.com", "pw")
    Token.objects.create(user=user, key="t0k3n")
    return {"item": PROFILE.store("a").pk}


def _logged_in_owner(client):
    keys = _owner(client)
    client.force_login(User.objects.get(username="owner"))
    return keys


def _logged_in_owner_with_csrf_cookie(client):
    keys = _logged_in_owner(client)
    client.cookies[settings.CSRF_COOKIE_NAME] = CSRF_SECRET
    return keys


def _put_case(name, setup, headers):
    return Case(
        name,
        "PUT",
        PROFILE.path(),
        setup,
        body=PROFILE.body("a"),
        headers=lambda keys: {
            "If-Match": quote_etag(Resource.objects.get(pk=keys["item"]).etag),
            **headers,
        },
    )


WRONG_BASIC_PASSWORD = Case(
    "wrong-basic-password",
    "GET",
    PROFILE.path(),
    _owner,
    headers=_basic("owner", "wrong"),
)
UNKNOWN_TOKEN = Case(
    "unknown-token", "GET", PROFILE.path(), _owner, headers=_token("nope")
)
ANONYMOUS_GET = Case("anonymous-get", "GET", PROFILE.path(), _owner)

DRF_HOOK_CASES = [
    # status, case
    (200, Case("basic-get", "GET", PROFILE.path(), _owner, headers=_basic("owner"))),
    (200, _put_case("basic-put-without-csrf", _owner, _basic("owner"))),
    (200, Case("token-get", "GET", PROFILE.path(), _owner, headers=_token("t0k3n"))),
    (200, _put_case("token-put-without-csrf", _owner, _token("t0k3n"))),
    (401, WRONG_BASIC_PASSWORD),
    (401, UNKNOWN_TOKEN),
    (403, _put_case("session-put-without-csrf", _logged_in_owner, {})),
    (
        200,
        _put_case(
            "session-put-with-csrf",
            _logged_in_owner_with_csrf_cookie,
            {"X-CSRFToken": CSRF_SECRET},
        ),
    ),
    (
        403,
        Case(
            "session-post-without-csrf",
            "POST",
            "{mount}partner-profiles/",
            _logged_in_owner,
            body=PROFILE.body("a"),
        ),
    ),
    (
        201,
        Case(
            "basic-post-without-csrf",
            "POST",
            "{mount}partner-profiles/",
            _owner,
            body=PROFILE.body("a"),
            headers=_basic("owner"),
        ),
    ),
    (200, Case("session-get", "GET", PROFILE.path(), _logged_in_owner)),
    (401, ANONYMOUS_GET),
]


@pytest.fixture
def drf_hooked(monkeypatch):
    """`hooking_drf` with a function setting the authentication classes."""
    stack = contextlib.ExitStack()
    with stack:
        yield lambda classes: stack.enter_context(hooking_drf(monkeypatch, classes))


@contextlib.contextmanager
def hooking_drf(monkeypatch, classes):
    """ninja resolves its user with `drf_user`, and both front ends
    authenticate with `classes`, in order (DRF's routes by their views'
    classes, the hook by ``SCRUD_DRF_AUTHENTICATION``), each case's client
    enforcing CSRF as a browser's would."""
    monkeypatch.setattr(APIView, "authentication_classes", classes)
    monkeypatch.setattr(
        front_ends, "Client", functools.partial(Client, enforce_csrf_checks=True)
    )
    with override_settings(
        SCOPED_RBAC=DRF_HOOKED,
        SCRUD_DRF_AUTHENTICATION=[
            f"{cls.__module__}.{cls.__qualname__}" for cls in classes
        ],
    ):
        yield


def _parity(case, uniform):
    with override_settings(
        SCOPED_RBAC={**settings.SCOPED_RBAC, "UNIFORM_ERRORS": uniform}
    ):
        return parity(case)


@pytest.mark.parametrize(
    "status,case", DRF_HOOK_CASES, ids=lambda value: getattr(value, "id", value)
)
@pytest.mark.parametrize("uniform", [True, False])
def test_the_drf_hook_answers_as_the_drf_routes(drf_hooked, status, case, uniform):
    drf_hooked(SESSION_FIRST)
    drf, ninja = _parity(case, uniform)

    assert drf.status == status, drf
    assert drf == ninja
    if status == 403:
        assert "CSRF Failed" in ninja.body
    if status == 401:
        # DRF's first authenticator, the session's, has no challenge.
        assert "WWW-Authenticate" not in ninja.headers


@pytest.mark.parametrize(
    "classes,challenge",
    [(BASIC_FIRST, 'Basic realm="api"'), (TOKEN_FIRST, "Token")],
    ids=["basic-first", "token-first"],
)
@pytest.mark.parametrize(
    "case", [WRONG_BASIC_PASSWORD, UNKNOWN_TOKEN, ANONYMOUS_GET], ids=lambda c: c.id
)
@pytest.mark.parametrize("uniform", [True, False])
def test_the_drf_hook_challenges_as_the_first_authenticator(
    drf_hooked, classes, challenge, case, uniform
):
    drf_hooked(classes)
    drf, ninja = _parity(case, uniform)

    assert drf.status == 401, drf
    assert drf.headers["WWW-Authenticate"] == challenge, drf
    assert drf == ninja


class RefusingAuthentication(BaseAuthentication):
    """An authenticator raising another ``APIException`` than DRF's
    authentication errors for an ``Authorization: Refuse <kind>`` header."""

    def authenticate(self, request):
        scheme, _, kind = request.headers.get("Authorization", "").partition(" ")
        if scheme != "Refuse":
            return None
        if kind == "throttled":
            raise Throttled(wait=7)
        if kind == "invalid":
            raise ValidationError({"credential": ["Not a credential."]})
        raise PermissionDenied("Refused here.")


@pytest.mark.parametrize(
    "kind,status", [("throttled", 429), ("invalid", 400), ("denied", 403)]
)
@pytest.mark.parametrize("uniform", [True, False])
def test_the_drf_hook_answers_another_authenticator_error_as_drf(
    drf_hooked, kind, status, uniform
):
    drf_hooked([RefusingAuthentication, *SESSION_FIRST])
    case = Case(
        f"refused-{kind}",
        "GET",
        PROFILE.path(),
        _owner,
        headers={"Authorization": f"Refuse {kind}"},
    )
    drf, ninja = _parity(case, uniform)

    assert drf.status == status, drf
    if kind == "throttled":
        assert drf.headers["Retry-After"] == "7", drf
    assert drf == ninja


def test_basic_credentials_of_the_session_user_are_csrf_checked_on_ninja(
    drf_hooked,
):
    """Stricter than DRF, and safe: with Basic authentication first, DRF takes
    the session user's own Basic credentials sent with its session cookie as
    Basic (no CSRF check), while ninja's ``resolve_user`` CSRF-checks the
    session's user however the hook found it (follow-up
    ``scoped-rbac-ninja-user-hook-csrf-doc``)."""
    drf_hooked(BASIC_FIRST)
    case = _put_case("session-and-basic-put", _logged_in_owner, _basic("owner"))
    drf, ninja = parity(case)

    assert drf.status == 200, drf
    assert ninja.status == 403, ninja
    assert json.loads(ninja.body)["detail"].startswith("CSRF Failed"), ninja


def test_the_drf_hook_runs_drfs_default_authentication_classes(item):
    # Without SCRUD_DRF_AUTHENTICATION: DRF's DEFAULT_AUTHENTICATION_CLASSES
    # (Session and Basic in the tests), which have no Token authentication.
    Token.objects.create(user=User.objects.get(username="boss"), key="t0k3n")
    url = f"{NINJA}partner-profiles/{item.pk}/"
    with override_settings(SCOPED_RBAC=DRF_HOOKED):
        assert Client().get(url, headers=_basic()).status_code == 200
        assert Client().get(url, headers=_token("t0k3n")).status_code == 401
    with override_settings(
        SCOPED_RBAC=DRF_HOOKED,
        SCRUD_DRF_AUTHENTICATION=[TokenAuthentication],
    ):
        assert Client().get(url, headers=_token("t0k3n")).status_code == 200
        # Basic is not among the classes now: the anonymous user.
        assert Client().get(url, headers=_basic()).status_code == 401


def test_the_drf_hook_leaves_the_requests_user_to_resolve_user(item):
    from django.contrib.auth.models import AnonymousUser
    from django.test import RequestFactory

    from scrud_django.drf.auth import drf_user

    request = RequestFactory().get("/", headers=_basic("boss", "wrong"))
    request.user = AnonymousUser()
    session_user = request.user
    with pytest.raises(AuthenticationError):
        drf_user(request)
    assert request.user is session_user
    request = RequestFactory().get("/", headers=_basic())
    assert drf_user(request) == User.objects.get(username="boss")
    assert "user" not in vars(request)
