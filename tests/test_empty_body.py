"""A POST or PUT with an empty body is refused with 400.

DRF reads an empty body as ``{}``: a POST created an empty resource and a PUT
wiped the stored content (authorized in the default context).
"""

from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework import status
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .steps import *  # noqa: F403, F401

# The JSON-LD context type has no schema: any JSON object is a valid body, so
# only the empty-body check refuses an empty one.
JSON_LD = "http://www.w3.org/ns/json-ld#context"
BODY_MISSING = "https://api.openteams.com/http-problem/body-missing"
ALL_METHODS = ("http.GET", "http.POST", "http.PUT", "http.DELETE")
BODY_MISSING_DETAIL = (
    "A POST or PUT needs a JSON object as its body, sent with a Content-Length "
    "header; this request has an empty body or no Content-Length."
)


def json_ld_writer():
    role = Role(
        definition={m: [JSON_LD] for m in ALL_METHODS},
        etag=uuid4().hex,
        modified_at=now(),
    )
    role.save()
    user = UserFactory()
    RoleAssignment(
        user=user,
        role=role,
        rbac_context=DEFAULT_CONTEXT,
        etag=uuid4().hex,
        modified_at=now(),
    ).save()
    return user


def assert_body_missing():
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    body = Http.response.json()
    assert body == {
        "type": BODY_MISSING,
        "title": "Missing request body",
        "detail": BODY_MISSING_DETAIL,
        "status": 400,
    }, body
    assert Http.response["Content-Type"].startswith("application/problem+json")


@pytest.mark.django_db
@pytest.mark.parametrize("content_type", ["application/json", None])
@test_context
def test_an_empty_post_is_400(content_type):
    reverse("json-ld-list")
    before = Resource.objects.count()
    Given.http.force_authenticate(json_ld_writer())
    When.http.post(reverse("json-ld-list"), content_type=content_type)
    assert_body_missing()
    assert Resource.objects.count() == before


@pytest.mark.django_db
@pytest.mark.parametrize("content_type", ["application/json", None])
@test_context
def test_an_empty_put_leaves_the_content_unchanged(content_type):
    reverse("json-ld-list")
    stored = ResourceRegistration.register({"name": "kept"}, "json-ld")
    Given.http.force_authenticate(json_ld_writer())
    When.http.put(
        reverse("json-ld-detail", args=[stored.pk]),
        content_type=content_type,
        HTTP_IF_MATCH=quote_etag(stored.etag),
    )
    assert_body_missing()
    unchanged = Resource.objects.get(pk=stored.pk)
    assert unchanged.content == {"name": "kept"}
    assert unchanged.etag == stored.etag


@pytest.mark.django_db
@test_context
def test_an_empty_body_is_refused_before_authorization():
    # As an empty rbac_context list: the body alone decides, so an anonymous
    # request learns nothing from the 400.
    reverse("json-ld-list")
    When.http.post(reverse("json-ld-list"), content_type="application/json")
    assert_body_missing()


@pytest.mark.django_db
@test_context
def test_an_empty_object_is_still_a_body():
    # "{}" is a JSON object: written as before (a schema, if any, decides).
    reverse("json-ld-list")
    Given.http.force_authenticate(json_ld_writer())
    When.http.post(reverse("json-ld-list"), data="{}", content_type="application/json")
    Then.http.status_code_is(status.HTTP_201_CREATED)
    assert Http.response.json() == {}


@pytest.mark.django_db
@test_context
def test_a_body_without_a_content_length_is_refused():
    # A chunked body (no Content-Length): DRF does not read it and would see
    # {}. Refused as having no body the server reads.
    reverse("json-ld-list")
    before = Resource.objects.count()
    Given.http.force_authenticate(json_ld_writer())
    Http.response = Http.api_client.generic(
        "POST",
        reverse("json-ld-list"),
        '{"name": "x"}',
        content_type="application/json",
        CONTENT_LENGTH="",
        HTTP_TRANSFER_ENCODING="chunked",
    )
    assert_body_missing()
    assert Resource.objects.count() == before


@pytest.mark.django_db
@test_context
def test_an_unrouted_method_is_405_whatever_its_body():
    reverse("json-ld-list")
    stored = ResourceRegistration.register({"name": "kept"}, "json-ld")
    detail = reverse("json-ld-detail", args=[stored.pk])
    Given.http.force_authenticate(json_ld_writer())
    for method, url in (("PATCH", detail), ("PUT", reverse("json-ld-list"))):
        for body in ("", '{"name": "x"}'):
            Http.response = Http.api_client.generic(
                method, url, body, content_type="application/json"
            )
            Then.http.status_code_is(status.HTTP_405_METHOD_NOT_ALLOWED)
    When.http.post(detail, content_type="application/json")
    Then.http.status_code_is(status.HTTP_405_METHOD_NOT_ALLOWED)
    assert Resource.objects.get(pk=stored.pk).content == {"name": "kept"}
