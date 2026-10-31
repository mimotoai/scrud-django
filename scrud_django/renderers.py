"""The renderers of the schema and JSON-LD documents
(``scrud_django.views.DocumentMediaTypeMixin``)."""

from rest_framework.renderers import JSONRenderer

__all__ = ["DOCUMENT_RENDERERS", "JSONLDRenderer", "SchemaJSONRenderer"]


class SchemaJSONRenderer(JSONRenderer):
    """JSON served as ``application/schema+json`` (a JSON Schema); its own
    ``?format=schema-json``, so that ``?format=json`` gives
    ``application/json``."""

    media_type = "application/schema+json"
    format = "schema-json"


class JSONLDRenderer(JSONRenderer):
    """JSON served as ``application/ld+json`` (a JSON-LD document); its own
    ``?format=ld-json``, so that ``?format=json`` gives
    ``application/json``."""

    media_type = "application/ld+json"
    format = "ld-json"


# The documents' renderers: an error answered under one of them is problem
# details (``scrud_django.exceptions.scrud_exception_handler``).
DOCUMENT_RENDERERS = (SchemaJSONRenderer, JSONLDRenderer)
