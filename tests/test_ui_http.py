"""The ``text/html`` representation of resources at their own URLs: pages,
htmx fragments and errors, rendered exactly as the viewer is authorized."""

import json
import logging
import re
from contextlib import contextmanager
from html import escape
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment

from scrud_django import collection_type_uri_for
from scrud_django.registration import ResourceRegistration
from scrud_django.scrud_signals import scrud_post_save
from scrud_django.ui import render, renderer

from .fixtures import *  # noqa
from .fixtures import (
    RESOURCE_ENDPOINT_DETAIL_NAME,
    TYPE_URI_TODO_LIST,
    assign_role,
)
from .steps import *  # noqa: F403, F401

HTML = "text/html"
HTML_CONTENT_TYPE = "text/html; charset=utf-8"
FRAME = 'class="scrud-resource"'
STALE = "This item changed since you opened it."
PREFIXES = {"ex": "http://example.org/todo#"}
CONDITIONAL_PUT = {
    "http.PUT": {
        TYPE_URI_TODO_LIST: {
            "condition": "size(items) < 3",
            "message": "At most 3 items",
        }
    }
}


def detail_url(resource):
    return reverse(f"{resource.resource_type.slug}-detail", args=[resource.pk])


def list_url():
    return reverse("todo-lists-list")


def grant(user, definition, rbac_context):
    """Also give `user` a new role with `definition` in `rbac_context`."""
    role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
    role.save()
    RoleAssignment(
        user=user,
        role=role,
        rbac_context=rbac_context,
        etag=uuid4().hex,
        modified_at=now(),
    ).save()


@contextmanager
def todo_lists_moved_after_save(rbac_context):
    """A ``scrud_post_save`` receiver (server-side code inside the write,
    after the request was authorized) that moves every saved to-do list to
    `rbac_context`, so the post-save check sees it there."""

    def move(sender, instance, resource_type_uri=None, **kwargs):
        if resource_type_uri != TYPE_URI_TODO_LIST:
            return
        instance.content = {**instance.content, "rbac_context": rbac_context}
        type(instance).objects.filter(pk=instance.pk).update(content=instance.content)

    scrud_post_save.connect(move, weak=False, dispatch_uid="test-ui-move-after-save")
    try:
        yield
    finally:
        scrud_post_save.disconnect(dispatch_uid="test-ui-move-after-save")


def make_component(name, template, mode="view"):
    reverse("ui-components-list")
    return ResourceRegistration.register(
        {
            "name": name,
            "engine": "django",
            "mode": mode,
            "template": template,
            "rbac_context": "",
        },
        "ui-components",
    )


def make_stylesheet(name, rules, context=""):
    reverse("ui-stylesheets-list")
    return ResourceRegistration.register(
        {
            "name": name,
            "prefixes": PREFIXES,
            "rules": [{"select": s, "component": c} for s, c in rules],
            "rbac_context": context,
        },
        "ui-stylesheets",
    )


def make_list(title, context="project-a"):
    reverse("todo-lists-list")
    return ResourceRegistration.register(
        {"title": title, "rbac_context": context, "items": [{"text": title}]},
        "todo-lists",
    )


def html():
    return Http.response.content.decode()


def without_content(output):
    """`output` without its ``scrud-content`` script elements."""
    return re.sub(
        r'<script type="application/json" class="scrud-content">.*?</script>',
        "",
        output,
        flags=re.S,
    )


def frame_hrefs(output):
    return re.findall(r'class="scrud-resource" data-scrud-href="([^"]*)"', output)


def put_json(url, body, **headers):
    When.http.put(
        url, data=json.dumps(body), content_type="application/json", **headers
    )


def etag_attribute(etag):
    return f'data-scrud-etag="{escape(etag)}"'


def assert_not_cached():
    """M-1: HTML depends on more than the resource: always revalidated."""
    cache_control = {
        v.strip() for v in Http.response.get("Cache-Control", "").split(",")
    }
    assert {"private", "no-cache"} <= cache_control, cache_control


def assert_html():
    assert Http.response["Content-Type"] == HTML_CONTENT_TYPE, Http.response[
        "Content-Type"
    ]


def three_items(todo_list):
    return {
        **todo_list.content,
        "items": [{"text": f"Item {n}", "done": False} for n in range(3)],
    }


@pytest.mark.django_db
@test_context
def test_html_detail_page_and_fragment(todo_list, todo_owner_login):
    url = detail_url(todo_list)
    Given.http.force_authenticate(todo_owner_login)
    When.http.get(url, HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    assert_html()
    page = html()
    assert page.lower().startswith("<!doctype html>")
    assert '<meta name="csrf-token" content="' in page
    assert "/static/scrud_ui/htmx.min.js" in page
    assert "/static/scrud_ui/scrud-ui.js" in page
    assert "<title>To-do list</title>" in page
    assert "Buy milk" in page and FRAME in page
    vary = {v.strip().lower() for v in Http.response["Vary"].split(",")}
    assert {"accept", "hx-request", "scrud-ui-scope", "scrud-ui-pointer"} <= vary
    assert_not_cached()

    When.http.get(url, HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true")
    Then.http.status_code_is(200)
    assert_html()
    fragment = html()
    assert "<!doctype" not in fragment.lower()
    assert fragment.startswith('<form class="scrud-resource"')
    assert etag_attribute(Http.response["ETag"]) in fragment


@pytest.mark.django_db
@test_context
def test_json_unchanged_and_browsable_api_reachable(todo_list, todo_owner_login):
    url = detail_url(todo_list)
    Given.http.force_authenticate(todo_owner_login)
    When.http.get(url, HTTP_ACCEPT="application/json")
    Then.http.status_code_is(200)
    assert Http.response["Content-Type"] == "application/json"
    assert Http.response.json() == todo_list.content
    assert "no-cache" not in Http.response.get("Cache-Control", "")
    When.http.get(url + "?format=api", HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    assert "Django REST framework" in html()
    assert FRAME not in html()


@pytest.mark.django_db
@test_context
def test_scope_header_selects_component(todo_list, todo_reader_login):
    make_component("sidebar-list", '<nav class="sidebar">{{ value.title }}</nav>')
    make_stylesheet("s", [("sidebar ex:TodoList", "sidebar-list")])
    url = detail_url(todo_list)
    Given.http.force_authenticate(todo_reader_login)
    When.http.get(
        url, HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true", HTTP_SCRUD_UI_SCOPE="sidebar"
    )
    Then.http.status_code_is(200)
    assert '<nav class="sidebar">Groceries</nav>' in html()
    assert 'data-scrud-scope="sidebar"' in html()
    When.http.get(url, HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true")
    assert "sidebar" not in html()


@pytest.mark.django_db
@test_context
def test_invalid_scope_header_is_ignored(todo_list, todo_reader_login):
    """Ruling 3: a scope chain that is not up to 16 names is treated as
    empty."""
    make_component("sidebar-list", '<nav class="sidebar">{{ value.title }}</nav>')
    make_stylesheet("s", [("sidebar ex:TodoList", "sidebar-list")])
    url = detail_url(todo_list)
    Given.http.force_authenticate(todo_reader_login)
    for scope in ("sidebar 1bad", "sidebar <x>", " ".join(["sidebar"] * 17)):
        When.http.get(
            url, HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true", HTTP_SCRUD_UI_SCOPE=scope
        )
        Then.http.status_code_is(200)
        assert 'data-scrud-scope=""' in html(), scope
        assert "sidebar" not in html(), scope
    When.http.get(
        url,
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
        HTTP_SCRUD_UI_SCOPE=" ".join(["sidebar"] * 16),
    )
    assert '<nav class="sidebar">Groceries</nav>' in html()


@pytest.mark.django_db
@test_context
def test_pointer_header_renders_node(todo_list, todo_owner_login):
    url = detail_url(todo_list)
    Given.http.force_authenticate(todo_owner_login)
    When.http.get(
        url, HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true", HTTP_SCRUD_UI_POINTER="/items/1"
    )
    Then.http.status_code_is(200)
    output = html()
    assert 'data-scrud-pointer="/items/1"' in output
    assert "Buy eggs" in output and "Buy milk" not in without_content(output)
    # a node frame is no form (ruling 5): it may sit inside its root's form
    assert output.startswith('<div class="scrud-resource"')
    assert "<form" not in output
    assert f'hx-put="{url}"' in output


@pytest.mark.django_db
@test_context
def test_invalid_pointer_header_is_400(todo_list, todo_owner_login):
    """Ruling 3: a pointer that is not RFC 6901, or longer than 1024
    characters, is a 400 notice, and a PUT carrying it writes nothing."""
    url = detail_url(todo_list)
    Given.http.force_authenticate(todo_owner_login)
    for pointer in ("items/1", "/items/~2", "/" + "a" * 1024):
        When.http.get(
            url, HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true", HTTP_SCRUD_UI_POINTER=pointer
        )
        Then.http.status_code_is(400)
        assert_html()
        assert "Buy milk" not in html() and FRAME not in html()
        assert 'class="scrud-notice' in html()
    When.http.get(
        url, HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true", HTTP_SCRUD_UI_POINTER="/a~0~1"
    )
    Then.http.status_code_is(200)
    # JSON ignores the header
    When.http.get(url, HTTP_ACCEPT="application/json", HTTP_SCRUD_UI_POINTER="bad")
    Then.http.status_code_is(200)

    body = {**todo_list.content, "title": "Renamed"}
    put_json(
        url,
        body,
        HTTP_IF_MATCH=f'"{todo_list.etag}"',
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
        HTTP_SCRUD_UI_POINTER="bad",
    )
    Then.http.status_code_is(400)
    assert "<form" not in html()
    todo_list.refresh_from_db()
    assert todo_list.content["title"] == "Groceries"


@pytest.mark.django_db
@test_context
def test_put_json_returns_html_fragment(todo_list, todo_owner_login):
    url = detail_url(todo_list)
    body = {**todo_list.content, "title": "Weekend shopping"}
    Given.http.force_authenticate(todo_owner_login)
    put_json(
        url,
        body,
        HTTP_IF_MATCH=f'"{todo_list.etag}"',
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(200)
    assert_html()
    output = html()
    assert "Weekend shopping" in without_content(output)
    etag = Http.response["ETag"]
    assert etag != f'"{todo_list.etag}"'
    assert etag_attribute(etag) in output


@pytest.mark.django_db
@test_context
def test_put_refused_shows_messages_html_only(todo_list):
    user = assign_role(
        {"http.GET": [TYPE_URI_TODO_LIST], **CONDITIONAL_PUT}, "project-a"
    )
    url = detail_url(todo_list)
    body = three_items(todo_list)
    if_match = f'"{todo_list.etag}"'
    Given.http.force_authenticate(user)
    put_json(
        url, body, HTTP_IF_MATCH=if_match, HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true"
    )
    Then.http.status_code_is(403)
    assert_html()
    output = html()
    assert "At most 3 items" in output
    # the submitted body comes back as the form, at the ETag it was sent with
    assert output.startswith('<form class="scrud-resource"')
    assert "Item 2" in without_content(output)
    assert etag_attribute(if_match) in output

    # M-2: the form writes at the stored resource's ETag, never at the
    # If-Match sent ("*" would defeat lost-update protection)
    put_json(url, body, HTTP_IF_MATCH="*", HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true")
    Then.http.status_code_is(403)
    assert etag_attribute(quote_etag(todo_list.etag)) in html()
    assert 'data-scrud-etag="*"' not in html()

    put_json(url, body, HTTP_IF_MATCH=if_match, HTTP_ACCEPT="application/json")
    Then.http.status_code_is(403)
    assert Http.response["Content-Type"] == "application/problem+json"
    assert "At most 3 items" not in Http.response.content.decode()


@pytest.mark.django_db
@test_context
def test_put_invalid_html_400_rerenders_form(todo_list, todo_owner_login):
    url = detail_url(todo_list)
    body = {**todo_list.content, "items": [{"text": 5, "done": False}]}
    if_match = f'"{todo_list.etag}"'
    Given.http.force_authenticate(todo_owner_login)
    put_json(
        url, body, HTTP_IF_MATCH=if_match, HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true"
    )
    Then.http.status_code_is(400)
    assert_html()
    output = html()
    assert output.startswith('<form class="scrud-resource"')
    assert etag_attribute(if_match) in output
    assert "must be string" in output


@pytest.mark.django_db
@test_context
def test_crafted_put_by_reader_gets_no_form(todo_list, todo_reader_login):
    """Ruling 2: a user who may not PUT the stored resource gets a notice,
    never an edit form of what they sent."""
    url = detail_url(todo_list)
    body = {**todo_list.content, "title": "Crafted title"}
    Given.http.force_authenticate(todo_reader_login)
    for headers in ({"HTTP_HX_REQUEST": "true"}, {}):
        put_json(
            url,
            body,
            HTTP_IF_MATCH=f'"{todo_list.etag}"',
            HTTP_ACCEPT=HTML,
            **headers,
        )
        Then.http.status_code_is(403)
        assert_html()
        output = html()
        assert "Crafted title" not in output
        assert "<form" not in output and "hx-put" not in output
        assert "<input" not in output and FRAME not in output
        assert 'class="scrud-notice' in output


@pytest.mark.django_db
@test_context
def test_put_stale_etag_html_412(todo_list, todo_owner_login):
    url = detail_url(todo_list)
    body = {**todo_list.content, "title": "Too late"}
    Given.http.force_authenticate(todo_owner_login)
    put_json(
        url,
        body,
        HTTP_IF_MATCH='"stale"',
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
        HTTP_SCRUD_UI_SCOPE="sidebar",
        HTTP_SCRUD_UI_POINTER="/items/0",
    )
    Then.http.status_code_is(412)
    assert_html()
    output = html()
    assert STALE in output
    assert f'hx-get="{url}"' in output
    assert 'hx-trigger="load"' in output
    headers = re.search(r'hx-headers="([^"]*)"', output).group(1)
    assert json.loads(headers.replace("&quot;", '"')) == {
        "Scrud-UI-Scope": "sidebar",
        "Scrud-UI-Pointer": "/items/0",
    }
    assert "Too late" not in output and "Buy milk" not in output


@pytest.mark.django_db
@test_context
def test_list_html(todo_list):
    """Ruling 1: a reader of only project-a sees exactly the JSON list's
    items."""
    make_list("Other project", context="project-b")
    make_list("Second", context="project-a")
    reader = assign_role(
        {
            "http.GET": [
                TYPE_URI_TODO_LIST,
                collection_type_uri_for(TYPE_URI_TODO_LIST),
            ]
        },
        "project-a",
    )
    Given.http.force_authenticate(reader)
    When.http.get(list_url(), HTTP_ACCEPT="application/json")
    Then.http.status_code_is(200)
    expected = [item["href"] for item in Http.response.json()["content"]]
    assert len(expected) == 2
    When.http.get(list_url(), HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true")
    Then.http.status_code_is(200)
    assert_html()
    output = html()
    assert frame_hrefs(output) == expected
    assert output.count(FRAME) == 2
    assert "Other project" not in output
    When.http.get(list_url(), HTTP_ACCEPT=HTML)
    assert html().lower().startswith("<!doctype html>")
    assert frame_hrefs(html()) == expected


@pytest.mark.django_db
@test_context
def test_unauthorized_html_has_no_content(
    todo_list, partner_profiles, login_other_context
):
    """Review Focus 5: an anonymous or unauthorized HTML request gets the
    JSON status, as text/html, with a notice and none of the content."""
    url = detail_url(todo_list)
    When.http.get(url, HTTP_ACCEPT="application/json")
    json_status = Http.response.status_code
    assert json_status in (401, 403)
    for headers in ({}, {"HTTP_HX_REQUEST": "true"}):
        When.http.get(url, HTTP_ACCEPT=HTML, **headers)
        Then.http.status_code_is(json_status)
        assert_html()
        assert "Groceries" not in html() and "Buy milk" not in html()
        assert FRAME not in html()
        assert 'class="scrud-notice' in html()

    profile_url = reverse(
        RESOURCE_ENDPOINT_DETAIL_NAME, kwargs={"slug": partner_profiles.id}
    )
    Given.http.force_authenticate(login_other_context)
    When.http.get(profile_url, HTTP_ACCEPT=HTML)
    Then.http.status_code_is(404)
    assert_html()
    assert FRAME not in html()
    for value in partner_profiles.content.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in html()


@pytest.mark.django_db
@test_context
def test_error_content_type_is_html(todo_list, todo_owner_login):
    """Ruling 6: whatever error handler answers, an HTML request gets
    text/html."""
    url = detail_url(todo_list)
    Given.http.force_authenticate(todo_owner_login)
    # 405 (scoped-rbac's handler)
    When.http.post(url, data={}, format="json", HTTP_ACCEPT=HTML)
    Then.http.status_code_is(405)
    assert_html()
    assert_not_cached()
    # 428 (scrudful's own)
    put_json(url, todo_list.content, HTTP_ACCEPT=HTML)
    Then.http.status_code_is(428)
    assert_html()
    assert 'class="scrud-notice' in html()
    # a scrud error (scrud's handler)
    When.http.get(url, HTTP_ACCEPT=HTML, HTTP_SCRUD_UI_POINTER="bad")
    Then.http.status_code_is(400)
    assert_html()


@pytest.mark.django_db
@test_context
def test_create_html(todo_owner_login):
    reverse("todo-lists-list")
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
    assert_html()
    location = Http.response["Location"]
    assert f'data-scrud-href="{location.removeprefix("http://testserver")}"' in html()
    assert etag_attribute(Http.response["ETag"]) in html()
    assert "Created" in without_content(html())


@pytest.mark.django_db
@test_context
def test_without_ui_app_html_is_browsable_api(todo_list, todo_owner_login):
    url = detail_url(todo_list)
    installed = [app for app in settings.INSTALLED_APPS if app != "scrud_django.ui"]
    Given.http.force_authenticate(todo_owner_login)
    with override_settings(INSTALLED_APPS=installed):
        When.http.get(url, HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    assert "Django REST framework" in html()
    assert FRAME not in html()


@pytest.mark.django_db
@test_context
def test_page_component_resolves_from_stylesheet(todo_list, todo_reader_login):
    make_component(
        "my-page", '<html><title>{{ title }}</title><body id="mine">{{ body }}</body>'
    )
    make_stylesheet("pages", [("scrud:page", "my-page")])
    url = detail_url(todo_list)
    Given.http.force_authenticate(todo_reader_login)
    When.http.get(url, HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    assert html().startswith("<html><title>To-do list</title><body id=\"mine\">")
    assert "Buy milk" in html()
    # a fragment is not wrapped
    When.http.get(url, HTTP_ACCEPT=HTML, HTTP_HX_REQUEST="true")
    assert 'id="mine"' not in html()


@pytest.mark.django_db
@test_context
def test_failing_page_component_falls_back(todo_list, todo_reader_login):
    make_component("bad-page", '{{ body }}{% url "no-such-route" %}')
    make_stylesheet("pages", [("scrud:page", "bad-page")])
    url = detail_url(todo_list)
    Given.http.force_authenticate(todo_reader_login)
    When.http.get(url, HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    assert html().startswith('<!-- scrud-ui: component "bad-page" failed -->')
    assert "<title>To-do list</title>" in html()
    assert "Buy milk" in html()


@pytest.mark.django_db
@test_context
def test_refused_put_uses_stored_contexts_and_decisions(todo_list):
    """I-1: a refused PUT's body naming another context neither applies that
    context's stylesheets nor gains what the user may do there."""
    make_component("list-a", '<div class="styled-a">{{ value.title }}</div>', "edit")
    make_component("list-b", '<div class="styled-b">{{ value.title }}</div>', "edit")
    make_stylesheet("in-a", [("ex:TodoList", "list-a")], context="project-a")
    make_stylesheet("in-b", [("ex:TodoList", "list-b")], context="project-b")
    # project-a: read and write; project-b: delete only
    user = assign_role(
        {"http.GET": [TYPE_URI_TODO_LIST], "http.PUT": [TYPE_URI_TODO_LIST]},
        "project-a",
    )
    grant(user, {"http.DELETE": [TYPE_URI_TODO_LIST]}, "project-b")
    url = detail_url(todo_list)
    body = {**todo_list.content, "title": "Moved", "rbac_context": "project-b"}
    Given.http.force_authenticate(user)
    put_json(
        url,
        body,
        HTTP_IF_MATCH=quote_etag(todo_list.etag),
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(403)
    output = html()
    assert output.startswith('<form class="scrud-resource"')
    assert '<div class="styled-a">Moved</div>' in output
    assert "styled-b" not in output
    assert "hx-delete" not in output


@pytest.mark.django_db
@test_context
def test_post_save_refusal_rerenders_with_stored_state(todo_list, todo_owner_login):
    """M-5: refused by the post-save check (the saved list landed where the
    owner may not write), the form is decided on the resource as stored
    before the write, and nothing is written."""
    url = detail_url(todo_list)
    stored_etag = quote_etag(todo_list.etag)
    body = {**todo_list.content, "title": "Renamed"}
    Given.http.force_authenticate(todo_owner_login)
    with todo_lists_moved_after_save("project-b"):
        put_json(
            url,
            body,
            HTTP_IF_MATCH=stored_etag,
            HTTP_ACCEPT=HTML,
            HTTP_HX_REQUEST="true",
        )
    Then.http.status_code_is(403)
    output = html()
    # decided in project-a, as stored: the owner may PUT and DELETE there
    # (in project-b, where the write landed, nothing)
    assert output.startswith('<form class="scrud-resource"')
    assert "hx-delete" in output
    assert etag_attribute(stored_etag) in output
    assert "Renamed" in without_content(output)
    todo_list.refresh_from_db()
    assert todo_list.content["title"] == "Groceries"
    assert todo_list.content["rbac_context"] == "project-a"
    assert quote_etag(todo_list.etag) == stored_etag


def test_stored_is_kept_once():
    """M-5: a later lookup in the same request does not replace the stored
    resource the first one kept."""
    view = SimpleNamespace()
    request = SimpleNamespace(method="PUT")
    renderer.note_negotiation(request, renderer.ScrudHTMLRenderer())
    first = SimpleNamespace(content={"title": "before"}, etag="one")
    renderer.remember_stored(view, request, first)
    first.content["title"] = "changed by the write"
    renderer.remember_stored(
        view, request, SimpleNamespace(content={"title": "after"}, etag="two")
    )
    stored = view.__dict__["_scrud_ui_stored"]
    assert stored.content == {"title": "before"}
    assert stored.etag == '"one"'


@pytest.mark.django_db
@pytest.mark.parametrize("fragment", [True, False])
@test_context
def test_render_failure_is_a_notice_without_content(
    todo_list, todo_owner_login, monkeypatch, caplog, fragment
):
    """M-3: an unexpected error in the renderer keeps the status and shows a
    notice, never content or a traceback."""

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(render, "decisions_for", broken)
    headers = {"HTTP_HX_REQUEST": "true"} if fragment else {}
    Given.http.force_authenticate(todo_owner_login)
    with caplog.at_level(logging.ERROR, logger="scrud_django.ui.renderer"):
        When.http.get(detail_url(todo_list), HTTP_ACCEPT=HTML, **headers)
    Then.http.status_code_is(200)
    assert_html()
    output = html()
    assert "Something went wrong rendering this page." in output
    assert "Groceries" not in output and "Buy milk" not in output
    assert "boom" not in output and "Traceback" not in output
    assert any(r.exc_info for r in caplog.records)


@pytest.mark.django_db
@pytest.mark.parametrize("error", ["DatabaseError", "TransactionManagementError"])
@test_context
def test_refused_put_stored_lookup_failure_is_notice(todo_list, monkeypatch, error):
    """A refused PUT whose stored resource cannot be looked up (a database
    error) answers with the notices only, keeping its status."""
    from django.db import DatabaseError
    from django.db.transaction import TransactionManagementError

    from scrud_django.mixins.view_mixins import ResourceViewMixin

    errors = {
        "DatabaseError": DatabaseError,
        "TransactionManagementError": TransactionManagementError,
    }

    def failing(self):
        raise errors[error]("lookup failed")

    user = assign_role(
        {"http.GET": [TYPE_URI_TODO_LIST], **CONDITIONAL_PUT}, "project-a"
    )
    monkeypatch.setattr(ResourceViewMixin, "get_object", failing)
    Given.http.force_authenticate(user)
    put_json(
        detail_url(todo_list),
        three_items(todo_list),
        HTTP_IF_MATCH=f'"{todo_list.etag}"',
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(403)
    assert_html()
    output = html()
    # the problem's notice only: the condition's message needs the stored
    # resource
    assert 'class="scrud-notice scrud-notice-error"' in output
    assert "Forbidden" in output
    assert "<form" not in output and FRAME not in output
    assert "Item 2" not in output and "lookup failed" not in output
