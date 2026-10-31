"""Schema and JSON-LD documents are served with the media types their ``Link``
says: a list's collection schema and a JSON Schema resource as
``application/schema+json``, a list's collection context and a JSON-LD
context resource as ``application/ld+json``. A client that asks for
``application/json`` still gets the same body as ``application/json``;
errors, OPTIONS and lists keep theirs."""

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils.http import quote_etag
from rest_framework.test import APIClient

from scrud_django.models import Resource, ResourceType
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory

SCHEMA_JSON = "application/schema+json"
LD_JSON = "application/ld+json"


def superuser():
    client = APIClient()
    client.force_authenticate(UserFactory(is_superuser=True, is_staff=True))
    return client


def documents():
    """(url, media type) of each document a resource's Link names."""
    reverse("partner-profiles-list")  # importing tests.urls registers types
    partner_profiles = ResourceType.objects.get(slug="partner-profiles")
    return [
        (reverse("collections-json-schema", args=["partner-profiles"]), SCHEMA_JSON),
        (reverse("collections-json-ld", args=["partner-profiles"]), LD_JSON),
        (reverse("json-schema-detail", args=[partner_profiles.schema_id]), SCHEMA_JSON),
        (reverse("json-ld-detail", args=[partner_profiles.context_id]), LD_JSON),
    ]


def content_type(response):
    return response["Content-Type"].split(";")[0].strip()


@pytest.mark.django_db
def test_each_document_is_served_as_its_links_type():
    client = superuser()
    for url, media_type in documents():
        for accept in (
            None,
            "*/*",
            media_type,
            f"{media_type}, application/json;q=0.5",
        ):
            headers = {} if accept is None else {"HTTP_ACCEPT": accept}
            response = client.get(url, **headers)
            assert response.status_code == 200, (url, response.content)
            assert content_type(response) == media_type, (url, accept)
            assert "Accept" in response.get("Vary", ""), (url, response.get("Vary"))
        head = client.head(url)
        assert head.status_code == 200, url
        assert content_type(head) == media_type, url


@pytest.mark.django_db
def test_a_client_asking_for_json_gets_the_same_body_as_json():
    client = superuser()
    for url, _media_type in documents():
        typed = client.get(url)
        plain = client.get(url, HTTP_ACCEPT="application/json")
        assert plain.status_code == 200, (url, plain.content)
        assert content_type(plain) == "application/json", url
        assert "Accept" in plain.get("Vary", ""), (url, plain.get("Vary"))
        assert plain.content == typed.content, url
        if typed.has_header("ETag"):
            assert plain["ETag"] == typed["ETag"], url


@pytest.mark.django_db
def test_a_media_type_the_route_does_not_serve_is_406():
    client = superuser()
    for url, _media_type in documents():
        response = client.get(url, HTTP_ACCEPT="application/xml")
        assert response.status_code == 406, (url, response.content)


@pytest.mark.django_db
def test_a_put_of_a_json_schema_answers_with_the_schema_type():
    client = superuser()
    reverse("partner-profiles-list")
    schema = ResourceRegistration.register({"type": "object"}, "json-schema")
    url = reverse("json-schema-detail", args=[schema.pk])
    response = client.put(
        url,
        {"type": "object", "title": "changed"},
        format="json",
        HTTP_IF_MATCH=quote_etag(Resource.objects.get(pk=schema.pk).etag),
    )
    assert response.status_code == 200, response.content
    assert content_type(response) == SCHEMA_JSON


@pytest.mark.django_db
def test_lists_and_options_keep_application_json():
    client = superuser()
    documents()
    for name in ("json-schema-list", "json-ld-list"):
        response = client.get(reverse(name))
        assert response.status_code == 200, response.content
        assert content_type(response) == "application/json", name
    for url, _media_type in documents():
        response = client.options(url)
        assert response.status_code == 200, (url, response.content)
        assert content_type(response) == "application/json", url


@pytest.mark.django_db
@pytest.mark.parametrize("uniform", [True, False])
def test_errors_keep_their_media_type(uniform):
    error_type = "application/problem+json" if uniform else "application/json"
    client = superuser()
    schema_url = documents()[2][0]
    with override_settings(
        SCOPED_RBAC=dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=uniform)
    ):
        for url in (
            reverse("collections-json-schema", args=["no-such-type"]),
            reverse("collections-json-ld", args=["no-such-type"]),
            reverse("json-schema-detail", args=[999999]),
            reverse("json-ld-detail", args=[999999]),
        ):
            response = client.get(url)
            assert response.status_code == 404, (url, response.content)
            assert content_type(response) == error_type, (url, response["Content-Type"])
        # The anonymous user may not read a schema resource.
        anonymous = APIClient().get(schema_url)
    assert anonymous.status_code >= 400, anonymous.content
    assert content_type(anonymous) == error_type, anonymous["Content-Type"]


@pytest.mark.django_db
def test_the_format_parameter_names_each_type():
    client = superuser()
    for url, media_type in documents():
        response = client.get(url, {"format": "json"})
        assert response.status_code == 200, (url, response.content)
        assert content_type(response) == "application/json", url
        own = "schema-json" if media_type == SCHEMA_JSON else "ld-json"
        response = client.get(url, {"format": own})
        assert response.status_code == 200, (url, response.content)
        assert content_type(response) == media_type, url


@pytest.mark.django_db
def test_a_scrud_error_on_a_document_route_is_problem_details():
    # A PUT of a JSON Schema that does not compile: scrud's own 400, served
    # as problem details whichever renderer negotiation picked.
    client = superuser()
    reverse("partner-profiles-list")
    schema = ResourceRegistration.register({"type": "object"}, "json-schema")
    url = reverse("json-schema-detail", args=[schema.pk])
    for accept in (None, "application/json"):
        headers = {} if accept is None else {"HTTP_ACCEPT": accept}
        response = client.put(
            url, {"type": 5}, format="json", HTTP_IF_MATCH="*", **headers
        )
        assert response.status_code == 400, response.content
        assert content_type(response) == "application/problem+json", accept
