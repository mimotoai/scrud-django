"""R3 from the server's side: a stored component's `hx-post` /
`data-scrud-json="form"` markup renders as written, and the JSON body the
client builds from its inputs is an ordinary create."""

import json
import re
from html.parser import HTMLParser

import pytest
from django.urls import reverse
from rest_framework.test import APIClient
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource

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


class _Buttons(HTMLParser):
    """The attributes of each ``<button>`` in a page, unescaped as a
    browser reads them."""

    def __init__(self):
        super().__init__()
        self.buttons = []

    def handle_starttag(self, tag, attrs):
        if tag == "button":
            self.buttons.append(dict(attrs))


@pytest.mark.django_db
@test_context
def test_fixed_body_from_rendered_markup_posts_json_with_csrf(todo_owner_login):
    """A stored Jinja component's fixed-body button (the Jinja idiom: a
    single-quoted attribute of ``tojson``) carries a body with ``"``,
    ``&`` and ``<`` that the attribute decodes to exactly; posted as the
    client posts it (the attribute's text, with the page's CSRF token as
    ``X-CSRFToken``), it is a create: refused without the token."""
    url = reverse("todo-lists-list")
    body = {
        "title": 'Tom & "Jerry" <3',
        "rbac_context": "project-a",
        "items": [{"text": "a < b && c > \"d\"", "done": False}],
    }
    make_component(
        "fixed-create",
        "{% set body = "
        + json.dumps(body)
        + " %}"
        + '<button hx-ext="scrud-json" hx-post="'
        + url
        + "\" data-scrud-json=\"fixed\" data-scrud-body='{{ body|tojson }}'>"
        "Add</button>",
        engine="jinja2",
        mode="edit",
    )
    make_stylesheet("s", [("ex:TodoList", "fixed-create")], context="project-a")
    shell = make_list("Shell")
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(todo_owner_login)
    page = client.get(reverse("todo-lists-detail", args=[shell.pk]), HTTP_ACCEPT=HTML)
    assert page.status_code == 200
    markup = page.content.decode()
    parser = _Buttons()
    parser.feed(markup)
    (button,) = [b for b in parser.buttons if b.get("data-scrud-json") == "fixed"]
    assert button["hx-post"] == url
    sent = button["data-scrud-body"]
    assert json.loads(sent) == body
    token = re.search(r'<meta name="csrf-token" content="([^"]+)">', markup).group(1)
    refused = client.post(url, data=sent, content_type="application/json")
    assert refused.status_code == 403
    assert not Resource.objects.filter(content__title=body["title"]).exists()
    created = client.post(
        url,
        data=sent,
        content_type="application/json",
        HTTP_ACCEPT="application/json",
        HTTP_X_CSRFTOKEN=token,
    )
    assert created.status_code == 201, created.content
    stored = Resource.objects.get(content__title=body["title"])
    assert stored.content == body
