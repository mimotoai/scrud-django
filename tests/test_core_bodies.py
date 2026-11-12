"""The body checks and links without DRF (`scrud_django.core.bodies`,
`scrud_django.core.urls`): asked with a parsed body, and a plain Django
request."""

import pytest
from django.test import RequestFactory
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.core.bodies import check_body, max_body_depth
from scrud_django.core.urls import absolute_reverse
from scrud_django.exceptions import (
    RbacContextInvalidError,
    RequestBodyMissingError,
    RequestBodyNulError,
    RequestBodyTooDeepError,
)

from .steps import *  # noqa: F403, F401


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
