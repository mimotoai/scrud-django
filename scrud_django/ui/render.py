"""The render tree: a resource's content rendered as HTML, node by node, through
the components its stylesheets resolve, in the mode the viewing user may act in.

A node is the resource, or a value in it reached by walking its JSON Schema
(``properties`` and ``items``, through local ``$ref``). Content the schema does
not describe is not rendered: an unknown key is ignored, a missing property
renders nothing, and a value whose JSON type is not the schema's renders as
JSON (``scrud:json``, read-only). So does a node nested deeper than
`max_body_depth`, or one whose ``$ref`` cannot be resolved or loops.

Each node is given plain JSON and its children's HTML (the node context
contract of the built-ins); a stored component that fails while rendering is
replaced by the built-in for its node and mode, after an HTML comment naming it.
"""

import copy
import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from django.core.serializers.json import DjangoJSONEncoder
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe

from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.mixins.view_mixins import max_body_depth
from scrud_django.ui import ld
from scrud_django.ui.authz import ALLOWED, POSSIBLY, decisions_for
from scrud_django.ui.cascade import (
    BUILTIN_PREFIX,
    Component,
    builtin_component,
    effective_rules,
    request_memo,
    resolve_component,
)
from scrud_django.ui.selectors import TypeTerm

logger = logging.getLogger(__name__)

# What a ``<script>`` element must not contain raw (as Django's `json_script`).
_JSON_SCRIPT_ESCAPES = {
    ord(">"): "\\u003E",
    ord("<"): "\\u003C",
    ord("&"): "\\u0026",
}

# Keywords that make a schema's node more than its type says: without a
# ``type`` of its own it has no schema terms (and renders as JSON unless an
# IRI rule styles it).
_COMPOSITION = ("oneOf", "anyOf", "allOf", "not")


def render_resource(
    content: dict,
    *,
    resource_type,
    href: str,
    etag: str | None,
    request,
    scope_chain: tuple[str, ...] = (),
    pointer: str = "",
    errors: dict[str, list[str]] | None = None,
    mode: str | None = None,
    decisions: dict[str, str] | None = None,
    contexts: list[str] | None = None,
) -> SafeString:
    """`content` (a resource of `resource_type` at `href`) rendered for the
    user of `request`, in a resource frame: the node at `pointer` (the
    resource itself when ``""``), resolved in `scope_chain`, the scope chain
    of the resource. `errors` maps JSON Pointers to messages (``""``: the
    resource's, shown at the top of the frame). `mode` is ``"view"`` or
    ``"edit"``; ``None`` is ``"edit"`` when the user may (or possibly may)
    PUT the resource.

    `decisions` (as `decisions_for` gives them) and `contexts` (whose
    stylesheets apply) are those of `content` unless given: for content that
    is not the stored resource (a refused PUT's body), pass the stored
    resource's, so that nothing the request sent decides them."""
    with request_memo():
        return _render_resource(
            content,
            resource_type=resource_type,
            href=href,
            etag=etag,
            request=request,
            scope_chain=tuple(scope_chain),
            pointer=pointer,
            errors=errors,
            mode=mode,
            rules_cache={},
            decisions=decisions,
            contexts=contexts,
        )


def render_collection(
    envelope: dict, *, resource_type, href: str, request, scope_chain=()
) -> SafeString:
    """A page of a list (`envelope`, as the list route returns it: ``count``,
    ``next``, ``previous`` and the ``content`` envelopes) rendered with
    ``scrud:collection``, each resource in its own frame and its own
    contexts' stylesheets."""
    scope_chain = tuple(scope_chain)
    envelope = envelope if isinstance(envelope, Mapping) else {}
    items = envelope.get("content")
    children = []
    with request_memo():
        rules_cache = {}
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, Mapping) or not isinstance(
                item.get("content"), Mapping
            ):
                continue
            children.append(
                _render_resource(
                    item["content"],
                    resource_type=resource_type,
                    href=str(item.get("href", "")),
                    etag=item.get("etag"),
                    request=request,
                    scope_chain=scope_chain,
                    pointer="",
                    errors=None,
                    mode=None,
                    rules_cache=rules_cache,
                )
            )
    schema = _document(resource_type.schema)
    title = schema.get("title")
    body = _render_builtin(
        "scrud:collection",
        {
            "children": children,
            "count": envelope.get("count", len(children)),
            "next": envelope.get("next"),
            "previous": envelope.get("previous"),
            "label": title if isinstance(title, str) else "",
        },
    )
    return format_html(
        '<div class="scrud-collection-frame" data-scrud-href="{}"'
        ' data-scrud-scope="{}">{}</div>',
        href,
        " ".join(scope_chain),
        body,
    )


def _render_resource(
    content,
    *,
    resource_type,
    href,
    etag,
    request,
    scope_chain,
    pointer,
    errors,
    mode,
    rules_cache,
    decisions=None,
    contexts=None,
):
    type_uri = resource_type.type_uri
    if decisions is None:
        decisions = decisions_for(request, type_uri, content)
    if mode is None:
        mode = "edit" if decisions["PUT"] in (ALLOWED, POSSIBLY) else "view"
    contexts = rbac_contexts_of(content) if contexts is None else list(contexts)
    ld_context = _ld_context(resource_type.context)
    walk = _Walk(
        root_schema=_document(resource_type.schema),
        contexts=contexts,
        mode=mode,
        resource={
            "href": href,
            "etag": etag,
            "type": type_uri,
            "contexts": contexts,
            "decisions": decisions,
        },
        errors=_clean_errors(errors),
        max_depth=max_body_depth(),
        id_prefix="scrud-" + hashlib.sha256(href.encode()).hexdigest()[:8],
        rules_cache=rules_cache,
    )
    root_classes = [
        _keyword_free(ld.expand(t, ld_context)) if isinstance(t, str) else None
        for t in (
            content.get("@type") if isinstance(content, Mapping) else None,
            ld_context.get("@type"),
        )
    ] + [type_uri]
    root = walk.node(
        value=content,
        schema=walk.root_schema,
        pointer="",
        scope_chain=scope_chain,
        ld_ctx=ld_context,
        property_iri=None,
        term_class=None,
        classes=tuple(dict.fromkeys(c for c in root_classes if c)),
        depth=0,
        linked=(),
        name=None,
    )
    node = walk.find(root, pointer)
    body = walk.render(node)
    return _frame(
        content=content,
        body=body,
        notices=walk.top_errors(),
        href=href,
        etag=etag,
        scope_chain=scope_chain,
        pointer=node.pointer,
        mode=mode,
        deletable=node.pointer == "" and decisions["DELETE"] == ALLOWED,
    )


@dataclass
class _Node:
    value: object
    schema: dict
    pointer: str
    scope_chain: tuple[str, ...]
    ld_ctx: dict
    term_class: str | None  # the class its term definition names
    depth: int
    linked: tuple[str, ...]  # stylesheets its ancestors' components link
    label: str
    terms: list[TypeTerm] = field(default_factory=list)
    component: Component | None = None
    kind: str | None = None  # "object" or "array" when it has children to walk


@dataclass
class _Walk:
    root_schema: dict
    contexts: list[str]
    mode: str
    resource: dict
    errors: dict[str, list[str]]
    max_depth: int
    id_prefix: str
    rules_cache: dict
    rendered: set = field(default_factory=set)

    def rules(self, linked):
        key = (tuple(self.contexts), linked)
        if key not in self.rules_cache:
            self.rules_cache[key] = effective_rules(self.contexts, linked)
        return self.rules_cache[key]

    def node(
        self,
        *,
        value,
        schema,
        pointer,
        scope_chain,
        ld_ctx,
        property_iri,
        term_class,
        depth,
        linked,
        name,
        classes=None,
        reachable=True,
    ) -> _Node:
        """The node for `value` described by `schema`, with its component
        resolved."""
        schema, resolved = _resolve_ref(self.root_schema, schema)
        title = schema.get("title")
        node = _Node(
            value=value,
            schema=schema,
            pointer=pointer,
            scope_chain=scope_chain,
            ld_ctx=ld_ctx,
            term_class=term_class,
            depth=depth,
            linked=linked,
            label=title if isinstance(title, str) else (name or ""),
        )
        if classes is None:
            classes = (_class_of(value, term_class, ld_ctx),)
        iris = [c for c in (*classes, property_iri) if c]
        iri_terms = [TypeTerm(iri=iri) for iri in dict.fromkeys(iris)]
        actual = _json_type(value)
        if not reachable or not resolved or depth > self.max_depth:
            node.terms = [TypeTerm(schema_type=actual)] if actual else []
            node.component = builtin_component("scrud:json")
            return node
        schema_type, matches = _schema_type(schema, value, actual)
        if not matches:
            node.terms = [TypeTerm(schema_type=actual)] if actual else []
            node.component = builtin_component("scrud:json")
            return node
        node.kind = _walkable(schema, schema_type)
        node.terms = iri_terms + _schema_terms(schema, schema_type, node.kind)
        node.component = resolve_component(
            self.rules(linked), scope_chain, node.terms, self.mode
        )
        return node

    def children_of(self, node: _Node):
        """``(key, child node)`` for each child of `node` that is rendered: a
        property the schema and the value both have, or an array element."""
        if node.kind == "object":
            for prop, sub in node.schema["properties"].items():
                if prop in node.value:
                    yield prop, self.child(node, prop, node.value[prop], sub)
        elif node.kind == "array":
            for index, item in enumerate(node.value):
                yield (
                    index,
                    self.child(node, index, item, _items_schema(node.schema, index)),
                )

    def child(self, node, key, value, schema, reachable=True) -> _Node:
        """The child of `node` at `key` (a property name, or an array index),
        under the scopes and stylesheets of `node`'s component."""
        scopes = node.component.scopes
        if isinstance(key, int):
            # the term's class is its elements'
            property_iri, class_iri, ld_ctx = None, node.term_class, node.ld_ctx
            scope, name = scopes.get("*"), None
        else:
            property_iri, class_iri, ld_ctx = _term(node.ld_ctx, key)
            scope, name = scopes.get(key), key
        return self.node(
            value=value,
            schema=schema if isinstance(schema, dict) else {},
            pointer=f"{node.pointer}/{_escape_segment(str(key))}",
            scope_chain=node.scope_chain + ((scope,) if scope else ()),
            ld_ctx=ld_ctx,
            property_iri=property_iri,
            term_class=class_iri,
            depth=node.depth + 1,
            linked=node.linked + node.component.stylesheets,
            name=name,
            reachable=reachable,
        )

    def find(self, root: _Node, pointer: str) -> _Node:
        """The node at `pointer` below `root`, reached as the full render
        reaches it (scopes and linked stylesheets included). A node the
        schema does not describe there renders as JSON."""
        node = root
        if not pointer:
            return node
        tokens = pointer[1:].split("/") if pointer.startswith("/") else [pointer]
        for token in tokens:
            key = token.replace("~1", "/").replace("~0", "~")
            if node.kind == "object" and key in node.schema["properties"]:
                node = self.child(
                    node, key, node.value.get(key), node.schema["properties"][key]
                )
            elif node.kind == "array" and _is_index(token, len(node.value)):
                index = int(token)
                node = self.child(
                    node, index, node.value[index], _items_schema(node.schema, index)
                )
            else:
                value = node.value.get(key) if isinstance(node.value, Mapping) else None
                node = self.child(node, key, value, {}, reachable=False)
        return node

    def render(self, node: _Node) -> SafeString:
        self.rendered.add(node.pointer)
        child_items = [
            [key, self.render(child)] for key, child in self.children_of(node)
        ]
        if node.kind == "object":
            children = dict(child_items)
        elif node.kind == "array":
            children = [html for _, html in child_items]
            child_items = []
        else:
            children, child_items = {}, []
        context = {
            "value": node.value,
            "schema": node.schema,
            "pointer": node.pointer,
            "mode": self.mode,
            "label": node.label,
            "children": children,
            "child_items": child_items,
            "resource": self.resource,
            "scope": list(node.scope_chain),
            "errors": self.errors.get(node.pointer, []) if node.pointer else [],
            "value_json": "",
            "input_id": self.id_prefix + _id_part(node.pointer),
            "append_fields": self.append_fields(node),
        }
        component = node.component
        builtin = component.name.startswith(BUILTIN_PREFIX)
        if not builtin or component.name == "scrud:json":
            # only these read it (a stored one may fall back to scrud:json)
            context["value_json"] = json.dumps(
                node.value, indent=2, sort_keys=True, ensure_ascii=False, default=str
            )
        if builtin:
            return component.compiled().render(context)
        try:
            return component.compiled().render(_isolated(context))
        except Exception:
            logger.warning(
                "scrud-ui: component %r failed to render %s",
                component.name,
                node.pointer or "/",
                exc_info=True,
            )
        fallback = resolve_component(
            tuple(
                rule
                for rule in self.rules(node.linked)
                if rule.component.startswith(BUILTIN_PREFIX)
            ),
            node.scope_chain,
            node.terms,
            self.mode,
        )
        return format_html(
            '<!-- scrud-ui: component "{}" failed -->{}',
            component.name,
            fallback.compiled().render(context),
        )

    def append_fields(self, node):
        """``[{"name", "type", "pointer_name"}]`` of the properties of an array
        node's items: ``type`` is the property's schema type (the first
        non-null one of a list, through its local ``$ref``), and
        ``pointer_name`` the name escaped as a JSON Pointer reference token."""
        if node.kind != "array":
            return []
        items, _ = _resolve_ref(self.root_schema, node.schema.get("items"))
        properties = items.get("properties")
        if not isinstance(properties, dict):
            return []
        return [
            {
                "name": name,
                "type": _append_type(_resolve_ref(self.root_schema, sub)[0]),
                "pointer_name": _escape_segment(name),
            }
            for name, sub in properties.items()
        ]

    def top_errors(self) -> list[str]:
        """The messages for the resource, then those for nodes not rendered
        (prefixed with their pointer), so that none is lost."""
        messages = list(self.errors.get("", []))
        for pointer, found in self.errors.items():
            if pointer and pointer not in self.rendered:
                messages.extend(f"{pointer}: {m}" for m in found)
        return messages


def _frame(
    *, content, body, notices, href, etag, scope_chain, pointer, mode, deletable
):
    attributes = [
        ("class", "scrud-resource"),
        ("data-scrud-href", href),
        *([("data-scrud-etag", etag)] if etag is not None else []),
        ("data-scrud-scope", " ".join(scope_chain)),
        ("data-scrud-pointer", pointer),
    ]
    if mode == "edit":
        attributes += [
            ("hx-put", href),
            ("hx-ext", "scrud-json"),
            ("hx-trigger", "change, submit"),
            ("hx-target", "this"),
            ("hx-swap", "outerHTML"),
        ]
    # Only a root frame is a form: a node frame (a fragment of the resource)
    # may be swapped into its root's form, and forms do not nest.
    tag = "form" if mode == "edit" and pointer == "" else "div"
    if tag == "form":
        # Server-side JSON Schema validation is the authority: the browser's
        # constraint validation (a stored non-absolute URI in an
        # `<input type="url">`) must not block the submit.
        attributes.append(("novalidate", ""))
    parts = [
        mark_safe(f"<{tag}"),
        *(format_html(' {}="{}"', name, value) for name, value in attributes),
        mark_safe(">"),
        mark_safe(
            '<script type="application/json" class="scrud-content">'
            + json.dumps(content, cls=DjangoJSONEncoder).translate(_JSON_SCRIPT_ESCAPES)
            + "</script>"
        ),
        *(
            _render_builtin("scrud:notice", {"message": m, "level": "error"})
            for m in notices
        ),
        body,
    ]
    if deletable:
        parts.append(
            format_html(
                '<button type="button" hx-delete="{}" hx-ext="scrud-json"'
                ' hx-confirm="Delete?" hx-target="closest .scrud-resource"'
                ' hx-swap="outerHTML">Delete</button>',
                href,
            )
        )
    parts.append(mark_safe(f"</{tag}>"))
    return mark_safe("".join(parts))


def _render_builtin(name, context):
    return builtin_component(name).compiled().render(context)


def _isolated(context):
    """`context` with copies of its JSON values, so that a stored template
    cannot change the content, the schema or another node's context."""
    return {
        key: value if key in ("children", "child_items") else copy.deepcopy(value)
        for key, value in context.items()
    }


def _document(resource):
    """The content of the type's document `resource` (its schema or JSON-LD
    context) when it is a JSON object, else ``{}``."""
    content = getattr(resource, "content", None)
    return content if isinstance(content, dict) else {}


def _ld_context(resource):
    """The term definitions of a JSON-LD context document: its ``@context``
    when that is an object, else the document itself."""
    document = _document(resource)
    inner = document.get("@context")
    return inner if isinstance(inner, dict) else document


def _keyword_free(iri):
    """`iri`, or ``None`` for a JSON-LD keyword (``@id``, ``@vocab``, ...),
    which is no class or property."""
    return iri if isinstance(iri, str) and iri and not iri.startswith("@") else None


def _term(ld_ctx, key):
    property_iri, class_iri, nested = ld.term_definition(ld_ctx, key)
    return _keyword_free(property_iri), _keyword_free(class_iri), nested


def _class_of(value, term_class, ld_ctx):
    """The class IRI of a node: none for an array (its term's class is its
    elements'), an object's own ``@type``, else its term definition's."""
    if isinstance(value, list):
        return None
    if isinstance(value, Mapping) and isinstance(value.get("@type"), str):
        return _keyword_free(ld.expand(value["@type"], ld_ctx))
    return term_class


def _resolve_ref(root, schema):
    """``(schema, resolved)``: `schema` with its local ``$ref`` chain
    followed. Not resolved for a reference that is not local, does not
    resolve, or loops without descending."""
    seen = set()
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#") or ref in seen:
            return {}, False
        seen.add(ref)
        schema = _pointer_get(root, ref[1:])
    if not isinstance(schema, dict):
        return {}, False
    return schema, True


def _pointer_get(document, pointer):
    """The value at JSON Pointer `pointer` in `document`, or ``None``."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        return None
    for raw in pointer[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(document, dict) and key in document:
            document = document[key]
        elif isinstance(document, list) and _is_index(raw, len(document)):
            document = document[int(raw)]
        else:
            return None
    return document


def _json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return None


def _type_matches(declared, actual, value):
    if declared == actual:
        return True
    if declared == "number":
        return actual == "integer"
    if declared == "integer" and actual == "number":
        return value.is_integer()
    return False


def _schema_type(schema, value, actual):
    """``(type, matches)``: the schema's type that `value` has, or the value's
    own when the schema has none; ``matches`` is False when the schema
    declares types and the value has none of them."""
    declared = schema.get("type")
    declared = declared if isinstance(declared, list) else [declared]
    declared = [t for t in declared if isinstance(t, str)]
    if not declared:
        if any(k in schema for k in _COMPOSITION):
            return None, True
        return actual, True
    for schema_type in declared:
        if _type_matches(schema_type, actual, value):
            return schema_type, True
    return None, False


def _walkable(schema, schema_type):
    if schema_type == "object" and isinstance(schema.get("properties"), dict):
        return "object"
    if schema_type == "array" and isinstance(schema.get("items"), (dict, list)):
        return "array"
    return None


def _schema_terms(schema, schema_type, kind):
    """A node's schema terms: format, enum, then the bare type (an integer is
    a number too). An object or array with nothing to walk (no
    ``properties`` or ``items``) has none, so it renders as JSON unless an
    IRI rule styles it."""
    if schema_type is None or (schema_type in ("object", "array") and kind is None):
        return []
    terms = []
    if isinstance(schema.get("format"), str):
        terms.append(TypeTerm(schema_type=schema_type, format=schema["format"]))
    if isinstance(schema.get("enum"), list):
        terms.append(TypeTerm(schema_type=schema_type, enum=True))
    terms.append(TypeTerm(schema_type=schema_type))
    if schema_type == "integer":
        terms.append(TypeTerm(schema_type="number"))
    return terms


def _append_type(schema):
    """The schema type an append-row input is for: ``type``, or the first
    non-null string of a ``type`` list; else ``None``."""
    declared = schema.get("type")
    for schema_type in declared if isinstance(declared, list) else [declared]:
        if isinstance(schema_type, str) and schema_type != "null":
            return schema_type
    return None


def _items_schema(schema, index):
    items = schema.get("items")
    if isinstance(items, list):
        return items[index] if index < len(items) else {}
    return items


def _is_index(token, length):
    """Whether `token` is an array index (RFC 6901: no leading zero) below
    `length`."""
    return (
        token.isascii()
        and token.isdigit()
        and (token == "0" or not token.startswith("0"))
        and int(token) < length
    )


def _escape_segment(segment):
    """`segment` escaped as a JSON Pointer reference token (RFC 6901)."""
    return segment.replace("~", "~0").replace("/", "~1")


def _id_part(pointer):
    """`pointer` as part of an HTML id, one to one: ASCII letters, digits,
    ``_`` and ``~`` kept, ``/`` as ``-``, anything else as ``.<hex>.``."""
    out = []
    for char in pointer:
        if char == "/":
            out.append("-")
        elif (char.isascii() and char.isalnum()) or char in "_~":
            out.append(char)
        else:
            out.append(f".{ord(char):x}.")
    return "".join(out)


def _clean_errors(errors):
    """`errors` as ``{pointer: [message, ...]}`` of strings."""
    cleaned = {}
    for pointer, messages in (errors or {}).items():
        if not isinstance(messages, (list, tuple)):
            messages = [messages]
        cleaned[str(pointer)] = [str(m) for m in messages]
    return cleaned
