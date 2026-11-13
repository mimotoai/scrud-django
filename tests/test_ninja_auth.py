"""What ninja does with the credentials of a request: scoped-rbac's
``resolve_user`` (the ``SCOPED_RBAC["NINJA_USER"]`` hook, else ``request.auth``,
else the session), not DRF's ``DEFAULT_AUTHENTICATION_CLASSES``. The README
shows `tests.basic_auth_hook.basic_user`, the hook used here."""

import base64
import json
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.utils.http import quote_etag

from scrud_django.models import Resource

from .front_ends import MOUNTS, serve_both
from .test_front_end_parity import PROFILE

pytestmark = [pytest.mark.urls("tests.parity_urls"), pytest.mark.django_db]

NINJA = MOUNTS["ninja"]
HOOKED = {**settings.SCOPED_RBAC, "NINJA_USER": "tests.basic_auth_hook.basic_user"}


def _basic(username="boss", password="pw"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


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
        # Not a session user: no CSRF token is needed.
        written = _put(client, item, **_basic())
        assert written.status_code == 200, written.content
    assert Resource.objects.get(pk=item.pk).content["name"] == "y"


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
