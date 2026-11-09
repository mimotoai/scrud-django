"""Writes made over the HTML representation are refused exactly where the
JSON ones are: authorization, conditions and workflow included, and the HTML
notice never says more than the JSON problem does."""

import html as htmllib
import json
import re

import pytest
from django.urls import reverse
from django.utils.http import quote_etag
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration

from .fixtures import *  # noqa
from .fixtures import TYPE_URI_TODO_LIST, assign_role
from .steps import *  # noqa: F403, F401

HTML = "text/html"
JSON = "application/json"
FRAME = 'class="scrud-resource"'
NOTICE = re.compile(r'<div class="scrud-notice(?: [^"]*)?"[^>]*>(.*?)</div>', re.S)
TODO = {
    "title": "Buy milk",
    "status": "not started",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
}

REPRESENTATIONS = {
    "json": {"HTTP_ACCEPT": JSON},
    "html": {"HTTP_ACCEPT": HTML, "HTTP_HX_REQUEST": "true"},
}


def new_todo(**overrides):
    reverse("todos-list")
    return ResourceRegistration.register({**TODO, **overrides}, "todos")


def todo_url(todo):
    return reverse("todos-detail", args=[todo.pk])


def strings_of(value):
    """Every string in a JSON document, keys and values."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings_of(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_of(item)


def notice_texts(output):
    return [htmllib.unescape(text).strip() for text in NOTICE.findall(output)]


def stored_state(pk):
    """``(exists, content, etag)`` of the resource now."""
    row = Resource.objects.filter(pk=pk).first()
    return (False, None, None) if row is None else (True, row.content, row.etag)


def send(method, url, body, etag, representation):
    headers = {"HTTP_IF_MATCH": quote_etag(etag), **REPRESENTATIONS[representation]}
    if method == "PUT":
        When.http.put(
            url, data=json.dumps(body), content_type="application/json", **headers
        )
    else:
        When.http.delete(url, **headers)


def outcome(method, user, body, representation):
    """The status, whether the stored resource was removed, changed or
    kept, and the response text of `method` by `user` on a new to-do over
    `representation`."""
    todo = new_todo()
    before = stored_state(todo.pk)
    Given.http.force_authenticate(user)
    send(method, todo_url(todo), body, todo.etag, representation)
    after = stored_state(todo.pk)
    if not after[0]:
        effect = "removed"
    else:
        effect = "kept" if after[1:] == before[1:] else "changed"
    return Http.response.status_code, effect, Http.response.content.decode(), after[1]


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["PUT", "DELETE"])
@pytest.mark.parametrize(
    "who,status,effect",
    [
        # a superuser bypasses the workflow
        ("admin_login", {"PUT": 200, "DELETE": 204}, "ok"),
        # may only "cancel": "start" is a refused transition
        ("todo_creator_login", {"PUT": 400, "DELETE": 403}, "kept"),
        ("todo_viewer_login", {"PUT": 403, "DELETE": 403}, "kept"),
    ],
)
@test_context
def test_workflow_and_authorization_refuse_html_writes_as_json_does(
    request, who, status, effect, method
):
    user = request.getfixturevalue(who)
    body = {**TODO, "status": "in progress"}
    json_status, json_effect, json_text, json_content = outcome(
        method, user, body, "json"
    )
    html_status, html_effect, html_text, html_content = outcome(
        method, user, body, "html"
    )

    assert json_status == html_status == status[method], (json_text, html_text)
    assert json_effect == html_effect
    assert json_content == html_content
    if effect == "ok":
        assert json_effect == ("changed" if method == "PUT" else "removed")
        if method == "DELETE":
            assert html_text == ""
        else:
            assert "in progress" in html_text
        return
    assert json_effect == "kept"
    assert Http.response["Content-Type"].startswith(HTML)

    # the notice says nothing the problem does not: every part of each
    # notice ("Title: detail") is a string of the JSON problem
    problem = set(strings_of(json.loads(json_text)))
    notices = notice_texts(html_text)
    assert notices, html_text
    for notice in notices:
        for part in notice.split(": "):
            assert part in problem, (part, problem)
    if who == "todo_viewer_login":
        # the user may not PUT: not a form, none of the submitted body
        assert "<form" not in html_text and FRAME not in html_text
        assert "in progress" not in html_text


@pytest.mark.django_db
@test_context
def test_workflow_refusal_over_html_names_the_same_transition(todo_creator_login):
    """The creator may read the to-do: the HTML refusal carries the JSON
    problem's own words, and the form it returns is of the submitted body."""
    body = {**TODO, "status": "in progress"}
    _, _, json_text, _ = outcome("PUT", todo_creator_login, body, "json")
    _, _, html_text, _ = outcome("PUT", todo_creator_login, body, "html")
    detail = json.loads(json_text)["detail"]
    assert detail in " ".join(notice_texts(html_text))
    assert html_text.startswith('<form class="scrud-resource"')


WRITER_REASON = (
    "production bug: a user who may PUT but not GET a resource, refused on a"
    " condition, gets the edit form of their body at the STORED resource's"
    " ETag over HTML (renderer._refused_put decides on PUT alone, Ruling 2);"
    " the JSON refusal tells them neither"
)


def refused_writer_put(writer, pointer=None):
    todo = new_todo(title="Stored title")
    body = {**TODO, "title": "Submitted title", "status": "done"}
    Given.http.force_authenticate(writer)
    headers = {**REPRESENTATIONS["html"]}
    if pointer is not None:
        headers.update(HTTP_SCRUD_UI_POINTER=pointer, HTTP_SCRUD_UI_SCOPE="sidebar")
    When.http.put(
        todo_url(todo),
        data=json.dumps(body),
        content_type="application/json",
        HTTP_IF_MATCH=quote_etag(todo.etag),
        **headers,
    )
    Then.http.status_code_is(403)
    assert Http.response["Content-Type"].startswith(HTML)
    assert stored_state(todo.pk)[1]["title"] == "Stored title"
    return todo, Http.response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("pointer", [None, "/title"])
@test_context
def test_non_reader_gets_no_condition_message_or_stored_content(
    todo_writer_login, pointer
):
    """A user who may PUT but not GET: the condition's message is not shown
    (JSON neither), nor is the stored content."""
    todo, text = refused_writer_put(todo_writer_login, pointer)
    assert "Done is not for you" not in text
    assert "Stored title" not in text
    assert notice_texts(text)
    send("PUT", todo_url(todo), {**TODO, "status": "done"}, todo.etag, "json")
    Then.http.status_code_is(403)
    assert "Done is not for you" not in Http.response.content.decode()


@pytest.mark.django_db
@pytest.mark.xfail(strict=True, reason=WRITER_REASON)
@pytest.mark.parametrize("pointer", [None, "/title"])
@test_context
def test_non_reader_gets_no_form_and_no_stored_etag(todo_writer_login, pointer):
    todo, text = refused_writer_put(todo_writer_login, pointer)
    assert quote_etag(todo.etag) not in htmllib.unescape(text)
    assert "<form" not in text and FRAME not in text


CONDITIONAL_PUT = {
    "http.PUT": {
        TYPE_URI_TODO_LIST: {
            "condition": "size(items) < 3",
            "message": "At most 3 items",
        }
    }
}


@pytest.mark.django_db
@pytest.mark.parametrize("pointer", ["/items/7", "/nothing/here", "/items/0/text"])
@test_context
def test_refused_put_with_a_pointer_missing_from_the_body(todo_list, pointer):
    """A pointer that names no node of the submitted body is a notice, never
    a 500."""
    user = assign_role(
        {"http.GET": [TYPE_URI_TODO_LIST], **CONDITIONAL_PUT}, "project-a"
    )
    body = {
        **todo_list.content,
        "items": [{"done": False} for _ in range(3)],
    }
    Given.http.force_authenticate(user)
    When.http.put(
        reverse("todo-lists-detail", args=[todo_list.pk]),
        data=json.dumps(body),
        content_type="application/json",
        HTTP_IF_MATCH=quote_etag(todo_list.etag),
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
        HTTP_SCRUD_UI_POINTER=pointer,
    )
    Then.http.status_code_is(403)
    assert Http.response["Content-Type"].startswith(HTML)
    text = Http.response.content.decode()
    assert 'class="scrud-notice' in text
    assert "At most 3 items" in text
    assert "Buy milk" not in text
    assert stored_state(todo_list.pk)[1] == todo_list.content


# DELETE over HTML


def detail_page(user, resource, slug):
    Given.http.force_authenticate(user)
    When.http.get(
        reverse(f"{slug}-detail", args=[resource.pk]),
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(200)
    return Http.response.content.decode()


@pytest.mark.django_db
@test_context
def test_owner_deletes_over_html(todo_list, todo_owner_login):
    url = reverse("todo-lists-detail", args=[todo_list.pk])
    Given.http.force_authenticate(todo_owner_login)
    send("DELETE", url, None, todo_list.etag, "html")
    Then.http.status_code_is(204)
    assert Http.response.content == b""
    assert not Resource.objects.filter(pk=todo_list.pk).exists()
    send("DELETE", url, None, todo_list.etag, "html")
    Then.http.status_code_is(404)
    assert FRAME not in Http.response.content.decode()


@pytest.mark.django_db
@test_context
def test_reader_cannot_delete_over_html(todo_list, todo_reader_login):
    url = reverse("todo-lists-detail", args=[todo_list.pk])
    Given.http.force_authenticate(todo_reader_login)
    send("DELETE", url, None, todo_list.etag, "json")
    json_status = Http.response.status_code
    assert json_status in (403, 404)
    send("DELETE", url, None, todo_list.etag, "html")
    Then.http.status_code_is(json_status)
    text = Http.response.content.decode()
    assert Http.response["Content-Type"].startswith(HTML)
    assert FRAME not in text and "Groceries" not in text and "Buy milk" not in text
    assert 'class="scrud-notice' in text
    assert Resource.objects.filter(pk=todo_list.pk).exists()


@pytest.mark.django_db
@test_context
def test_delete_of_a_resource_the_user_cannot_read_is_the_same_over_html(todo_list):
    stranger = assign_role(
        {"http.DELETE": [TYPE_URI_TODO_LIST], "http.GET": [TYPE_URI_TODO_LIST]},
        "project-b",
    )
    url = reverse("todo-lists-detail", args=[todo_list.pk])
    Given.http.force_authenticate(stranger)
    send("DELETE", url, None, todo_list.etag, "json")
    json_status = Http.response.status_code
    assert json_status in (403, 404)
    send("DELETE", url, None, todo_list.etag, "html")
    Then.http.status_code_is(json_status)
    assert "Groceries" not in Http.response.content.decode()
    assert Resource.objects.filter(pk=todo_list.pk).exists()


@pytest.mark.django_db
@test_context
def test_delete_button_only_where_delete_is_allowed(
    todo_list,
    todo_owner_login,
    todo_reader_login,
    todo_item,
    todo_creator_login,
    admin_login,
):
    owner = detail_page(todo_owner_login, todo_list, "todo-lists")
    assert "hx-delete" in owner and "hx-put" in owner
    reader = detail_page(todo_reader_login, todo_list, "todo-lists")
    assert FRAME in reader and "Groceries" in reader
    assert "hx-delete" not in reader and "Delete" not in reader
    assert "hx-put" not in reader
    # a to-do the creator may PUT (possibly: a workflow decides) but not DELETE
    creator = detail_page(todo_creator_login, todo_item, "todos")
    assert FRAME in creator and "hx-put" in creator
    assert "hx-delete" not in creator and "Delete" not in creator
    # a superuser may do both
    admin = detail_page(admin_login, todo_item, "todos")
    assert "hx-delete" in admin
