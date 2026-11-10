"""The studio's sample instance: a deterministic JSON value built from a
resource type's JSON Schema, so that a type with no instances can be
previewed."""

import copy
import math

from scrud_django.mixins.view_mixins import max_body_depth

_PLACEHOLDER = "Lorem ipsum"
_FORMATS = {
    "date": "2026-01-01",
    "date-time": "2026-01-01T00:00:00Z",
    "uri": "https://example.com/",
    "email": "name@example.com",
}


def sample_for(schema, root=None, *, max_depth=None):
    """The sample value for `schema`, whose local ``$ref`` s resolve in `root`
    (default `schema`). ``default``, then the first of ``examples``, then the
    first of ``enum`` (each a copy); otherwise a value by type. A ``$ref``
    cycle, a node deeper than `max_depth` (default `max_body_depth()`) and
    any construct not understood give ``None``. It never raises."""
    if root is None:
        root = schema
    if max_depth is None:
        max_depth = max_body_depth()
    return _sample(schema, root, max_depth, 0, frozenset())


def _sample(schema, root, max_depth, depth, active):
    if depth > max_depth:
        return None
    schema, active = _resolve(root, schema, active)
    if schema is None:
        return None
    # Copies: the caller may change the sample (`sample_of` sets its
    # ``rbac_context``), never the schema it came from.
    if "default" in schema:
        return copy.deepcopy(schema["default"])
    for key in ("examples", "enum"):
        if isinstance(schema.get(key), list) and schema[key]:
            return copy.deepcopy(schema[key][0])

    def child(sub, at=depth + 1):
        return _sample(sub, root, max_depth, at, active)

    declared = schema.get("type")
    if isinstance(declared, list):
        declared = next((t for t in declared if t != "null"), None)
    if declared == "string":
        return _FORMATS.get(schema.get("format"), _PLACEHOLDER)
    if declared in ("integer", "number"):
        minimum = schema.get("minimum")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
            # The renderer types a fractional number as a number only.
            return math.ceil(minimum) if declared == "integer" else minimum
        return 0
    if declared == "boolean":
        return False
    if declared == "array":
        items = schema.get("items")
        return [child(items)] if isinstance(items, dict) else []
    if declared == "object":
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return {}
        return {name: child(sub) for name, sub in properties.items()}
    if declared is None and "type" not in schema:
        # As the renderer does, an untyped schema has its value's type.
        if isinstance(schema.get("properties"), dict):
            return {name: child(sub) for name, sub in schema["properties"].items()}
        if isinstance(schema.get("items"), dict):
            return [child(schema["items"])]
        branches = schema.get("allOf")
        if isinstance(branches, list):
            values = [child(branch, depth) for branch in branches]
            objects = [v for v in values if isinstance(v, dict)]
            if objects:
                return {k: v for o in objects for k, v in o.items()}
            return next((v for v in values if v is not None), None)
        for key in ("oneOf", "anyOf"):
            branches = schema.get(key)
            if isinstance(branches, list):
                for branch in branches:
                    value = child(branch, depth)
                    if value is not None:
                        return value
    return None


def _resolve(root, schema, active):
    """``(schema, active)`` with `schema`'s local ``$ref`` chain followed, or
    ``(None, active)`` for a reference that is not local, does not resolve or
    is already being followed (a cycle)."""
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#") or ref in active:
            return None, active
        active = active | {ref}
        schema = _pointer_get(root, ref[1:])
    if not isinstance(schema, dict):
        return None, active
    return schema, active


def _pointer_get(document, pointer):
    """The part of `document` at JSON Pointer `pointer`, or ``None``."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        return None
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(document, dict) and part in document:
            document = document[part]
        elif (
            isinstance(document, list) and part.isdigit() and int(part) < len(document)
        ):
            document = document[int(part)]
        else:
            return None
    return document
