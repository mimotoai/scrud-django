"""`render_html`, the neutral body of the DRF HTML renderer: the HTML the API
answers with ``Accept: text/html`` is what `render_html` makes of the arguments
the renderer derives, from a plain Django request."""

import json
import re
import subprocess
import sys

import pytest
from django.http import HttpRequest
from django.urls import reverse
from django.utils.http import quote_etag
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.ui import html as ui_html
from scrud_django.ui import renderer

from .fixtures import *  # noqa
from .fixtures import assign_role
from .steps import *  # noqa: F403, F401
from .test_core_imports import ROOT, SCRIPT
from .test_ui_http import (
    CONDITIONAL_PUT,
    HTML,
    TYPE_URI_TODO_LIST,
    detail_url,
    list_url,
    put_json,
    three_items,
)


@pytest.fixture
def calls(monkeypatch):
    """What the renderer called `render_html` with: ``(request, arguments)``,
    and what it returned."""
    recorded = []
    real = ui_html.render_html

    def spy(request, **arguments):
        body = real(request, **arguments)
        recorded.append((request, arguments, body))
        return body

    monkeypatch.setattr(renderer, "render_html", spy)
    return recorded


def normalized(output):
    """`output` without its one per-request value: the CSRF token of the page's
    ``<meta name="csrf-token">`` (a new masked value on every ``get_token``).
    None of these documents carries a confirmation token (no branch link) or
    a nonce, so there is nothing else to normalise."""
    return re.sub(r'(name="csrf-token"\s+content=")[A-Za-z0-9]+', r"\1T", output)


def assert_renders_as_the_renderer(calls):
    """The one `render_html` call the renderer made, made again from the
    arguments it derived, answers the same document; its request is a plain
    Django one."""
    assert len(calls) == 1
    request, arguments, body = calls[0]
    assert isinstance(request, HttpRequest)
    assert not hasattr(request, "data") and not hasattr(request, "query_params")
    again = ui_html.render_html(request, **arguments)
    assert normalized(again) == normalized(body)
    sent = normalized(Http.response.content.decode())
    assert sent == normalized(body)
    return arguments


@pytest.mark.django_db
@test_context
@pytest.mark.parametrize("fragment", [False, True])
def test_render_html_equals_the_renderer_for_an_item(
    todo_list, todo_owner_login, calls, fragment
):
    Given.http.force_authenticate(todo_owner_login)
    headers = {"HTTP_HX_REQUEST": "true"} if fragment else {}
    When.http.get(detail_url(todo_list), HTTP_ACCEPT=HTML, **headers)
    Then.http.status_code_is(200)
    arguments = assert_renders_as_the_renderer(calls)
    assert arguments["kind"] == "item"
    assert arguments["etag"] == Http.response["ETag"]
    assert "Groceries" in Http.response.content.decode()


@pytest.mark.django_db
@test_context
def test_render_html_equals_the_renderer_for_a_list(todo_list, todo_owner_login, calls):
    Given.http.force_authenticate(todo_owner_login)
    When.http.get(list_url(), HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    arguments = assert_renders_as_the_renderer(calls)
    assert arguments["kind"] == "list"
    assert "Groceries" in Http.response.content.decode()


@pytest.mark.django_db
@test_context
def test_render_html_equals_the_renderer_for_a_create(todo_owner_login, calls):
    body = {"title": "Created", "rbac_context": "project-a", "items": []}
    Given.http.force_authenticate(todo_owner_login)
    When.http.post(
        list_url(),
        data=json.dumps(body),
        content_type="application/json",
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(201)
    arguments = assert_renders_as_the_renderer(calls)
    assert arguments["kind"] == "create"
    assert arguments["location"] == Http.response["Location"]
    assert Http.response["Location"].removeprefix("http://testserver") in (
        Http.response.content.decode()
    )


@pytest.mark.django_db
@test_context
def test_render_html_equals_the_renderer_for_a_computed_type(
    todo_list, todo_reader_login, calls, settings
):
    settings.ROOT_URLCONF = "tests.mounted_urls"
    Given.http.force_authenticate(todo_reader_login)
    When.http.get(
        reverse("todo-summaries-detail", args=["project-a"]), HTTP_ACCEPT=HTML
    )
    Then.http.status_code_is(200)
    arguments = assert_renders_as_the_renderer(calls)
    assert arguments["kind"] == "computed"
    assert "Groceries" in Http.response.content.decode()


@pytest.mark.django_db
@test_context
def test_render_html_equals_the_renderer_for_a_refused_put(todo_list, calls):
    user = assign_role(
        {"http.GET": [TYPE_URI_TODO_LIST], **CONDITIONAL_PUT}, "project-a"
    )
    Given.http.force_authenticate(user)
    put_json(
        detail_url(todo_list),
        three_items(todo_list),
        HTTP_IF_MATCH=quote_etag(todo_list.etag),
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(403)
    arguments = assert_renders_as_the_renderer(calls)
    assert arguments["status"] == 403
    assert arguments["submitted"] == three_items(todo_list)
    assert arguments["stored"] is not None
    assert Http.response.content.decode().startswith('<form class="scrud-resource"')


def test_ui_html_imports_no_drf():
    script = (
        SCRIPT
        + """
import scrud_django.ui.html
import scrud_django.ui.render
"""
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
