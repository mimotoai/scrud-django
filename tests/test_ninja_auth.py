"""What ninja does with the credentials of a request: scoped-rbac's
``resolve_user`` (the ``SCOPED_RBAC["NINJA_USER"]`` hook, else ``request.auth``,
else the session), not DRF's ``DEFAULT_AUTHENTICATION_CLASSES`` unless the hook
is scrud's `scrud_django.drf.auth.drf_user`, which runs them. The README shows
`tests.basic_auth_hook.basic_user`, the hook used by the first tests here."""

import base64
import functools
import json
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.utils.http import quote_etag
from ninja.errors import AuthenticationError
from rest_framework.authentication import (
    BasicAuthentication,
    SessionAuthentication,
    TokenAuthentication,
)
from rest_framework.authtoken.models import Token
from rest_framework.views import APIView

from scrud_django.models import Resource

from . import front_ends
from .front_ends import MOUNTS, Case, parity, serve_both
from .test_front_end_parity import PROFILE

pytestmark = [pytest.mark.urls("tests.parity_urls"), pytest.mark.django_db]

NINJA = MOUNTS["ninja"]
HOOKED = {**settings.SCOPED_RBAC, "NINJA_USER": "tests.basic_auth_hook.basic_user"}


def _basic(username="boss", password="pw"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _token(key):
    return {"Authorization": f"Token {key}"}


@pytest.fixture
def item():
    serve_both()
    User.objects.create_superuser("boss", "boss@example.com", "pw")
    return PROFILE.store("a")


def _put(client, item, **headers):
    return client.put(
        f"{NINJA}partner-profiles/{item.pk}/",
        json.dumps({"name": "y", "rbac_context": "a"}),
        content_type="application/json",
        headers={"If-Match": quote_etag(item.etag), **headers},
    )


def test_basic_credentials_alone_are_the_anonymous_user_on_ninja(item):
    client = Client()
    services = client.get(f"{NINJA}services/", headers=_basic())
    assert services.status_code == 200, services.content
    assert services.json() == client.get(f"{NINJA}services/").json()

    read = client.get(f"{NINJA}partner-profiles/{item.pk}/", headers=_basic())
    assert read.status_code == 401, read.content
    # DRF authenticates the same header (its default Basic authentication).
    drf = client.get(f"/drf/partner-profiles/{item.pk}/", headers=_basic())
    assert drf.status_code == 200, drf.content


def test_a_ninja_user_hook_maps_basic_credentials(item):
    client = Client(enforce_csrf_checks=True)
    with override_settings(SCOPED_RBAC=HOOKED):
        read = client.get(f"{NINJA}partner-profiles/{item.pk}/", headers=_basic())
        assert read.status_code == 200, read.content
        # A Basic-only client has no session: no CSRF token is needed.
        written = _put(client, item, **_basic())
        assert written.status_code == 200, written.content
    assert Resource.objects.get(pk=item.pk).content["name"] == "y"


def _session_client():
    client = Client(enforce_csrf_checks=True)
    client.force_login(User.objects.get(username="boss"))
    return client


def test_a_session_user_stays_that_user_with_the_hook_set(item):
    with override_settings(SCOPED_RBAC=HOOKED):
        read = _session_client().get(f"{NINJA}partner-profiles/{item.pk}/")
    assert read.status_code == 200, read.content


def test_a_session_users_write_is_csrf_checked_with_the_hook_set(item):
    with override_settings(SCOPED_RBAC=HOOKED):
        written = _put(_session_client(), item)
    assert written.status_code == 403, written.content
    assert b"CSRF Failed" in written.content


def test_a_session_cookie_with_basic_credentials_is_csrf_checked(item):
    with override_settings(SCOPED_RBAC=HOOKED):
        written = _put(_session_client(), item, **_basic())
    assert written.status_code == 403, written.content
    assert b"CSRF Failed" in written.content


@pytest.mark.parametrize("credentials", [_basic(password="wrong"), _basic("nobody")])
def test_a_ninja_user_hook_refuses_wrong_credentials(item, credentials):
    with override_settings(SCOPED_RBAC=HOOKED):
        response = Client().get(
            f"{NINJA}partner-profiles/{item.pk}/", headers=credentials
        )
    assert response.status_code == 401, response.content
    assert response.json()["code"] == "not_authenticated"


def test_a_request_the_hook_does_not_map_is_the_anonymous_user(item):
    with override_settings(SCOPED_RBAC=HOOKED):
        response = Client().get(f"{NINJA}partner-profiles/{item.pk}/")
    assert response.status_code == 401, response.content


def test_the_readme_shows_the_hook_these_tests_use():
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    hook = (Path(__file__).parent / "basic_auth_hook.py").read_text()
    indented = "".join(
        f"  {line}" if line.strip() else line for line in hook.splitlines(True)
    )
    assert indented.strip("\n") in readme


# scrud's own hook, `scrud_django.drf.auth.drf_user`: DRF's authentication
# classes run on the ninja request, so each client is the same user on both
# front ends, refused alike, and CSRF-checked alike. Each case runs on both
# (`front_ends.parity`, fresh data each), with a client enforcing CSRF.

DRF_HOOKED = {**settings.SCOPED_RBAC, "NINJA_USER": "scrud_django.drf.auth.drf_user"}
TOKEN_AUTHENTICATION = [
    SessionAuthentication,
    BasicAuthentication,
    TokenAuthentication,
]


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


DRF_HOOK_CASES = [
    # status, case
    (200, Case("basic-get", "GET", PROFILE.path(), _owner, headers=_basic("owner"))),
    (200, _put_case("basic-put-without-csrf", _owner, _basic("owner"))),
    (
        200,
        Case(
            "token-get",
            "GET",
            PROFILE.path(),
            _owner,
            headers=_token("t0k3n"),
        ),
    ),
    (
        200,
        _put_case("token-put-without-csrf", _owner, _token("t0k3n")),
    ),
    (
        401,
        Case(
            "wrong-basic-password",
            "GET",
            PROFILE.path(),
            _owner,
            headers=_basic("owner", "wrong"),
        ),
    ),
    (
        401,
        Case("unknown-token", "GET", PROFILE.path(), _owner, headers=_token("nope")),
    ),
    (403, _put_case("session-put-without-csrf", _logged_in_owner, {})),
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
    (401, Case("anonymous-get", "GET", PROFILE.path(), _owner)),
]


@pytest.fixture
def drf_hooked(monkeypatch):
    """ninja resolves its user with `drf_user`, and both front ends
    authenticate with Session, Basic and Token authentication (DRF's routes
    by their views' classes, the hook by ``SCRUD_DRF_AUTHENTICATION``), each
    case's client enforcing CSRF as a browser's would."""
    monkeypatch.setattr(APIView, "authentication_classes", TOKEN_AUTHENTICATION)
    monkeypatch.setattr(
        front_ends, "Client", functools.partial(Client, enforce_csrf_checks=True)
    )
    with override_settings(
        SCOPED_RBAC=DRF_HOOKED,
        SCRUD_DRF_AUTHENTICATION=[
            f"{cls.__module__}.{cls.__qualname__}" for cls in TOKEN_AUTHENTICATION
        ],
    ):
        yield


@pytest.mark.parametrize(
    "status,case", DRF_HOOK_CASES, ids=lambda value: getattr(value, "id", value)
)
@pytest.mark.parametrize("uniform", [True, False])
def test_the_drf_hook_answers_as_the_drf_routes(drf_hooked, status, case, uniform):
    with override_settings(
        SCOPED_RBAC={**settings.SCOPED_RBAC, "UNIFORM_ERRORS": uniform}
    ):
        drf, ninja = parity(case)

    assert drf.status == status, drf
    assert drf == ninja
    if status == 403:
        assert "CSRF Failed" in ninja.body


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
