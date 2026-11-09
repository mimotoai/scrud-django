"""The built-in generic components render each JSON Schema type."""

import json

import pytest
from django.utils.safestring import mark_safe

from scrud_django.ui.cascade import BUILTIN_NAMES, builtin_component
from scrud_django.ui.engines import cached_template


def escape_segment(name):
    return name.replace("~", "~0").replace("/", "~1")


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
        "append_fields",
        [
            {"name": k, "type": v.get("type"), "pointer_name": escape_segment(k)}
            for k, v in props.items()
        ],
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
        schema={"type": "string", "maxLength": 100},
    )
    assert 'name="/items/0/text"' in edit
    assert 'value="Buy milk"' in edit
    assert "<label" in edit
    assert "<form" not in edit
    assert "<textarea" not in edit


@pytest.mark.parametrize(
    "value, schema",
    [
        ("line 1\nline <2>", {"type": "string", "maxLength": 100}),
        ("line 1\r\nline <2>", {"type": "string", "format": "email"}),
        ("short <2>", {"type": "string"}),
        ("short <2>", {}),
        ("short <2>", {"type": "string", "maxLength": 201}),
    ],
)
def test_string_edit_textarea(value, schema):
    # A text input's value loses CR and LF in the browser: multi-line text,
    # and strings that may be long, edit in a textarea.
    out = render(
        "string-edit",
        value=value,
        pointer="/a",
        input_id="scrud-x",
        label="A",
        schema=schema,
        errors=["Bad"],
    )
    assert "<input" not in out
    escaped = value.replace("<", "&lt;").replace(">", "&gt;")
    # the newline after the start tag keeps a leading newline of the value
    assert (
        f'<textarea id="scrud-x" name="/a" aria-invalid="true">\n{escaped}</textarea>'
        in out
    )
    assert '<label for="scrud-x">A</label>' in out


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "maxLength": 200},
        {"type": "string", "format": "email"},
    ],
)
def test_string_edit_single_line_input(schema):
    out = render("string-edit", value="a <b>", pointer="/a", schema=schema)
    assert "<textarea" not in out
    assert 'type="text"' in out and 'value="a &lt;b&gt;"' in out


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


def test_array_edit_append_row_types_and_escaped_names():
    out = render(
        "array-edit",
        pointer="/x",
        input_id="n",
        schema={
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "a/b": {"type": "integer"},
                    "c~d": {"type": "number"},
                    "<e>": {"type": "boolean"},
                    "f": {"type": "string"},
                },
            },
        },
    )
    assert (
        '<label for="n-new-1">a/b</label> '
        '<input type="number" step="1" id="n-new-1" name="/x/-/a~1b">'
    ) in out
    assert '<input type="number" step="any" id="n-new-2" name="/x/-/c~0d">' in out
    assert '<label for="n-new-3">&lt;e&gt;</label>' in out
    assert '<input type="checkbox" id="n-new-3" name="/x/-/&lt;e&gt;">' in out
    assert '<input type="text" id="n-new-4" name="/x/-/f">' in out


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


def test_uri_edit():
    out = render(
        "uri-edit",
        value="https://ex.com/a?b=<c>",
        pointer="/link",
        input_id="scrud-l",
        label="Link",
        schema={"type": "string", "format": "uri"},
        errors=["Bad"],
    )
    assert (
        '<input type="url" id="scrud-l" name="/link"'
        ' value="https://ex.com/a?b=&lt;c&gt;" aria-invalid="true">'
    ) in out
    assert '<label for="scrud-l">Link</label>' in out
    assert "<textarea" not in out
    # a multi-line value is no URI: still an input, never a textarea
    assert "<textarea" not in render("uri-edit", value="a\nb", pointer="/l")
    assert 'value=""' in render("uri-edit", value=None, pointer="/l")


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
        "uri-edit",
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
            {"name": "items", "type": "string", "pointer_name": "items"},
            {"name": "keys", "type": "boolean", "pointer_name": "keys"},
        ],
    )
    assert 'name="/x/-/items"' in out and 'name="/x/-/keys"' in out
    assert 'id="scrud-f-~1x-new-1"' in out and 'for="scrud-f-~1x-new-2"' in out


@pytest.mark.parametrize(
    "item_type, expected",
    [
        ("integer", '<input type="number" step="1" name="/nums/-"'),
        ("number", '<input type="number" step="any" name="/nums/-"'),
        ("boolean", '<input type="checkbox" name="/nums/-"'),
        ("string", '<input type="text" name="/nums/-"'),
        (None, '<input type="text" name="/nums/-"'),
    ],
)
def test_array_edit_append_input_for_primitive_items(item_type, expected):
    out = render("array-edit", pointer="/nums", append_item_type=item_type)
    assert expected in out
    assert out.count("<input") == 1


def test_label_uses_input_id():
    out = render("string-edit", pointer="/a", input_id="zz", label="A")
    assert 'id="zz"' in out and 'for="zz"' in out


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:x",
        " javascript:x",
        "data:text/html,x",
        "//evil.example",
        "/\\evil.example",
        "vbscript:x",
    ],
)
def test_uri_unsafe_schemes_are_plain_text(bad):
    out = render("uri", value=bad)
    assert "<a" not in out and "href" not in out
    assert 'class="scrud-uri"' in out


@pytest.mark.parametrize(
    "good", ["https://ex.com/a", "HTTP://ex.com", "mailto:a@ex.com", "/relative"]
)
def test_uri_safe_values_link(good):
    assert f'<a class="scrud-uri" href="{good}"' in render("uri", value=good)


def test_uri_href_is_escaped():
    out = render("uri", value="https://ex.com/a?b=<c>&d=1")
    assert 'href="https://ex.com/a?b=&lt;c&gt;&amp;d=1"' in out


@pytest.mark.parametrize("empty", [None, ""])
def test_uri_empty_renders_nothing(empty):
    out = render("uri", value=empty)
    assert "href" not in out and "None" not in out and "<a" not in out


def test_enum_numeric_and_boolean_selection():
    out = render("enum-edit", value=2, pointer="/n", schema={"enum": [1, 2, 3]})
    assert out.count(" selected") == 1 and '<option value="2" selected>' in out
    out = render("enum-edit", value=True, pointer="/b", schema={"enum": [1, True]})
    assert out.count(" selected") == 1 and '<option value="True" selected>' in out
    out = render("enum-edit", value=1, pointer="/b", schema={"enum": [True, 1]})
    assert out.count(" selected") == 1 and '<option value="1" selected>' in out


def test_number_not_localized():
    from django.utils import translation

    with translation.override("fr"):
        assert 'value="1.5"' in render("number-edit", value=1.5, pointer="/n")
        assert "1.5" in render("number", value=1.5)


def test_array_edit_append_row_skips_object_and_array_fields():
    out = render(
        "array-edit",
        pointer="/x",
        input_id="n",
        append_fields=[
            {"name": "meta", "type": "object", "pointer_name": "meta"},
            {"name": "name", "type": "string", "pointer_name": "name"},
            {"name": "tags", "type": "array", "pointer_name": "tags"},
        ],
    )
    assert 'name="/x/-/name"' in out
    assert "meta" not in out and "tags" not in out
    assert out.count("<input") == 1 and out.count("<label") == 1


def test_array_edit_append_row_with_only_nested_fields_has_no_input():
    out = render(
        "array-edit",
        pointer="/x",
        append_fields=[{"name": "meta", "type": "object", "pointer_name": "meta"}],
    )
    assert "<input" not in out and "<label" not in out


@pytest.mark.parametrize("item_type", ["object", "array"])
def test_array_edit_no_append_row_for_object_or_array_items(item_type):
    out = render("array-edit", pointer="/x", append_item_type=item_type)
    assert "<input" not in out and "scrud-append" not in out
