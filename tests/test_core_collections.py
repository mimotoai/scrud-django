"""The collection documents without DRF (`scrud_django.core.collections`):
the JSON Schema and JSON-LD context of a type's list body, as the DRF views
(``collections-json-schema/<slug>/``, ``collections-json-ld/<slug>/``) serve
them, and as the ninja routes serve them too."""

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from scrud_django.core.collections import (
    COLLECTION_CONTENT_POINTER,
    collection_context,
    collection_schema,
    embedded_schema,
)
from scrud_django.models import ResourceType
from scrud_django.registration import get_resource_type_for

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "name, document",
    [
        ("collections-json-schema", collection_schema),
        ("collections-json-ld", collection_context),
    ],
)
@pytest.mark.parametrize("slug", ["partner-profiles", "json-schema", "todos"])
def test_collection_documents_are_the_drf_views(name, document, slug):
    response = APIClient().get(
        reverse(name, args=[slug]), headers={"Accept": "application/json"}
    )

    assert response.status_code == 200, response.content
    assert response.json() == document(get_resource_type_for(slug))


def test_collection_schema_of_a_type_without_a_stored_schema():
    reverse("partner-profiles-list")
    stored = get_resource_type_for("partner-profiles")
    by_uri = ResourceType(
        type_uri="tests://ByUri", slug="by-uri", schema_uri="tests://ByUri.json"
    )
    bare = ResourceType(type_uri="tests://Bare", slug="bare")

    def content_of(resource_type):
        schema = collection_schema(resource_type)
        return schema["properties"]["content"]["items"]["properties"]["content"]

    assert content_of(by_uri) == {"$ref": "tests://ByUri.json"}
    assert content_of(bare) == {}
    assert content_of(stored) == embedded_schema(
        stored.schema.content, COLLECTION_CONTENT_POINTER
    )
    assert collection_schema(by_uri)["$id"] == (
        "https://api.openteams.com/json-schema/ResourceCollection"
        "?contents_type=tests://ByUri"
    )
    assert collection_context(bare)["content"]["openteams:envelopeContents"] == (
        "tests://Bare"
    )
