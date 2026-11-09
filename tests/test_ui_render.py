"""The render tree: a resource's JSON rendered as HTML through the components
its stylesheets resolve, in the mode the viewing user may act in."""

import json
import logging
import re
from uuid import uuid4

import pytest
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.timezone import now

from scrud_django.models import Resource, ResourceType
from scrud_django.registration import ResourceRegistration
from scrud_django.ui import authz, render
from scrud_django.ui.render import render_collection, render_resource

from .fixtures import *  # noqa: F401, F403
from .fixtures import (  # noqa: F401
    TYPE_URI_TODO_LIST,
    assign_role,
    role_todo_creator,
    todo_creator_login,
)

PREFIXES = {"ex": "http://example.org/todo#"}
ROW = '<li class="row">'


def request_for(user):
    request = RequestFactory().get("/")
    request.user = user
    return request


def detail_url(resource):
    return reverse(f"{resource.resource_type.slug}-detail", args=[resource.pk])


def html_of(resource, user, content=None, **kwargs):
    """`resource` rendered for `user` (its stored content, or `content`)."""
    return str(
        render_resource(
            resource.content if content is None else content,
            resource_type=resource.resource_type,
            href=detail_url(resource),
            etag=resource.etag,
            request=request_for(user),
            **kwargs,
        )
    )


def make_component(name, template, mode="view", engine="django", **overrides):
    reverse("ui-components-list")
    return ResourceRegistration.register(
        {
            "name": name,
            "engine": engine,
            "mode": mode,
            "template": template,
            "rbac_context": "",
            **overrides,
        },
        "ui-components",
    )


def make_stylesheet(name, rules, context="", **overrides):
    reverse("ui-stylesheets-list")
    return ResourceRegistration.register(
        {
            "name": name,
            "prefixes": PREFIXES,
            "rules": [{"select": s, "component": c} for s, c in rules],
            "rbac_context": context,
            **overrides,
        },
        "ui-stylesheets",
    )


def make_list(title="Groceries", context="project-a", items=None):
    reverse("todo-lists-list")
    if items is None:
        items = [{"text": "Buy milk", "done": False}]
    return ResourceRegistration.register(
        {"title": title, "rbac_context": context, "items": items}, "todo-lists"
    )


def without_content(output):
    """`output` without its ``scrud-content`` script elements."""
    return re.sub(
        r'<script type="application/json" class="scrud-content">.*?</script>',
        "",
        output,
        flags=re.S,
    )


def scripted_content(output):
    """The JSON of every ``scrud-content`` script element in `output`."""
    found = re.findall(
        r'<script type="application/json" class="scrud-content">(.*?)</script>',
        output,
        re.S,
    )
    return [json.loads(text) for text in found]


@pytest.mark.django_db
def test_unstyled_list_renders_generic(todo_list, todo_owner_login):
    output = html_of(todo_list, todo_owner_login)
    assert "Groceries" in output
    assert "Buy milk" in output and "Buy eggs" in output
    done = re.search(r'<input type="checkbox"[^>]*name="/items/1/done"[^>]*>', output)
    assert done and "checked" in done.group(0)
    not_done = re.search(
        r'<input type="checkbox"[^>]*name="/items/0/done"[^>]*>', output
    )
    assert not_done and "checked" not in not_done.group(0)
    assert f'hx-put="{detail_url(todo_list)}"' in output
    assert output.startswith('<form class="scrud-resource"')
    assert f'data-scrud-etag="{todo_list.etag}"' in output
    assert 'data-scrud-pointer=""' in output
    assert 'hx-ext="scrud-json"' in output


@pytest.mark.django_db
def test_reader_gets_view_mode(todo_list, todo_reader_login, todo_owner_login):
    output = html_of(todo_list, todo_reader_login)
    assert "Buy milk" in output
    assert "<input" not in output
    assert "hx-put" not in output
    assert "hx-delete" not in output
    assert output.startswith('<div class="scrud-resource"')
    owner = html_of(todo_list, todo_owner_login)
    assert (
        f'<button type="button" hx-delete="{detail_url(todo_list)}"'
        ' hx-ext="scrud-json" hx-confirm="Delete?"'
    ) in owner


@pytest.mark.django_db
def test_decisions(todo_list, todo_reader_login, todo_owner_login, login_no_roles):
    def decisions(user):
        return authz.decisions_for(
            request_for(user), TYPE_URI_TODO_LIST, todo_list.content
        )

    assert decisions(todo_owner_login) == {
        "GET": "allowed",
        "PUT": "allowed",
        "DELETE": "allowed",
    }
    assert decisions(todo_reader_login) == {
        "GET": "allowed",
        "PUT": "denied",
        "DELETE": "denied",
    }
    assert set(decisions(login_no_roles).values()) == {"denied"}


@pytest.mark.django_db
def test_forced_mode(todo_list, todo_owner_login, todo_reader_login):
    view = html_of(todo_list, todo_owner_login, mode="view")
    assert "<input" not in view and "hx-put" not in view
    # forcing the mode never adds the delete button the decisions deny
    edit = html_of(todo_list, todo_reader_login, mode="edit")
    assert 'name="/items/0/text"' in edit and "hx-delete" not in edit


@pytest.mark.django_db
def test_component_stylesheets_apply_to_subtree(todo_reader_login):
    make_component(
        "todo-list", "<section>{{ children.items }}</section>", stylesheets=["rows"]
    )
    make_component("todo-row", '<li class="row">{{ value.text }}</li>')
    make_component("plain-row", '<li class="plain">{{ value.text }}</li>')
    # app-wide, "base" (the higher order) beats "rows"; linked, "rows" wins
    make_stylesheet("rows", [("ex:TodoItem", "todo-row")], order=0)
    make_stylesheet(
        "base",
        [("ex:TodoItem", "plain-row"), ("fancy ex:TodoList", "todo-list")],
        order=1,
    )
    styled = make_list("Styled")
    sibling = make_list("Sibling")
    output = html_of(styled, todo_reader_login, scope_chain=("fancy",))
    assert "<section>" in output
    assert output.count(ROW) == 1 and '<li class="plain">' not in output
    assert 'data-scrud-scope="fancy"' in output
    other = html_of(sibling, todo_reader_login)
    assert "<section>" not in other
    assert '<li class="plain">' in other and ROW not in other


@pytest.mark.django_db
def test_styled_row_by_class_iri(todo_list, todo_reader_login):
    make_component("todo-row", '<li class="row">{{ value.text }}</li>')
    make_stylesheet("s", [("ex:TodoItem", "todo-row")])
    output = html_of(todo_list, todo_reader_login)
    assert output.count(ROW) == 2
    assert '<li class="row">Buy milk</li>' in output


@pytest.mark.django_db
def test_own_type_member_names_the_class(todo_list, todo_reader_login):
    make_component("todo-row", '<li class="row">{{ value.text }}</li>')
    make_component("urgent", '<li class="urgent">{{ value.text }}</li>')
    make_stylesheet("s", [("ex:TodoItem", "todo-row"), ("ex:Urgent", "urgent")])
    content = {
        **todo_list.content,
        "items": [
            {"text": "Buy milk", "@type": "ex:Urgent"},
            {"text": "Buy eggs"},
        ],
    }
    output = html_of(todo_list, todo_reader_login, content=content)
    assert '<li class="urgent">Buy milk</li>' in output
    assert '<li class="row">Buy eggs</li>' in output


@pytest.mark.django_db
def test_property_iri_styles_container(todo_list, todo_reader_login):
    make_component("todo-row", '<li class="row">{{ value.text }}</li>')
    make_component(
        "my-list", '<ul class="mine">{% for c in children %}{{ c }}{% endfor %}</ul>'
    )
    make_stylesheet("s", [("ex:TodoItem", "todo-row"), ("ex:items", "my-list")])
    output = html_of(todo_list, todo_reader_login)
    assert output.count('<ul class="mine">') == 1
    assert (
        '<ul class="mine"><li class="row">Buy milk</li>'
        '<li class="row">Buy eggs</li></ul>'
    ) in output


@pytest.mark.django_db
def test_component_opens_scope_for_children(todo_list, todo_reader_login):
    make_component(
        "list-c",
        '<div class="list-c">{{ children.items }}</div>',
        scopes={"items": "row"},
    )
    make_component("todo-row", '<li class="row">{{ value.text }}</li>')
    make_component("compact", '<li class="compact">{{ value.text }}</li>')
    make_stylesheet(
        "s",
        [
            ("ex:TodoList", "list-c"),
            ("ex:TodoItem", "todo-row"),
            ("row ex:TodoItem", "compact"),
        ],
    )
    output = html_of(todo_list, todo_reader_login)
    assert '<div class="list-c">' in output
    assert output.count('<li class="compact">') == 2
    assert ROW not in output


@pytest.mark.django_db
def test_context_stylesheet_applies_only_there(admin_login):
    make_component("a-list", '<div class="a-list">{{ value.title }}</div>')
    make_stylesheet("s", [("ex:TodoList", "a-list")], context="project-a")
    in_a = make_list("In A", context="project-a")
    in_b = make_list("In B", context="project-b")
    assert '<div class="a-list">In A</div>' in html_of(in_a, admin_login, mode="view")
    other = html_of(in_b, admin_login, mode="view")
    assert "a-list" not in other and '<dl class="scrud-object">' in other


@pytest.mark.django_db
def test_jinja_component_renders(todo_list, todo_reader_login):
    make_component("todo-row", '<li class="row">{{ value.text }}</li>', engine="jinja2")
    make_stylesheet("s", [("ex:TodoItem", "todo-row")])
    output = html_of(todo_list, todo_reader_login)
    assert output.count(ROW) == 2
    assert '<li class="row">Buy eggs</li>' in output


@pytest.mark.django_db
def test_workflow_put_is_possibly(todo_creator_login):
    reverse("todos-list")
    todo = ResourceRegistration.register(
        {"title": "Buy milk", "status": "not started", "priority": "low"}, "todos"
    )
    request = request_for(todo_creator_login)
    decisions = authz.decisions_for(request, "tests://ToDo", todo.content)
    assert decisions == {"GET": "allowed", "PUT": "possibly", "DELETE": "denied"}
    output = html_of(todo, todo_creator_login)
    assert f'hx-put="{detail_url(todo)}"' in output
    assert 'name="/status"' in output
    assert "hx-delete" not in output


@pytest.mark.django_db
def test_refusal_messages_readers_only(todo_list, login_no_roles):
    put_grant = {
        "http.PUT": {
            TYPE_URI_TODO_LIST: {
                "condition": "size(items) < 3",
                "message": "At most 3 items",
            }
        }
    }
    reader = assign_role({"http.GET": [TYPE_URI_TODO_LIST], **put_grant}, "project-a")
    writer_only = assign_role(put_grant, "project-a")
    body = {
        **todo_list.content,
        "items": [{"text": str(n)} for n in range(3)],
    }

    def messages(user, sent=body):
        return authz.refusal_messages(
            request_for(user), TYPE_URI_TODO_LIST, sent, todo_list.content
        )

    assert messages(reader) == ["At most 3 items"]
    assert messages(writer_only) == []
    assert messages(login_no_roles) == []
    # an allowed body has nothing refused
    assert messages(reader, todo_list.content) == []


@pytest.mark.django_db
def test_content_not_matching_schema(todo_list, todo_owner_login):
    """Review Focus 1: legacy content that its schema refuses still renders:
    a missing property renders nothing, an unknown key is ignored, a value of
    the wrong type renders as JSON."""
    content = {
        "title": "Legacy",
        "rbac_context": "project-a",
        "items": [
            {"text": "Buy milk", "done": "yes", "x": 1},
            {"text": "Buy eggs"},
        ],
    }
    output = html_of(todo_list, todo_owner_login, content=content)
    assert '<pre class="scrud-json">&quot;yes&quot;</pre>' in output
    assert 'name="/items/0/done"' not in output
    assert "/items/0/x" not in output and ">x<" not in output
    assert 'name="/items/1/text"' in output
    assert "/items/1/done" not in output
    # the frame still carries the whole content, unknown key included
    assert scripted_content(output) == [content]

    wrong = {"title": 5, "rbac_context": "project-a", "items": "none"}
    output = html_of(todo_list, todo_owner_login, content=wrong)
    assert '<pre class="scrud-json">5</pre>' in output
    assert '<pre class="scrud-json">&quot;none&quot;</pre>' in output
    assert 'name="/title"' not in output


@pytest.mark.django_db
@pytest.mark.parametrize(
    "engine, template",
    [
        ("jinja2", "{{ value.text.upper().nope() }}"),
        ("django", '{% url "no-such-route" %}'),
    ],
)
def test_failing_component_falls_back(
    todo_list, todo_reader_login, caplog, engine, template
):
    """Review Focus 2: a stored component that raises while rendering renders
    the built-in for its node and mode, after a comment naming it."""
    make_component("bad", template, engine=engine)
    make_stylesheet("s", [("ex:TodoItem", "bad")])
    with caplog.at_level(logging.WARNING, logger="scrud_django.ui.render"):
        output = html_of(todo_list, todo_reader_login)
    comment = '<!-- scrud-ui: component "bad" failed -->'
    assert output.count(comment) == 2
    assert comment + '<dl class="scrud-object">' in output
    assert "Buy milk" in output and "Buy eggs" in output
    assert any("component 'bad'" in r.getMessage() for r in caplog.records)


@pytest.mark.django_db
def test_failing_component_name_cannot_close_the_comment(todo_list, admin_login):
    # names are restricted by their schema; a raw save bypasses it
    reverse("ui-components-list")
    component = Resource(
        resource_type=ResourceType.objects.get(slug="ui-components"),
        content={
            "name": "x-->y",
            "engine": "jinja2",
            "mode": "view",
            "template": "{{ value.nope() }}",
            "rbac_context": "",
        },
        etag=uuid4().hex,
        modified_at=now(),
    )
    component.save()
    make_stylesheet("s", [("ex:TodoItem", "x-->y")])
    output = html_of(todo_list, admin_login, mode="view")
    assert '<!-- scrud-ui: component "x--&gt;y" failed -->' in output
    assert "x-->y" not in output


@pytest.mark.django_db
@pytest.mark.parametrize("engine", ["django", "jinja2"])
def test_stored_components_escape(todo_reader_login, todo_owner_login, engine):
    """Review Focus 3: markup in content renders escaped, in stored components
    of both engines and in the built-ins in either mode."""
    script = "<script>alert(1)</script>"
    todo = make_list(items=[{"text": script, "done": False}], title=script)
    for user in (todo_reader_login, todo_owner_login):
        output = html_of(todo, user)
        assert "<script>alert" not in output
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in output
        assert scripted_content(output)[0]["title"] == script
    make_component("todo-row", '<li class="row">{{ value.text }}</li>', engine=engine)
    make_stylesheet("s", [("ex:TodoItem", "todo-row")])
    output = html_of(todo, todo_reader_login)
    assert '<li class="row">&lt;script&gt;alert(1)&lt;/script&gt;</li>' in output
    assert "<script>alert" not in output


def node_type(schema, ld=None):
    """An unsaved resource type with `schema` and JSON-LD context `ld`."""
    return ResourceType(
        type_uri="tests://Node",
        slug="nodes",
        schema=Resource(content=schema),
        context=Resource(content=ld or {}),
    )


def render_node_type(resource_type, content, user, **kwargs):
    return str(
        render_resource(
            content,
            resource_type=resource_type,
            href="/nodes/1/",
            etag="e",
            request=request_for(user),
            **kwargs,
        )
    )


@pytest.mark.django_db
def test_recursive_ref_terminates(admin_login):
    """Review Focus 4: a recursive schema renders as deep as the content goes;
    a `$ref` cycle that never descends renders the JSON fallback."""
    schema = {
        "type": "object",
        "properties": {"head": {"$ref": "#/definitions/Node"}},
        "definitions": {
            "Node": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "next": {"$ref": "#/definitions/Node"},
                },
            }
        },
    }
    content = {
        "head": {"name": "one", "next": {"name": "two", "next": {"name": "three"}}}
    }
    output = render_node_type(node_type(schema), content, admin_login)
    for pointer in ("/head/name", "/head/next/name", "/head/next/next/name"):
        assert f'name="{pointer}"' in output

    cyclic = {
        "type": "object",
        "properties": {"loop": {"$ref": "#/definitions/A"}},
        "definitions": {
            "A": {"$ref": "#/definitions/B"},
            "B": {"$ref": "#/definitions/A"},
        },
    }
    output = render_node_type(node_type(cyclic), {"loop": {"a": 1}}, admin_login)
    assert "scrud-json" in output and "&quot;a&quot;: 1" in output
    self_ref = {"$ref": "#"}
    output = render_node_type(node_type(self_ref), {"a": 1}, admin_login)
    assert "scrud-json" in output


@pytest.mark.django_db
def test_depth_limit_renders_json(admin_login, monkeypatch):
    monkeypatch.setattr(render, "max_body_depth", lambda: 2)
    schema = {
        "type": "object",
        "properties": {"next": {"$ref": "#"}, "name": {"type": "string"}},
    }
    content = {
        "name": "0",
        "next": {"name": "1", "next": {"name": "2", "next": {"name": "3"}}},
    }
    output = render_node_type(node_type(schema), content, admin_login)
    # nodes nested deeper than the limit (2) render as JSON
    assert 'name="/name"' in output and 'name="/next/name"' in output
    assert 'name="/next/next/name"' not in output
    assert '<pre class="scrud-json">&quot;2&quot;</pre>' in output
    assert '<pre class="scrud-json">{\n  &quot;name&quot;: &quot;3&quot;\n}</pre>' in (
        output
    )


@pytest.mark.django_db
def test_schema_keywords_without_children(admin_login):
    schema = {
        "type": "object",
        "properties": {
            "choice": {"oneOf": [{"type": "string"}, {"type": "number"}]},
            "free": {"type": "object"},
            "count": {"type": "integer"},
            "remote": {"$ref": "http://example.org/schema.json"},
            "untyped": {},
        },
    }
    content = {
        "choice": "a",
        "free": {"k": "v"},
        "count": 3,
        "remote": "r",
        "untyped": "u",
    }
    output = render_node_type(node_type(schema), content, admin_login)
    assert '<pre class="scrud-json">&quot;a&quot;</pre>' in output
    assert "&quot;k&quot;: &quot;v&quot;" in output
    assert '<pre class="scrud-json">&quot;r&quot;</pre>' in output
    assert 'type="number" step="1"' in output and 'name="/count"' in output
    # a schema with no type renders by the value's own type
    assert 'name="/untyped"' in output


@pytest.mark.django_db
def test_ld_keywords_are_not_classes(admin_login):
    make_component("id-styled", '<b class="id">{{ value }}</b>')
    # "k:id" expands to the keyword "@id"
    make_stylesheet("s", [("k:id", "id-styled")], prefixes={"k": "@"})
    schema = {"type": "object", "properties": {"link": {"type": "string"}}}
    ld = {"link": {"@id": "http://example.org/link", "@type": "@id"}}
    output = render_node_type(
        node_type(schema, ld), {"link": "x"}, admin_login, mode="view"
    )
    assert 'class="id"' not in output
    assert '<span class="scrud-string">x</span>' in output
    # a context that is not a JSON object is no context
    output = render_node_type(node_type(schema, ["x"]), {"link": "x"}, admin_login)
    assert 'name="/link"' in output


@pytest.mark.django_db
def test_input_ids_unique(admin_login):
    schema = {
        "type": "object",
        "properties": {
            "a-b": {"type": "string"},
            "a": {"type": "object", "properties": {"b": {"type": "string"}}},
            "c/d": {"type": "string"},
            "c~1d": {"type": "string"},
        },
    }
    content = {"a-b": "1", "a": {"b": "2"}, "c/d": "3", "c~1d": "4"}
    output = render_node_type(node_type(schema), content, admin_login)
    ids = re.findall(r'<input[^>]* id="([^"]+)"', output)
    assert len(ids) == 4 and len(set(ids)) == 4
    assert all(re.fullmatch(r"[^\s]+", i) for i in ids)
    assert 'name="/c~1d"' in output and 'name="/c~01d"' in output


@pytest.mark.django_db
def test_errors_render(todo_list, todo_owner_login):
    errors = {
        "": ["Not allowed"],
        "/items/0/text": ["Too long"],
        "/nowhere": ["Lost?"],
    }
    output = html_of(todo_list, todo_owner_login, errors=errors)
    notice = '<div class="scrud-notice scrud-notice-error" role="alert">'
    assert notice + "Not allowed</div>" in output
    text = output.index('name="/items/0/text"')
    assert output.index("Too long") > text
    # a message for a node that is not rendered is shown at the top
    assert notice + "/nowhere: Lost?</div>" in output


@pytest.mark.django_db
def test_pointer_renders_node_frame(todo_list, todo_owner_login):
    output = html_of(todo_list, todo_owner_login, pointer="/items/1")
    assert 'data-scrud-pointer="/items/1"' in output
    assert "Buy eggs" in output and "Buy milk" not in without_content(output)
    assert 'name="/items/1/done"' in output
    # only a root frame is a form: a node frame may sit inside one
    assert output.startswith('<div class="scrud-resource"')
    assert "<form" not in output
    assert f'hx-put="{detail_url(todo_list)}"' in output
    assert 'hx-ext="scrud-json"' in output
    assert "hx-delete" not in output
    # the frame carries the whole content, from which the PUT body is built
    assert scripted_content(output) == [todo_list.content]


@pytest.mark.django_db
def test_pointer_frame_keeps_scopes_and_linked_stylesheets(todo_list, admin_login):
    make_component(
        "list-c",
        "<div>{{ children.items }}</div>",
        mode="edit",
        scopes={"items": "row"},
        stylesheets=["rows"],
    )
    make_component("compact", '<p class="compact">{{ value.text }}</p>', mode="edit")
    make_stylesheet("rows", [("row ex:TodoItem", "compact")], order=-1)
    make_stylesheet("s", [("ex:TodoList", "list-c")])
    whole = html_of(todo_list, admin_login)
    node = html_of(todo_list, admin_login, pointer="/items/0")
    assert '<p class="compact">Buy milk</p>' in whole
    assert '<p class="compact">Buy milk</p>' in node


@pytest.mark.django_db
def test_pointer_to_missing_node(todo_list, todo_owner_login):
    output = html_of(todo_list, todo_owner_login, pointer="/items/9")
    assert 'data-scrud-pointer="/items/9"' in output
    assert '<pre class="scrud-json">null</pre>' in output


@pytest.mark.django_db
def test_frame_content_cannot_break_out(todo_owner_login):
    title = "</script><script>alert(1)</script>&"
    todo = make_list(title=title)
    output = html_of(todo, todo_owner_login)
    assert "</script><script>" not in output
    assert scripted_content(output)[0]["title"] == title
    # attribute values are escaped
    output = html_of(todo, todo_owner_login, scope_chain=('a"b', "c"))
    assert 'data-scrud-scope="a&quot;b c"' in output


def envelope_of(resources):
    return {
        "count": len(resources),
        "next": "/todo-lists/?page=2",
        "previous": None,
        "content": [
            {
                "content": r.content,
                "etag": r.etag,
                "href": detail_url(r),
                "last_modified": "",
            }
            for r in resources
        ],
    }


def collection_html(resources, user):
    return str(
        render_collection(
            envelope_of(resources),
            resource_type=resources[0].resource_type,
            href=reverse("todo-lists-list"),
            request=request_for(user),
            scope_chain=(),
        )
    )


@pytest.mark.django_db
def test_collection_items_use_their_own_contexts():
    reader = assign_role({"http.GET": [TYPE_URI_TODO_LIST]}, "project-a", "project-b")
    make_component("a-list", '<div class="a-list">{{ value.title }}</div>')
    make_stylesheet("s", [("ex:TodoList", "a-list")], context="project-a")
    in_a = make_list("In A", context="project-a")
    in_b = make_list("In B", context="project-b")
    output = collection_html([in_a, in_b], reader)
    assert output.count('class="scrud-resource"') == 2
    assert '<div class="a-list">In A</div>' in output
    assert "a-list" not in output.split(detail_url(in_b))[1]
    assert '<section class="scrud-collection">' in output
    assert 'href="/todo-lists/?page=2"' in output


@pytest.mark.django_db
def test_render_query_count_is_constant(todo_owner_login):
    make_component("todo-row", '<li class="row">{{ value.text }}</li>', mode="edit")
    make_stylesheet("s", [("ex:TodoItem", "todo-row")])
    make_stylesheet("p", [("ex:TodoItem", "todo-row")], context="project-a")

    def count(n):
        lists = [
            make_list(f"L{i}", items=[{"text": f"t{j}"} for j in range(n)])
            for i in range(n)
        ]
        with CaptureQueriesContext(connection) as queries:
            output = collection_html(lists, todo_owner_login)
        assert output.count(ROW) == n * n
        return len(queries)

    two, four = count(2), count(4)
    assert two == four
    assert two <= 6, two
