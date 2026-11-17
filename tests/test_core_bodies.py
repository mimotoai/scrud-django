"""The body checks and links without DRF (`scrud_django.core.bodies`,
`scrud_django.core.urls`): asked with a parsed body, and a plain Django
request."""

import pytest
from django.test import RequestFactory
from django.urls import reverse
from django.utils.http import quote_etag
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.core.bodies import (
    check_body,
    check_nul,
    max_body_depth,
    request_json,
)
from scrud_django.core.errors import error_answer
from scrud_django.core.urls import absolute_reverse
from scrud_django.exceptions import (
    RbacContextInvalidError,
    RequestBodyMalformedError,
    RequestBodyMediaTypeError,
    RequestBodyMissingError,
    RequestBodyNulError,
    RequestBodyTooDeepError,
)

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_core_errors import uniform_errors_set


def nested(depth):
    body = "x"
    for _ in range(depth):
        body = [body]
    return body


@pytest.mark.django_db
@test_context
def test_check_body_order():
    too_deep_with_nul = nested(max_body_depth() + 1)
    too_deep_with_nul[0].append("a\x00b")
    # The NUL alone would be refused, but the depth is checked first.
    with pytest.raises(RequestBodyNulError):
        check_nul(too_deep_with_nul)
    with pytest.raises(RequestBodyTooDeepError):
        check_body(too_deep_with_nul, present=True)
    with pytest.raises(RequestBodyNulError):
        check_body({"name": "a\x00b", "rbac_context": []}, present=True)
    with pytest.raises(RbacContextInvalidError):
        check_body({"rbac_context": []}, present=True)
    check_body({"rbac_context": "a"}, present=True)
    check_body([1, 2], present=True)


@pytest.mark.django_db
@test_context
def test_missing_body():
    with pytest.raises(RequestBodyMissingError):
        check_body(None, present=False)
    # Missing wins over everything the body could have.
    with pytest.raises(RequestBodyMissingError):
        check_body({"rbac_context": []}, present=False)


@pytest.mark.django_db
@test_context
def test_absolute_reverse_matches_drf():
    from rest_framework.reverse import reverse as drf_reverse

    for query in ("", "?format=json", "?a=1&format=api"):
        request = RequestFactory().get("/notes/" + query, HTTP_HOST="testserver")
        assert absolute_reverse(
            "collections-json-schema", args=["notes"], request=request
        ) == drf_reverse("collections-json-schema", args=["notes"], request=request)
    assert absolute_reverse("collections-json-schema", args=["notes"]) == drf_reverse(
        "collections-json-schema", args=["notes"]
    )


def test_view_mixins_keeps_its_public_names():
    from scrud_django.mixins import view_mixins

    for name in (
        "check_body_present",
        "check_body_depth",
        "check_body_nul",
        "check_written_contexts",
        "RbacContextInvalidError",
        "RequestBodyMissingError",
        "RequestBodyNulError",
        "RequestBodyTooDeepError",
        "rbac_contexts_of",
        "context_fits",
        "MAX_CONTEXT_BYTES",
        "BODY_METHODS",
        "MAX_BODY_DEPTH",
        "MARIADB_MAX_BODY_DEPTH",
        "body_depth",
        "body_has_nul",
        "max_body_depth",
    ):
        assert hasattr(view_mixins, name), name


def put_request(data, content_type):
    return RequestFactory().put("/", data, content_type=content_type)


@pytest.mark.parametrize(
    "data,content_type,expected",
    [
        ('{"a": [1, 2]}', "application/json", ({"a": [1, 2]}, True)),
        ('"text"', "application/json; charset=utf-8", ("text", True)),
        ("[1]", "*/*", ([1], True)),
        ("", "application/json", (None, False)),
        ("", "application/x-www-form-urlencoded", (None, False)),
    ],
)
def test_request_json_reads_the_body_as_drf_reads_it(data, content_type, expected):
    assert request_json(put_request(data, content_type)) == expected


@pytest.mark.parametrize(
    "data,content_type,error",
    [
        ('{"a": ', "application/json", RequestBodyMalformedError),
        ("NaN", "application/json", RequestBodyMalformedError),
        ("a=1", "application/x-www-form-urlencoded", RequestBodyMediaTypeError),
        ("{}", "application/problem+json", RequestBodyMediaTypeError),
        ("{}", "text/plain", RequestBodyMediaTypeError),
    ],
)
def test_request_json_refuses_what_drf_refuses(data, content_type, error):
    with pytest.raises(error):
        request_json(put_request(data, content_type))


@pytest.mark.django_db
@pytest.mark.parametrize("uniform", [True, False])
@pytest.mark.parametrize(
    "data,content_type",
    [
        ("title=x", "application/x-www-form-urlencoded"),
        ('{"title": ', "application/json"),
        ("", "application/json"),
    ],
)
@test_context
def test_a_refused_body_is_answered_as_the_route_answers_it(
    admin_login, todo_list, uniform, data, content_type
):
    request = put_request(data, content_type)
    request.user = admin_login
    with uniform_errors_set(uniform):
        try:
            body, present = request_json(request)
            check_body(body, present=present)
        except (
            RequestBodyMalformedError,
            RequestBodyMediaTypeError,
            RequestBodyMissingError,
        ) as exc:
            status, answered, headers = error_answer(exc, request)
        Given.http.force_authenticate(admin_login)
        When.http.put(
            reverse("todo-lists-detail", args=[todo_list.pk]),
            data=data,
            content_type=content_type,
            HTTP_IF_MATCH=quote_etag(todo_list.etag),
        )
    assert status == Http.response.status_code
    assert answered == Http.response.json()
    assert headers["Content-Type"] == Http.response["Content-Type"]
