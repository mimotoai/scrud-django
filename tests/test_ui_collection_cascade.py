"""The list's wrapper is resolved through the cascade: a rule on the collection
IRI (or ``scrud:collection``) restyles it; with none, the built-in renders."""

import logging

import pytest

from scrud_django import collection_type_uri_for
from scrud_django.ui.render import render_collection

from .fixtures import *  # noqa: F401, F403
from .fixtures import TYPE_URI_TODO_LIST
from .test_ui_plain_output import ARRAY_OPS, golden, normalized
from .test_ui_render import (  # noqa: F401
    envelope_of,
    make_component,
    make_list,
    make_stylesheet,
    request_for,
)

COLLECTION_IRI = collection_type_uri_for(TYPE_URI_TODO_LIST)
WRAPPER = (
    '<div class="studio-list" data-href="{{ href }}" data-scope="{{ scope|join:"," }}">'
    "{{ count }}|{{ label }}|{% for c in children %}{{ c }}{% endfor %}</div>"
)


def collection_html(resources, user, **kwargs):
    return str(
        render_collection(
            envelope_of(resources),
            resource_type=resources[0].resource_type,
            href="/todo-lists/",
            request=request_for(user),
            **kwargs,
        )
    )


@pytest.mark.django_db
def test_list_unchanged_without_rule(todo_owner_login):
    lists = [make_list("A"), make_list("B")]
    output = collection_html(lists, todo_owner_login)
    assert output.startswith(
        '<div class="scrud-collection-frame" data-scrud-href="/todo-lists/"'
        ' data-scrud-scope=""><section class="scrud-collection">'
    )
    assert "studio-list" not in output
    assert output.count('class="scrud-resource"') == 2
    # Exactly develop's output (f43f1e8, `tests/plain_output/`), but for
    # the array edit controls of each list's one item, in edit mode.
    edit = normalized(output)
    assert len(ARRAY_OPS.findall(edit)) == 2
    assert ARRAY_OPS.sub("", edit) == golden("collection")


@pytest.mark.django_db
def test_rule_on_collection_iri_restyles_one_type_list(todo_owner_login):
    make_component("studio-list", WRAPPER)
    make_stylesheet("s", [(COLLECTION_IRI, "studio-list")])
    lists = [make_list("A")]
    output = collection_html(lists, todo_owner_login)
    assert '<div class="studio-list" data-href="/todo-lists/" data-scope="">' in output
    assert '<section class="scrud-collection">' not in output
    assert output.startswith('<div class="scrud-collection-frame"')
    assert 'class="scrud-resource"' in output
    # another type's list is unchanged
    other = type("Other", (), {"type_uri": "http://example.org/other#Other"})
    other.schema = lists[0].resource_type.schema
    unchanged = str(
        render_collection(
            envelope_of([]),
            resource_type=other,
            href="/others/",
            request=request_for(todo_owner_login),
        )
    )
    assert "studio-list" not in unchanged
    assert '<section class="scrud-collection">' in unchanged


@pytest.mark.django_db
def test_scoped_collection_rule(todo_owner_login):
    make_component("studio-list", WRAPPER)
    make_stylesheet("s", [("sidebar scrud:collection", "studio-list")])
    lists = [make_list("A")]
    assert "studio-list" not in collection_html(lists, todo_owner_login)
    output = collection_html(lists, todo_owner_login, scope_chain=("sidebar",))
    assert 'data-scope="sidebar"' in output


@pytest.mark.django_db
def test_failing_collection_component_falls_back(todo_owner_login, caplog):
    make_component("bad-j", "{{ value.nope() }}", engine="jinja2")
    make_stylesheet("s", [(COLLECTION_IRI, "bad-j")])
    lists = [make_list("A")]
    with caplog.at_level(logging.WARNING, logger="scrud_django.ui.render"):
        output = collection_html(lists, todo_owner_login)
    assert (
        '<div class="scrud-collection-frame" data-scrud-href="/todo-lists/"'
        ' data-scrud-scope=""><!-- scrud-ui: component "bad-j" failed -->'
        '<section class="scrud-collection">'
    ) in output
    assert 'class="scrud-resource"' in output
    assert any("component 'bad-j'" in r.getMessage() for r in caplog.records)
