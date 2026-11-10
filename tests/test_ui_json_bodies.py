"""R3 from the server's side: a stored component's `hx-post` /
`data-scrud-json="form"` markup renders as written, and the JSON body the
client builds from its inputs is an ordinary create."""

import json

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_client import BUILD_SCRIPT, parse_form, run_node
from .test_ui_render import make_component, make_list, make_stylesheet
from .test_ui_static import require_node

HTML = "text/html"


@pytest.mark.django_db
@test_context
def test_create_through_rendered_form_markup_returns_201_html(
    todo_owner_login,
):
    require_node()
    url = reverse("todo-lists-list")
    make_component(
        "create-list",
        '<form hx-ext="scrud-json" hx-post="' + url + '" data-scrud-json="form">'
        '<input type="text" name="/title" value="Created">'
        '<input type="text" name="/rbac_context" value="project-a">'
        '<input type="text" name="/items/-/text" value="Buy milk">'
        '<input type="checkbox" name="/items/-/done" checked>'
        "</form>",
        engine="jinja2",
        mode="edit",
    )
    make_stylesheet("s", [("ex:TodoList", "create-list")], context="project-a")
    shell = make_list("Shell")
    Given.http.force_authenticate(todo_owner_login)
    When.http.get(
        reverse("todo-lists-detail", args=[shell.pk]),
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(200)
    markup = Http.response.content.decode()
    assert 'hx-post="' + url + '"' in markup
    assert 'data-scrud-json="form"' in markup and 'hx-ext="scrud-json"' in markup

    form = parse_form(markup)
    built = run_node(
        BUILD_SCRIPT, stdin=json.dumps({"content": {}, "fields": form.fields})
    )
    assert built["errors"] == []
    assert built["body"] == {
        "title": "Created",
        "rbac_context": "project-a",
        "items": [{"text": "Buy milk", "done": True}],
    }
    When.http.post(
        url,
        data=json.dumps(built["body"]),
        content_type="application/json",
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(201)
    assert Http.response["Content-Type"].startswith(HTML)
    assert "Created" in Http.response.content.decode()
