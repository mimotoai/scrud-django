"""What ninja does with the credentials of a request: scoped-rbac's
``resolve_user`` (the ``SCOPED_RBAC["NINJA_USER"]`` hook, else ``request.auth``,
else the session), not DRF's ``DEFAULT_AUTHENTICATION_CLASSES`` unless the hook
is scrud's `scrud_django.drf.auth.drf_user`, which runs them. The README shows
`tests.basic_auth_hook.basic_user`, the hook used here. scrud's DRF hook,
`scrud_django.drf.auth.drf_user`, is `tests.test_ninja_drf_auth`'s: these
run without DRF too."""

import base64
import json
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.utils.http import quote_etag

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration

from .front_ends import MOUNTS, serve_both

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
    return ResourceRegistration.register(
        {"name": "x", "rbac_context": "a"}, "partner-profiles"
    )


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
