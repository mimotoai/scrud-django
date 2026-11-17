"""Errors answered without a front end (`scrud_django.core.errors`): the
status, body and headers `error_answer` gives an exception are those the DRF
routes answer it with (``scrud_exception_handler``, the configured
``EXCEPTION_HANDLER``), uniform errors on and off."""

import json

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.http import Http404
from django.test import RequestFactory, override_settings
from django.urls import reverse
from fastjsonschema import JsonSchemaValueException
from rest_framework import exceptions as drf
from rest_framework.authentication import SessionAuthentication
from rest_framework.renderers import JSONRenderer
from rest_framework.request import ForcedAuthentication, Request
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access

from scrud_django.core.errors import error_answer, parse_error_detail
from scrud_django.drf.exceptions import scrud_exception_handler
from scrud_django.exceptions import (
    ContentFilterError,
    JsonValidationError,
    MethodNotAllowedError,
    QueryParameterError,
    RequestBodyMalformedError,
    UIComponentNameTakenError,
)

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401

CHALLENGE = 'Basic realm="api"'


def uniform_errors_set(uniform):
    return override_settings(
        SCOPED_RBAC=dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=uniform)
    )


def same(exc):
    """A case whose DRF exception is the core's own."""
    return exc, exc


def json_invalid():
    return JsonValidationError(
        JsonSchemaValueException(
            "data.name must be string",
            value=1,
            name="data.name",
            definition={"type": "string"},
            rule="type",
        )
    )


# (core exception, the exception the DRF route answers for it), by id.
CASES = {
    "UIComponentNameTakenError": same(UIComponentNameTakenError("row")),
    "JsonValidationError": same(json_invalid()),
    "QueryParameterError": same(QueryParameterError({"a": ["Not a key."]})),
    # The scrud errors standing for an error DRF answers through
    # scoped-rbac's handler: answered as that one.
    "ContentFilterError": (
        ContentFilterError({"0": ["An integer is not a content key."]}),
        drf.ValidationError({"0": ["An integer is not a content key."]}),
    ),
    "MethodNotAllowedError": (
        MethodNotAllowedError("PUT"),
        drf.MethodNotAllowed("PUT"),
    ),
    "RequestBodyMalformedError": (
        RequestBodyMalformedError("JSON parse error - Expecting value"),
        drf.ParseError("JSON parse error - Expecting value"),
    ),
    "AccessDenied": same(access.AccessDenied()),
    "AccessDenied-message": same(access.AccessDenied("Not in this context.")),
    "ObjectNotFound": same(access.ObjectNotFound()),
    "ReservedContext": same(access.ReservedContext()),
    "PrimaryKeyChanged": same(access.PrimaryKeyChanged()),
    "InvalidContext": same(access.InvalidContext()),
    "ConditionalListTooLarge": same(access.ConditionalListTooLarge()),
    "Http404": same(Http404()),
    "Http404-message": same(Http404("No such branch.")),
    "Http404-empty": same(Http404("")),
    "Http404-list": same(Http404(["a", "b"])),
    "ObjectDoesNotExist": (ObjectDoesNotExist("Resource does not exist."), Http404()),
    "PermissionDenied": same(PermissionDenied()),
    "PermissionDenied-message": same(PermissionDenied("Not yours.")),
}

# The cases answered for the anonymous user: an AccessDenied is DRF's
# NotAuthenticated.
ANONYMOUS_CASES = {
    "AccessDenied": same(access.AccessDenied()),
    "NotAuthenticated": (access.AccessDenied(), drf.NotAuthenticated()),
    "Http404": same(Http404()),
}


class Challenging:
    """A view whose first authenticator challenges (DRF's
    ``get_authenticate_header``)."""

    def get_authenticate_header(self, request):
        return CHALLENGE


def drf_answer(exc, user, *, view=None):
    """``(status, body, headers)`` of DRF's exception handler for `exc`
    raised serving a request by `user` (authenticated by the session for the
    anonymous, as the DRF routes are)."""
    django_request = RequestFactory().get("/")
    if user.is_authenticated:
        request = Request(
            django_request, authenticators=[ForcedAuthentication(user, None)]
        )
    else:
        django_request.user = user
        request = Request(django_request, authenticators=[SessionAuthentication()])
    request.accepted_renderer = JSONRenderer()
    request.accepted_media_type = JSONRenderer.media_type
    response = scrud_exception_handler(exc, {"request": request, "view": view})
    body = json.loads(json.dumps(response.data))
    headers = {"Content-Type": response.content_type or "application/json"}
    for name in ("Link", "WWW-Authenticate"):
        if response.has_header(name):
            headers[name] = response[name]
    return response.status_code, body, headers


def core_answer(exc, user, **kwargs):
    request = RequestFactory().get("/")
    request.user = user
    status, body, headers = error_answer(exc, request, **kwargs)
    return status, json.loads(json.dumps(body)), headers


@pytest.mark.django_db
@pytest.mark.parametrize("uniform", [True, False])
@pytest.mark.parametrize("case", CASES)
def test_error_answer_matches_the_drf_answer(admin_login, case, uniform):
    core_exc, drf_exc = CASES[case]
    with uniform_errors_set(uniform):
        assert core_answer(core_exc, admin_login) == drf_answer(drf_exc, admin_login)


@pytest.mark.django_db
@pytest.mark.parametrize("uniform", [True, False])
@pytest.mark.parametrize("case", ANONYMOUS_CASES)
def test_error_answer_matches_the_drf_answer_to_the_anonymous(case, uniform):
    core_exc, drf_exc = ANONYMOUS_CASES[case]
    anonymous = AnonymousUser()
    with uniform_errors_set(uniform):
        assert core_answer(core_exc, anonymous) == drf_answer(drf_exc, anonymous)
        if drf_exc is core_exc and isinstance(core_exc, access.AccessDenied):
            # The route's first authenticator challenges.
            status, _body, headers = core_answer(
                core_exc, anonymous, challenge=CHALLENGE
            )
            assert status == 401
            assert headers["WWW-Authenticate"] == CHALLENGE
            assert (status, _body, headers) == drf_answer(
                drf_exc, anonymous, view=Challenging()
            )


@pytest.mark.parametrize("exc", [ValueError("x"), KeyError("k"), RuntimeError()])
def test_error_answer_leaves_other_errors_to_the_caller(exc):
    assert error_answer(exc) is None


def test_error_answer_without_a_request_denies_with_a_403():
    status, body, _headers = error_answer(access.AccessDenied())
    assert status == 403
    assert body["status"] == 403


@pytest.mark.django_db
@pytest.mark.parametrize("uniform", [True, False])
@test_context
def test_a_malformed_body_is_answered_as_the_route_answers_it(
    admin_login, todo_list, uniform
):
    body = '{"name": '
    request = RequestFactory().put("/", body, content_type="application/json")
    request.user = admin_login
    with uniform_errors_set(uniform):
        status, answered, headers = error_answer(
            RequestBodyMalformedError(parse_error_detail(request)), request
        )
        Given.http.force_authenticate(admin_login)
        When.http.put(
            reverse("todo-lists-detail", args=[todo_list.pk]),
            data=body,
            content_type="application/json",
            HTTP_IF_MATCH=f'"{todo_list.etag}"',
        )
    assert status == Http.response.status_code == 400
    assert answered == Http.response.json()
    assert headers["Content-Type"] == Http.response["Content-Type"]
    if uniform:
        assert Http.response["Link"].startswith(headers["Link"])
    else:
        assert answered["detail"].startswith("JSON parse error - ")


def precondition_exchange(todo_list, user, method, headers, uniform):
    """``(core, api)``: the core's answer to the failed precondition of a
    `method` request with `headers` on `todo_list`, and the DRF route's
    response to it."""
    from scrud_django.core.resources import item_precondition_answer

    request = getattr(RequestFactory(), method.lower())(
        "/", **{f"HTTP_{k.upper().replace('-', '_')}": v for k, v in headers.items()}
    )
    request.user = user
    with uniform_errors_set(uniform):
        core = item_precondition_answer(
            request,
            etag=todo_list.etag,
            last_modified=todo_list.modified_at,
            method=method,
        )
        Given.http.force_authenticate(user)
        sent = {f"HTTP_{k.upper().replace('-', '_')}": v for k, v in headers.items()}
        url = reverse("todo-lists-detail", args=[todo_list.pk])
        if method == "GET":
            When.http.get(url, **sent)
        else:
            When.http.put(url, data=todo_list.content, format="json", **sent)
    return core, Http.response


@pytest.mark.django_db
@pytest.mark.parametrize("uniform", [True, False])
@pytest.mark.parametrize(
    "method, headers, status",
    [
        ("PUT", {}, 428),
        ("PUT", {"If-Match": '"stale"'}, 412),
        ("PUT", {"If-Match": "not an etag"}, 400),
    ],
)
@test_context
def test_item_precondition_answer_matches_the_drf_route(
    admin_login, todo_list, method, headers, status, uniform
):
    core, api = precondition_exchange(todo_list, admin_login, method, headers, uniform)
    answered, body, answered_headers = core
    assert answered == api.status_code == status
    assert body == api.json()
    assert answered_headers["Content-Type"] == api["Content-Type"]
    if uniform:
        assert api["Link"].startswith(answered_headers["Link"])
    else:
        assert "Link" not in answered_headers


@pytest.mark.django_db
@test_context
def test_item_precondition_answer_of_a_not_modified_get(admin_login, todo_list):
    from scrud_django.core.resources import item_preconditions

    core, api = precondition_exchange(
        todo_list, admin_login, "GET", {"If-None-Match": f'"{todo_list.etag}"'}, True
    )
    status, body, headers = core
    assert status == api.status_code == 304
    assert body == {}
    assert headers["ETag"] == api["ETag"]
    assert "Content-Type" not in headers
    request = RequestFactory().get("/", HTTP_IF_NONE_MATCH=f'"{todo_list.etag}"')
    assert (
        item_preconditions(
            request,
            etag=todo_list.etag,
            last_modified=todo_list.modified_at,
            method="GET",
        )
        == 304
    )


@pytest.mark.django_db
@test_context
def test_item_precondition_answer_proceeds_when_nothing_fails(todo_list):
    from scrud_django.core.resources import item_precondition_answer

    request = RequestFactory().put("/", HTTP_IF_MATCH=f'"{todo_list.etag}"')
    assert (
        item_precondition_answer(
            request,
            etag=todo_list.etag,
            last_modified=todo_list.modified_at,
            method="PUT",
        )
        is None
    )
