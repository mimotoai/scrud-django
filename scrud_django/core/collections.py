"""The documents a list's Links name, without DRF: the JSON Schema of a
resource type's list body (``collections-json-schema/<slug>/``, its
``describedby``) and its JSON-LD context (``collections-json-ld/<slug>/``),
as the DRF views (``scrud_django.views.ResourceCollectionSchemaView`` and
``ResourceCollectionContextView``) and the ninja routes serve them."""

from scrud_django import collection_type_uri_for

# Where a list's collection schema embeds the schema of its resources' content.
COLLECTION_CONTENT_POINTER = "#/properties/content/items/properties/content"

# The keywords whose values are data, not schemas: a ``$ref`` in them is not
# a reference.
_DATA_KEYWORDS = frozenset({"enum", "const", "default", "examples"})


def embedded_schema(schema, pointer):
    """A copy of JSON Schema `schema` to embed at `pointer` (a ``#`` JSON
    pointer) of another: without its top-level ``$id``, which would make its
    references resolve against that URI rather than the document it is in,
    and with every reference into itself (a ``$ref`` starting with ``#``)
    rebased under `pointer`. A reference to another document is kept."""

    def rebase(node, data=False):
        if isinstance(node, list):
            return [rebase(item, data) for item in node]
        if not isinstance(node, dict):
            return node
        rebased = {}
        for key, value in node.items():
            if (
                not data
                and key == "$ref"
                and isinstance(value, str)
                and value.startswith("#")
            ):
                rebased[key] = pointer + value[1:]
            else:
                rebased[key] = rebase(value, data or key in _DATA_KEYWORDS)
        return rebased

    if not isinstance(schema, dict):
        return schema
    schema = {key: value for key, value in schema.items() if key != "$id"}
    return rebase(schema)


def collection_schema(resource_type) -> dict:
    """The JSON Schema of `resource_type`'s list body: the page envelope of
    the list (`scrud_django.core.lists.list_body`), whose ``content`` is the
    page's resource envelopes, each with the type's schema embedded as its
    ``content`` (`embedded_schema`), a ``$ref`` to the type's
    ``schema_uri``, or any value for a type without a schema."""
    if resource_type.schema:
        content_defn = embedded_schema(
            resource_type.schema.content, COLLECTION_CONTENT_POINTER
        )
    elif resource_type.schema_uri:
        content_defn = {"$ref": resource_type.schema_uri}
    else:
        content_defn = {}

    url_or_null = {"type": ["string", "null"], "format": "uri"}
    schema = {
        "$id": "https://api.openteams.com/json-schema/ResourceCollection"
        f"?contents_type={resource_type.type_uri}",
        "$schema": "http://json-schema.org/draft-04/schema",
        "description": f"A listing of resources of type {resource_type.type_uri}.",
        "type": "object",
        "properties": {
            "count": {
                "type": "integer",
                "description": "The total number of items in the collection.",
            },
            "page_count": {
                "type": "integer",
                "description": "The total number of pages in the collection.",
            },
            "previous": dict(
                url_or_null, description="URL of the previous page, or null."
            ),
            "next": dict(url_or_null, description="URL of the next page, or null."),
            "content": {
                "type": "array",
                "description": f"Listing of {resource_type.type_uri} "
                "resources in Envelopes.",
                "items": {
                    "type": "object",
                    "properties": {
                        "href": {
                            "type": "string",
                            "format": "uri",
                            "description": "URL of the nested resource.",
                        },
                        "etag": {
                            "type": "string",
                            "description": "HTTP ETag header of the nested resource.",
                        },
                        "last_modified": {
                            "type": "string",
                            "description": "HTTP Last-Modified header of the "
                            "nested resource.",
                        },
                        "link": {
                            "type": "string",
                            "description": "HTTP Link header of the nested "
                            "resource: its schema and JSON-LD context.",
                        },
                        "content": content_defn,
                    },
                },
            },
        },
    }

    return schema


def collection_context(resource_type) -> dict:
    """The JSON-LD context of `resource_type`'s list body. Its terms are kept
    as they were (the ``opententeams:`` prefix of first/previous/next/last
    included): the ``api.openteams.com`` IRIs are part of the API's
    semantics."""
    return {
        "openteams": "https://api.openteams.com/json-ld/",
        "@id": collection_type_uri_for(resource_type.type_uri),
        "http://www.w3.org/2000/01/rdf-schema#subClassOf": {
            "@id": "https://api.openteams.com/json-ld/ResourceCollection"
        },
        "count": {"@id": "openteams:count"},
        "page_count": {"@id": "openteams:page_count"},
        "first": {"@id": "opententeams:first"},
        "previous": {"@id": "opententeams:previous"},
        "next": {"@id": "opententeams:next"},
        "last": {"@id": "opententeams:last"},
        "content": {
            "@id": "openteams:Envelope",
            "@container": "@list",
            "openteams:envelopeContents": resource_type.type_uri,
        },
    }
