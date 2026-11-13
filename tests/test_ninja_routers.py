"""The ninja type routers in a URLconf of their own, without the DRF routes
(`tests.ninja_only_urls`): what they link to (their Links and their HTTP
Schema Resources) is served by the same API."""

import json
from urllib.parse import urlsplit

import pytest
from django.contrib.auth.models import User
from django.urls import resolve
from scoped_rbac.models import Role, RoleAssignment

from scrud_django import collection_type_uri_for, registration, services

from .front_ends import HSR_REL, split_links
from .resource_types import PARTNER_PROFILES_TYPE, SUMMARIES_TYPE

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


def test_a_ninja_only_apis_list_links_and_hsr_resolve_within_it(client):
    client.force_login(
        User.objects.create(username="admin", is_staff=True, is_superuser=True)
    )
    rtype = registration.get_resource_type_for("partner-profiles")

    response = client.get("/api/partner-profiles/")

    assert response.status_code == 200, response.content
    links = _links(response)
    schema = "http://testserver/api/collections-json-schema/partner-profiles/"
    context = "http://testserver/api/collections-json-ld/partner-profiles/"
    assert links["describedby"] == schema
    assert links["http://www.w3.org/ns/json-ld#context"] == context
    document = client.get(links[HSR_REL])
    assert document.status_code == 200, document.content
    text = document.content.decode()
    json.loads(text)
    item_schema = f"http://testserver/api/json-schema/{rtype.schema.pk}/"
    for url in (schema, context, item_schema):
        assert url in text, (url, text)
    for url in (schema, context):
        resolve(urlsplit(url).path)
        assert client.get(url).status_code == 200

    created = client.post(
        "/api/partner-profiles/",
        json.dumps({"name": "x", "rbac_context": "a"}),
        content_type="application/json",
    )

    assert created.status_code == 201, created.content
    assert created["Location"].startswith("http://testserver/api/partner-profiles/")
    assert client.get(created["Location"]).json() == {"name": "x", "rbac_context": "a"}
    listed = client.get("/api/partner-profiles/").json()
    assert [entry["href"] for entry in listed["content"]] == [created["Location"]]


def test_a_ninja_only_apis_computed_links_resolve_within_it(client):
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    client.force_login(admin)
    # Not one of the types the session commits (conftest's django_db_setup).
    registration.computed_resource_type_spec(**SUMMARIES_TYPE).register()
    rtype = registration.get_resource_type_for("summaries")

    response = client.get("/api/summaries/k/")

    assert response.status_code == 200, response.content
    assert response.json() == {"key": "k", "user": "admin"}
    links = _links(response)
    schema = f"http://testserver/api/json-schema/{rtype.schema.pk}/"
    context = f"http://testserver/api/json-ld/{rtype.context.pk}/"
    assert links == {
        "describedby": schema,
        "http://www.w3.org/ns/json-ld#context": context,
    }
    for url in (schema, context):
        resolve(urlsplit(url).path)
        assert client.get(url).status_code == 200


def test_a_ninja_only_apis_catalog_lists_its_own_routes(client, monkeypatch):
    # The process's catalog holds the types every URLconf of the session
    # registered, which this one does not route (follow-up
    # services-unrouted-types): the API's own only.
    monkeypatch.setattr(services, "services", {})
    registration.json_resource_type_spec(**PARTNER_PROFILES_TYPE).register()
    registration.register_role_services()

    response = client.get("/api/services/")

    assert response.status_code == 200, response.content
    assert response.json() == {
        "tests://PartnerProfiles": "http://testserver/api/partner-profiles/",
        collection_type_uri_for(Role.resource_type.iri): (
            "http://testserver/api/roles/"
        ),
        collection_type_uri_for(RoleAssignment.resource_type.iri): (
            "http://testserver/api/role-assignments/"
        ),
    }
    for url in response.json().values():
        assert client.get(url).status_code in (200, 401), url


def test_a_ninja_only_apis_scrudful_routes_serve_their_hsrs(client, monkeypatch):
    """The ScrudfulRouters are mounted before any path that could shadow
    their HTTP Schema Resource routes: each one's HSR is served."""
    # Follow-up services-unrouted-types: a catalog type this API does not route.
    monkeypatch.setattr(services, "services", {})
    client.force_login(
        User.objects.create(username="admin", is_staff=True, is_superuser=True)
    )
    for path in (
        "/api/services/",
        "/api/roles/",
        "/api/role-assignments/",
        "/api/user-rbac-policy/",
        "/api/partner-profiles/",
    ):
        response = client.get(path)
        assert response.status_code == 200, (path, response.content)
        document = client.get(_links(response)[HSR_REL])
        assert document.status_code == 200, (path, document.content)
        json.loads(document.content)
