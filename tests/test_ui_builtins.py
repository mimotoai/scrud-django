"""The built-in generic components render each JSON Schema type."""

import json

import pytest
from django.utils.safestring import mark_safe

from scrud_django.ui.cascade import BUILTIN_NAMES, builtin_component
from scrud_django.ui.engines import cached_template


def render(short, **context):
    component = builtin_component("scrud:" + short)
    context.setdefault("value", None)
    context.setdefault("schema", {})
    context.setdefault("pointer", "")
    context.setdefault("label", "")
    context.setdefault("children", {})
    children = context["children"]
    context.setdefault(
        "child_items", list(children.items()) if isinstance(children, dict) else []
    )
    context.setdefault("input_id", "scrud-f-" + context["pointer"].replace("/", "~1"))
    schema_items = (
        context["schema"].get("items") if isinstance(context["schema"], dict) else None
    )
    props = schema_items.get("properties", {}) if isinstance(schema_items, dict) else {}
    context.setdefault(
        "append_fields", [{"name": k, "type": v.get("type")} for k, v in props.items()]
    )
    context.setdefault("errors", [])
    context.setdefault("value_json", json.dumps(context["value"], indent=2))
    template = cached_template(component.cache_key, "django", component.template)
    return str(template.render(context))


def test_string_view_and_edit():
    view = render("string", value="Buy milk", mode="view")
    assert "Buy milk" in view
    edit = render(
        "string-edit",
        value="Buy milk",
        pointer="/items/0/text",
        label="Text",
        mode="edit",
    )
    assert 'name="/items/0/text"' in edit
    assert 'value="Buy milk"' in edit
    assert "<label" in edit
    assert "<form" not in edit


def test_boolean_edit_checked():
    checked = render("boolean-edit", value=True, pointer="/done", label="Done")
    assert 'type="checkbox"' in checked and "checked" in checked
    unchecked = render("boolean-edit", value=False, pointer="/done", label="Done")
    assert 'type="checkbox"' in unchecked and "checked" not in unchecked


def test_boolean_view():
    assert "Yes" in render("boolean", value=True)
    assert "No" in render("boolean", value=False)


def test_number_edit():
    out = render("number-edit", value=3, pointer="/n", schema={"type": "integer"})
    assert 'type="number"' in out and 'step="1"' in out and 'value="3"' in out
    out = render("number-edit", value=1.5, pointer="/n", schema={"type": "number"})
    assert 'step="any"' in out
    assert "7" in render("number", value=7)


def test_date_edit():
    out = render("date-edit", value="2026-01-02", pointer="/d")
    assert 'type="date"' in out and 'value="2026-01-02"' in out


def test_array_edit_has_append_row():
    out = render(
        "array-edit",
        pointer="/items",
        value=[],
        children=[mark_safe("<i>child</i>")],
        schema={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "done": {"type": "boolean"}},
            },
        },
    )
    assert "<i>child</i>" in out
    assert 'name="/items/-/text"' in out
    assert 'name="/items/-/done"' in out
    assert 'type="checkbox"' in out
    assert "<form" not in out


def test_array_edit_primitive_items():
    out = render(
        "array-edit",
        pointer="/tags",
        schema={"type": "array", "items": {"type": "string"}},
    )
    assert 'name="/tags/-"' in out


def test_array_and_object_view_lay_out_children():
    arr = render("array", children=[mark_safe("<b>one</b>"), mark_safe("<b>two</b>")])
    assert "<ul" in arr and "<b>one</b>" in arr and "<b>two</b>" in arr
    obj = render("object", children={"title": mark_safe("<b>x</b>")})
    assert "title" in obj and "<b>x</b>" in obj
    obj_edit = render("object-edit", children={"title": mark_safe("<b>x</b>")})
    assert "title" in obj_edit and "<b>x</b>" in obj_edit and "<form" not in obj_edit


def test_enum_edit_options():
    out = render(
        "enum-edit",
        value="b",
        pointer="/c",
        schema={"enum": ["a", "b", "c"]},
    )
    assert out.count("<option") == 3
    assert '<option value="b" selected>' in out
    assert 'name="/c"' in out


def test_null_and_uri():
    assert "scrud-null" in render("null")
    out = render("uri", value="https://example.com/x")
    assert 'href="https://example.com/x"' in out


@pytest.mark.parametrize("mode", ["view", "edit"])
def test_json_is_read_only_in_both_modes(mode):
    out = render("json", value={"a": 1}, value_json='{\n  "a": 1\n}', mode=mode)
    assert "<pre" in out and "&quot;a&quot;" in out
    assert "<input" not in out and "<textarea" not in out


@pytest.mark.parametrize(
    "short", ["string", "string-edit", "number-edit", "boolean-edit", "enum-edit"]
)
def test_errors_rendered(short):
    out = render(
        short,
        value="x",
        pointer="/a",
        schema={"enum": ["x"]},
        errors=["Too long"],
    )
    assert 'role="alert"' in out
    assert out.index('role="alert"') < out.index("Too long")
    if short.endswith("-edit"):
        assert 'aria-invalid="true"' in out


def test_no_errors_no_alert():
    out = render("string-edit", value="x", pointer="/a")
    assert 'role="alert"' not in out and "aria-invalid" not in out


@pytest.mark.parametrize(
    "short",
    [
        "string",
        "string-edit",
        "number",
        "uri",
        "enum-edit",
        "date-edit",
        "json",
        "null",
    ],
)
def test_builtins_escape(short):
    evil = "<script>x</script>"
    out = render(
        short,
        value=evil,
        value_json=evil,
        pointer="/a",
        label=evil,
        schema={"enum": [evil]},
        errors=[evil],
        mode="edit" if short.endswith("edit") else "view",
    )
    assert "<script>x" not in out


def test_escape_in_children_keys_and_collection():
    out = render("object", children={"<script>x</script>": mark_safe("ok")})
    assert "<script>x" not in out
    out = render("collection", children=[], count=0, label="<script>x</script>")
    assert "<script>x" not in out


def test_page_context():
    out = render(
        "page",
        title="Things <b>",
        csrf_token="tok123",
        static={"htmx": "/s/htmx.js", "scrud_ui": "/s/scrud-ui.js"},
        body=mark_safe("<section>BODY</section>"),
    )
    assert out.lstrip().lower().startswith("<!doctype html>")
    assert '<meta charset="utf-8">' in out
    assert 'name="viewport"' in out
    assert '<meta name="csrf-token" content="tok123">' in out
    assert '<script src="/s/htmx.js" defer></script>' in out
    assert '<script src="/s/scrud-ui.js" defer></script>' in out
    assert "<main>" in out and "<section>BODY</section>" in out
    assert "<title>Things &lt;b&gt;</title>" in out


def test_collection_context():
    frames = [mark_safe("<article>A</article>"), mark_safe("<article>B</article>")]
    out = render(
        "collection",
        children=frames,
        count=2,
        label="Todos",
        next="/todos?page=2",
        previous=None,
    )
    assert "<article>A</article>" in out and "<article>B</article>" in out
    assert "<li" in out and "Todos" in out and "2" in out
    assert 'href="/todos?page=2"' in out
    assert "Previous" not in out
    out = render("collection", children=[], count=0, label="", next=None, previous="/p")
    assert 'href="/p"' in out and "Next" not in out


def test_notice_context():
    out = render("notice", message="Nope <b>")
    assert 'role="alert"' in out and "Nope &lt;b&gt;" in out
    assert "scrud-notice-error" in out
    assert "scrud-notice-info" in render("notice", message="hi", level="info")


def test_all_builtins_render_with_minimal_context():
    for name in BUILTIN_NAMES:
        render(name[len("scrud:") :])


def test_object_with_property_named_items_and_keys():
    kids = {"items": mark_safe("<b>I</b>"), "keys": mark_safe("<b>K</b>")}
    for short in ("object", "object-edit"):
        out = render(short, children=kids, child_items=list(kids.items()))
        assert "<b>I</b>" in out and "<b>K</b>" in out
        assert "items" in out and "keys" in out


def test_array_edit_append_field_named_items():
    out = render(
        "array-edit",
        pointer="/x",
        input_id="scrud-f-~1x",
        append_fields=[
            {"name": "items", "type": "string"},
            {"name": "keys", "type": "boolean"},
        ],
    )
    assert 'name="/x/-/items"' in out and 'name="/x/-/keys"' in out
    assert 'id="scrud-f-~1x-new-1"' in out and 'for="scrud-f-~1x-new-2"' in out


def test_label_uses_input_id():
    out = render("string-edit", pointer="/a", input_id="zz", label="A")
    assert 'id="zz"' in out and 'for="zz"' in out
