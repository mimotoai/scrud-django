"""The ninja type routers in a URLconf of their own, without the DRF routes
(`tests.ninja_only_urls`): what they link to (their Links and their HTTP
Schema Resources) is served by the same API."""

import json
from urllib.parse import urlsplit

import pytest
from django.contrib.auth.models import User
from django.urls import resolve

from scrud_django import registration

from .front_ends import HSR_REL, split_links

pytestmark = [pytest.mark.urls("tests.ninja_only_urls"), pytest.mark.django_db]


def _links(response):
    """`response`'s Link targets, by relation."""
    links = {}
    for link in split_links(response["Link"]):
        rel = link.split('rel="', 1)[1].split('"', 1)[0]
        links[rel] = link[1 : link.index(">")]
    return links


def test_a_ninja_only_apis_links_and_hsr_resolve_within_it(client):
    client.force_login(
        User.objects.create(username="admin", is_staff=True, is_superuser=True)
    )
    profile = registration.ResourceRegistration.register(
        {"name": "x", "rbac_context": "a"}, "partner-profiles"
    )
    rtype = registration.get_resource_type_for("partner-profiles")

    response = client.get(f"/api/partner-profiles/{profile.pk}/")

    assert response.status_code == 200, response.content
    links = _links(response)
    schema = f"http://testserver/api/json-schema/{rtype.schema.pk}/"
    context = f"http://testserver/api/json-ld/{rtype.context.pk}/"
    assert links["describedby"] == schema
    assert links["http://www.w3.org/ns/json-ld#context"] == context
    document = client.get(links[HSR_REL])
    assert document.status_code == 200, document.content
    text = document.content.decode()
    json.loads(text)
    assert schema in text, text
    assert context in text, text
    for url in (schema, context):
        resolve(urlsplit(url).path)
        assert client.get(url).status_code == 200
