"""The ``Http`` steps over ``django.test.Client`` (`tests.steps.PlainClient`),
which they use where DRF is not installed or a test opts in (the
``plain_client`` fixture), on the ninja URLconf; and the ``drf`` marker's
skipping when DRF cannot be imported (`tests.conftest.mark_drf_modules`)."""

import json
from copy import copy
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import safetydance_django.steps as http_steps
from django.test import Client
from django.test.client import MULTIPART_CONTENT
from safetydance_django.test import *  # noqa: F403, F401

from . import conftest
from .fixtures import *  # noqa
from .fixtures import RESOURCE_ENDPOINT_DETAIL_NAME, RESOURCE_ENDPOINT_LIST_NAME
from .front_ends import api_url
from .steps import *  # noqa: F403, F401
from .steps import PlainClient

ninja_only = pytest.mark.parametrize("front_end", ["ninja"], indirect=True)


def test_the_client_is_a_plain_django_test_client(plain_client):
    client = PlainClient()
    assert isinstance(client, Client)
    assert not client.handler.enforce_csrf_checks


@pytest.mark.django_db
@ninja_only
@test_context
def test_the_steps_use_the_plain_client(plain_client, front_end, admin_login):
    Given.http.force_login(admin_login)
    assert type(Http.api_client) is PlainClient


@pytest.mark.django_db
@ninja_only
@test_context
def test_a_round_trip_over_the_plain_client(
    plain_client, front_end, admin_login, partner_profile_post_data
):
    list_url = api_url(front_end, RESOURCE_ENDPOINT_LIST_NAME)

    Given.http.force_login(admin_login)
    When.http.post(list_url, data=partner_profile_post_data, format="json")
    Then.http.status_code_is(HTTPStatus.CREATED)
    And.http.content_type_is("application/json")
    And.http.resource_json_is(partner_profile_post_data)
    slug = Http.response["Location"].rstrip("/").split("/")[-1]
    assert type(Http.api_client) is PlainClient

    detail_url = api_url(
        front_end,
        RESOURCE_ENDPOINT_DETAIL_NAME,
        kwargs={"slug": slug},
    )
    When.http.get(detail_url)
    Then.http.status_code_is(HTTPStatus.OK)
    And.http.resource_json_is(partner_profile_post_data)
    response = Http.response

    changed = copy(partner_profile_post_data)
    changed["display_name"] = "Changed"
    When.http.put(
        detail_url,
        data=json.dumps(changed),
        content_type="application/json",
        HTTP_IF_UNMODIFIED_SINCE=response["Last-Modified"],
        HTTP_IF_MATCH=response["ETag"],
    )
    Then.http.status_code_is(HTTPStatus.OK)
    And.http.response_json_matches({"display_name": "Changed"})

    When.http.get(detail_url)
    response = Http.response
    When.http.delete(
        detail_url,
        HTTP_IF_UNMODIFIED_SINCE=response["Last-Modified"],
        HTTP_IF_MATCH=response["ETag"],
    )
    Then.http.status_code_is(HTTPStatus.NO_CONTENT)

    When.http.get(detail_url)
    Then.http.status_code_is(HTTPStatus.NOT_FOUND)


@pytest.mark.django_db
@ninja_only
@test_context
def test_a_json_body_is_serialized_with_its_content_type(plain_client, front_end):
    client = PlainClient()
    seen = {}

    def request(self, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(status_code=204)

    Client.request, saved = request, Client.request
    try:
        client.put("/x/", data={"a": 1}, format="json")
        assert seen["CONTENT_TYPE"] == "application/json"
        assert json.loads(seen["wsgi.input"].read()) == {"a": 1}
    finally:
        Client.request = saved


# the drf marker


class FakeItem:
    def __init__(self, name, *marks):
        self.path = Path(f"tests/{name}.py")
        self.marks = list(marks)

    def add_marker(self, mark):
        self.marks.append(mark.mark)

    def get_closest_marker(self, name):
        return next((mark for mark in self.marks if mark.name == name), None)


def test_a_drf_module_is_marked_and_skipped_without_drf(monkeypatch):
    monkeypatch.setattr(conftest, "drf_importable", lambda: False)
    drf_module = FakeItem("test_drf_package")
    own_mark = FakeItem("test_models", pytest.mark.drf.mark)
    neutral = FakeItem("test_models")

    conftest.mark_drf_modules([drf_module, own_mark, neutral])

    assert drf_module.get_closest_marker("drf")
    assert drf_module.get_closest_marker("skip")
    assert own_mark.get_closest_marker("skip")
    assert not neutral.get_closest_marker("drf")
    assert not neutral.get_closest_marker("skip")


def test_a_drf_module_is_marked_but_runs_with_drf(monkeypatch):
    monkeypatch.setattr(conftest, "drf_importable", lambda: True)
    drf_module = FakeItem("test_drf_package")

    conftest.mark_drf_modules([drf_module])

    assert drf_module.get_closest_marker("drf")
    assert not drf_module.get_closest_marker("skip")


def test_every_drf_module_name_is_a_test_module():
    tests = {path.stem for path in Path(__file__).parent.glob("test_*.py")}
    assert not set(conftest.DRF_MODULES) - tests


# the private factory the steps' client comes from


def test_safetydance_builds_its_client_through_new_api_client():
    assert callable(http_steps._new_api_client)
    sentinel = object()
    with mock.patch.object(http_steps, "_new_api_client", lambda: sentinel):

        @test_context
        def client_of_a_new_context():
            return Http.api_client

        assert client_of_a_new_context() is sentinel


# format


def sent(client, method, path, *args, **kwargs):
    """The method, content type and body the client sends."""
    seen = {}

    def request(self, **environ):
        seen["method"] = environ["REQUEST_METHOD"]
        seen["content_type"] = environ.get("CONTENT_TYPE")
        body = environ.get("wsgi.input")
        seen["body"] = body.read() if body else b""
        return SimpleNamespace(status_code=204)

    with mock.patch.object(Client, "request", request):
        getattr(client, method)(path, *args, **kwargs)
    return seen


def test_multipart_is_encoded_for_every_method():
    client = PlainClient()
    for method in ("post", "put", "patch"):
        seen = sent(client, method, "/x/", {"a": ["1", "2"]}, format="multipart")
        assert seen["method"] == method.upper()
        assert seen["content_type"].startswith(MULTIPART_CONTENT)
        assert b'name="a"' in seen["body"] and b"2" in seen["body"]


def test_an_unknown_format_is_a_value_error():
    with pytest.raises(ValueError, match="xml"):
        PlainClient().post("/x/", {"a": 1}, format="xml")


def test_requests_match_apiclients_in_method_content_type_and_body():
    """The requests of `tests.test_context_smuggling` (and a JSON one) are
    the same for both clients."""
    pytest.importorskip("rest_framework")
    from rest_framework.test import APIClient

    smuggling = {"foo": "pwn", "rbac_context": ["victim", "attacker"]}
    cases = [
        ("post", ("/t/", smuggling), {"format": "multipart"}),
        ("put", ("/t/1/", smuggling), {"format": "multipart", "HTTP_IF_MATCH": "x"}),
        (
            "put",
            ("/t/1/", "a=b"),
            {"content_type": "application/x-www-form-urlencoded"},
        ),
        ("post", ("/t/", {"a": [1, {"b": None}]}), {"format": "json"}),
        ("delete", ("/t/1/",), {}),
    ]
    for method, args, kwargs in cases:
        assert sent(PlainClient(), method, *args[:1], *args[1:], **kwargs) == sent(
            APIClient(), method, *args[:1], *args[1:], **kwargs
        ), (method, args, kwargs)
