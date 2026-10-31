"""A POST or PUT body with a NUL character (``\\u0000``) in any string, a key
or a value at any depth, is refused with a 400 problem on every database.

PostgreSQL's ``jsonb`` cannot hold one: such a POST raised ``DataError``, a
500, on PostgreSQL and stored the resource on SQLite.
"""

import json

import pytest
from django.urls import reverse
from django.utils.http import quote_etag
from rest_framework.test import APIClient

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory

BODY_NUL = "https://api.openteams.com/http-problem/body-nul-character"

# JSON texts as a client sends them: the NUL is the escape \u0000.
NUL_BODIES = [
    '{"name": "x\\u0000y"}',
    '{"name": "x", "nested": {"deeper": ["ok", "a\\u0000"]}}',
    '{"name": "x", "a\\u0000b": 1}',
    '{"name": "x", "nested": [{"k\\u0000": "v"}]}',
    '{"name": "x", "rbac_context": "c\\u0000d"}',
    '{"name": "x", "rbac_context": ["a", "\\u0000"]}',
]


def admin_client():
    client = APIClient()
    client.force_authenticate(UserFactory(is_staff=True, is_superuser=True))
    return client


def assert_refused(response):
    assert response.status_code == 400, response.content
    assert response["Content-Type"].startswith("application/problem+json")
    body = json.loads(response.content)
    assert body == {
        "type": BODY_NUL,
        "title": "NUL character in request body",
        "detail": "A request body may not contain a NUL character (\\u0000) in "
        "any string, key or value.",
        "status": 400,
    }, body


@pytest.mark.django_db
@pytest.mark.parametrize("body", NUL_BODIES)
def test_a_post_with_a_nul_anywhere_is_400(body):
    url = reverse("json-ld-list")
    count = Resource.objects.count()
    response = admin_client().post(url, body, content_type="application/json")
    assert_refused(response)
    assert Resource.objects.count() == count


@pytest.mark.django_db
@pytest.mark.parametrize("body", NUL_BODIES)
def test_a_put_with_a_nul_anywhere_is_400_and_changes_nothing(body):
    reverse("json-ld-list")
    stored = ResourceRegistration.register({"name": "kept"}, "json-ld")
    response = admin_client().put(
        reverse("json-ld-detail", args=[stored.pk]),
        body,
        content_type="application/json",
        HTTP_IF_MATCH=quote_etag(stored.etag),
    )
    assert_refused(response)
    assert Resource.objects.get(pk=stored.pk).content == {"name": "kept"}


@pytest.mark.django_db
def test_other_control_characters_are_still_stored():
    url = reverse("json-ld-list")
    response = admin_client().post(
        url, '{"name": "x\\u0001\\u001fy"}', content_type="application/json"
    )
    assert response.status_code == 201, response.content
    pk = int(response["Location"].rstrip("/").rsplit("/", 1)[1])
    assert Resource.objects.get(pk=pk).content == {"name": "x\x01\x1fy"}
