"""Server-side embeds (`scrud_django.ui.embed`): a stored Jinja component's
``embed(path, scope, mode)`` renders another resource's fragment in process,
as the same user, on the embedding request's branch, so that every check the
resource's own URL makes applies. A refused embed renders a notice, never the
content and never a 500. These tests serve the API at ``/api/``
(``tests.mounted_urls``)."""

import logging
import re

import pytest
from django.test import RequestFactory
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django import collection_type_uri_for
from scrud_django.registration import ResourceRegistration
from scrud_django.ui import dispatch as ui_dispatch
from scrud_django.ui.branches import UI_BRANCH_TYPE_URI, middleware
from scrud_django.ui.branches.effective import (
    Draft,
    Selection,
    using_drafts,
    using_safe_mode,
    using_selection,
)
from scrud_django.ui.embed import (
    MAX_EMBED_DEPTH,
    MAX_EMBEDS,
    REFUSED_MESSAGE,
    embed_for,
)
from scrud_django.ui.studio import dispatch as studio_dispatch
from scrud_django.ui.types import UI_COMPONENT_TYPE_URI

from .fixtures import *  # noqa
from .fixtures import TYPE_URI_TODO_LIST, assign_role, assign_ui_role
from .steps import *  # noqa: F403, F401
from .test_ui_branch_prefixes import ADMIN, mounted, publish
from .test_ui_branch_render import BRANCH_HEADER, may_select_branches, put_item
from .test_ui_branch_types import make_branch
from .test_ui_branch_versions import versions_of
from .test_ui_http import HTML, html, make_component, make_stylesheet

REFUSED = (
    f'<div class="scrud-notice scrud-notice-error" role="alert">{REFUSED_MESSAGE}'
    "</div>\n"
)


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    settings.ROOT_URLCONF = "tests.mounted_urls"


def make_jinja(name, template, mode="view", context=""):
    """A stored Jinja component on main."""
    reverse("ui-components-list")
    return ResourceRegistration.register(
        {
            "name": name,
            "engine": "jinja2",
            "mode": mode,
            "template": template,
            "rbac_context": context,
        },
        "ui-components",
    )


def make_list(title, context="project-a", items=()):
    reverse("todo-lists-list")
    return ResourceRegistration.register(
        {
            "title": title,
            "rbac_context": context,
            "items": [{"text": text} for text in items],
        },
        "todo-lists",
    )


def reader_of(context):
    """A user who may only read to-do lists in `context`."""
    return assign_role(
        {
            "http.GET": [
                TYPE_URI_TODO_LIST,
                collection_type_uri_for(TYPE_URI_TODO_LIST),
            ]
        },
        context,
    )


def embed_warnings(caplog):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "scrud_django.ui.embed" and r.levelno == logging.WARNING
    ]


def url_of(resource):
    return reverse(f"{resource.resource_type.slug}-detail", args=[resource.pk])


def get_page(url, status=200, scope=None, **headers):
    if scope is not None:
        headers["HTTP_SCRUD_UI_SCOPE"] = scope
    When.http.get(url, HTTP_ACCEPT=HTML, **headers)
    Then.http.status_code_is(status)
    return html()


def host(template):
    """`template` as the Jinja component of to-do lists in scope ``host``,
    in view and in edit mode."""
    make_jinja("host", template)
    make_jinja("host-edit", template, mode="edit")
    make_stylesheet(
        "hosts", [("host ex:TodoList", "host"), ("host ex:TodoList", "host-edit")]
    )


def frames(page, url):
    """The opening tags of the resource frames of `url` in `page` (a list's
    items' frames have absolute URLs)."""
    return re.findall(
        r'<(?:form|div) class="scrud-resource" data-scrud-href="'
        rf'(?:http://testserver)?{re.escape(url)}"[^>]*>',
        page,
    )


@pytest.mark.django_db
@test_context
def test_embed_renders_resource_with_viewers_rights(
    todo_list, todo_reader_login, todo_owner_login
):
    other = make_list("Chores", items=["Sweep"])
    summary = reverse("todo-summaries-detail", args=["project-a"])
    lists = reverse("todo-lists-list")
    host(
        '<section class="host">{{ embed("' + url_of(other) + '") }}'
        '|{{ embed("' + summary + '") }}'
        '|{{ embed("' + lists + '?page=1") }}</section>'
    )
    Given.http.force_authenticate(todo_reader_login)
    page = get_page(url_of(todo_list), scope="host")
    assert '<section class="host">' in page
    # A list, with a query: its collection frame, each item in its own.
    assert f'<div class="scrud-collection-frame" data-scrud-href="{lists}"' in page
    # The embedded list, as its own fragment: its frame, its content.
    assert "Sweep" in page and len(frames(page, url_of(other))) == 2
    # A reader gets view mode: no write attributes on the embedded frames.
    assert not any("hx-put" in frame for frame in frames(page, url_of(other)))
    # A computed type is a resource view too (ruling 3).
    assert len(frames(page, summary)) == 1
    assert "Chores" in page and "Groceries" in page
    assert REFUSED_MESSAGE not in page
    # The owner may PUT: the embedded frame is the edit form, at its URL.
    Given.http.force_authenticate(todo_owner_login)
    page = get_page(url_of(todo_list), scope="host")
    assert f'hx-put="{url_of(other)}"' in frames(page, url_of(other))[0]
    # The fragment of the embedding request embeds too.
    page = get_page(url_of(todo_list), scope="host", HTTP_HX_REQUEST="true")
    assert len(frames(page, url_of(other))) == 2  # by itself, and in the list


@pytest.mark.django_db
@test_context
def test_embed_of_unreadable_resource_shows_refusal_not_content(
    todo_list, todo_reader_login
):
    secret = make_list("Secret plans", context="project-b", items=["Hidden item"])
    host('<section class="host">{{ embed("' + url_of(secret) + '") }}</section>')
    Given.http.force_authenticate(todo_reader_login)
    When.http.get(url_of(secret), HTTP_ACCEPT="application/json")
    refused_status = Http.response.status_code
    assert refused_status in (403, 404)
    page = get_page(url_of(todo_list), scope="host")
    assert "Secret plans" not in page and "Hidden item" not in page
    assert not frames(page, url_of(secret))
    # The notice the resource's own URL answers with, in place of the embed.
    embedded = re.search(r'<section class="host">(.*?)</section>', page, re.S)
    own = get_page(url_of(secret), refused_status, HTTP_HX_REQUEST="true")
    assert embedded.group(1) == own
    assert 'class="scrud-notice' in own


@pytest.mark.django_db
@test_context
def test_embed_cycle_refused(todo_list, todo_reader_login, caplog):
    # A self-embed: the resource, in the scope it is rendered in.
    make_jinja(
        "self", '<section class="self">{{ embed(resource.href, scope) }}</section>'
    )
    other = make_list("Chores", items=["Sweep"])
    # Two components embedding each other.
    make_jinja(
        "a", '<section class="a">{{ embed("' + url_of(other) + '", "b") }}</section>'
    )
    make_jinja(
        "b",
        '<section class="b">{{ embed("' + url_of(todo_list) + '", ["a"]) }}</section>',
    )
    make_stylesheet(
        "cycles",
        [
            ("self ex:TodoList", "self"),
            ("a ex:TodoList", "a"),
            ("b ex:TodoList", "b"),
        ],
    )
    Given.http.force_authenticate(todo_reader_login)
    with caplog.at_level(logging.WARNING, logger="scrud_django.ui.embed"):
        page = get_page(url_of(todo_list), scope="self")
    assert page.count('<section class="self">') == 1
    assert page.count(REFUSED) == 1
    assert '<section class="self">' + REFUSED + "</section>" in page
    assert any("cycle" in m for m in embed_warnings(caplog))
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="scrud_django.ui.embed"):
        page = get_page(url_of(todo_list), scope="a")
    # a (the page) embeds b, which embeds a again: refused there.
    assert page.count('<section class="a">') == 1
    assert page.count('<section class="b">') == 1
    assert '<section class="b">' + REFUSED + "</section>" in page
    assert page.count(REFUSED) == 1
    assert any("cycle" in m for m in embed_warnings(caplog))
    # The same resource in another scope is embedded: a cycle is a path and
    # scope already on the stack.
    page = get_page(url_of(other), scope="a")
    assert '<section class="a">' in page and '<section class="b">' in page
    assert len(frames(page, url_of(other))) == 2


@pytest.mark.django_db
@test_context
def test_embed_foreign_host_and_non_resource_path_refused(
    todo_list, todo_reader_login, caplog
):
    other = make_list("Chores", items=["Sweep"])
    refused = [
        "https://evil.example" + url_of(other),
        "//evil.example" + url_of(other),
        "http:" + url_of(other),
        "javascript:alert(1)",
        url_of(other)[1:],  # not absolute
        "",
        "/shop/todo-lists/",  # a project page, not a resource view
        reverse("scrud-studio:preview", args=["todo-lists"]),
        "/nowhere/",
        "/api/",  # the API root
        # A resource view, but not the UI's HTML: JSON, the browsable API.
        url_of(other) + "?format=json",
        url_of(other) + "?format=api",
    ]
    template = "".join(
        '<p class="e">{{ embed(' + repr(path) + ") }}</p>" for path in refused
    )
    host(template + '<p class="e">{{ embed(42) }}</p>')
    Given.http.force_authenticate(todo_reader_login)
    with caplog.at_level(logging.WARNING, logger="scrud_django.ui.embed"):
        page = get_page(url_of(todo_list), scope="host")
    assert page.count(f'<p class="e">{REFUSED}</p>') == len(refused) + 1
    assert "Sweep" not in page and "The shop's to-do lists" not in page
    assert len(embed_warnings(caplog)) == len(refused) + 1


@pytest.mark.django_db
@test_context
def test_embed_of_a_published_prefix_path(todo_list, todo_reader_login):
    other = make_list("Chores", items=["Sweep"])
    publish("admin", ADMIN)
    url = mounted(ADMIN, other)
    host('<section class="host">{{ embed("' + url + '") }}</section>')
    # The prefix's branch is readable: the embed is the API's resource,
    # with links in the prefix.
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    page = get_page(url_of(todo_list), scope="host")
    assert "Sweep" in page and len(frames(page, url)) == 1
    # A user who may not GET the prefix's branch gets check_mount's refusal.
    Given.http.force_authenticate(reader_of("project-a"))
    page = get_page(url_of(todo_list), scope="host")
    assert "Sweep" not in page and not frames(page, url)
    assert 'class="scrud-notice' in page


@pytest.mark.django_db
@test_context
def test_embed_limits(todo_list, todo_reader_login, caplog):
    # Depth: each list embeds the list its first item names.
    chain = [make_list(f"Level {n}") for n in range(MAX_EMBED_DEPTH + 2)]
    for current, following in zip(chain[:-1], chain[1:], strict=True):
        current.content["items"] = [{"text": url_of(following)}]
        current.save()
    make_jinja(
        "chain",
        '<section class="chain">{{ value.title }}'
        '{{ embed(value["items"][0].text, scope) if value["items"] }}</section>',
    )
    make_stylesheet("chains", [("chain ex:TodoList", "chain")])
    Given.http.force_authenticate(todo_reader_login)
    with caplog.at_level(logging.WARNING, logger="scrud_django.ui.embed"):
        page = get_page(url_of(chain[0]), scope="chain")
    for n in range(MAX_EMBED_DEPTH + 1):
        assert f'<section class="chain">Level {n}' in page
    assert f"Level {MAX_EMBED_DEPTH + 1}<" not in page
    assert page.count(REFUSED) == 1
    assert any("depth" in m for m in embed_warnings(caplog))
    caplog.clear()

    # Count: per top-level response, nested embeds included.
    inner = make_list("Inner")
    outer = make_list("Outer")
    make_jinja(
        "fan",
        '{% for n in range(15) %}<p class="fan">'
        '{{ embed("' + url_of(outer) + '", "out") }}</p>{% endfor %}',
    )
    make_jinja("out", '<p class="out">{{ embed("' + url_of(inner) + '") }}</p>')
    make_stylesheet(
        "fans",
        [("fan ex:TodoList", "fan"), ("out ex:TodoList", "out")],
    )
    for _ in range(2):  # the count is per response
        with caplog.at_level(logging.WARNING, logger="scrud_django.ui.embed"):
            page = get_page(url_of(todo_list), scope="fan")
        # 15 outer embeds, each embedding the inner one: 30 calls, the first
        # MAX_EMBEDS rendered (outer, inner, outer, inner, ...).
        assert len(frames(page, url_of(outer))) == MAX_EMBEDS // 2
        assert len(frames(page, url_of(inner))) == MAX_EMBEDS // 2
        assert page.count(REFUSED) == 15 - MAX_EMBEDS // 2
        assert any("count" in m for m in embed_warnings(caplog))
        caplog.clear()


@pytest.mark.django_db
@test_context
def test_embed_uses_embedding_branch(todo_list, todo_reader_login):
    other = make_list("Chores", items=["Sweep"])
    make_component("row", "<b>{{ value.text }}</b>")
    host('<section class="host">{{ embed("' + url_of(other) + '") }}</section>')
    make_stylesheet("s", [("ex:TodoItem", "row")])
    make_branch("wip", "project-a", branching_point={"main": versions_of("main")[-1]})
    put_item("ui-components", "row", "<i>{{ value.text }}</i>", "wip")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    page = get_page(url_of(todo_list), scope="host")
    assert "<b>Sweep</b>" in page and "<i>Sweep</i>" not in page
    page = get_page(url_of(todo_list), scope="host", **{BRANCH_HEADER: "wip"})
    assert "<i>Sweep</i>" in page and "<b>Sweep</b>" not in page
    # The embedded frame's own requests stay on the branch.
    assert 'data-scrud-branch="wip"' in frames(page, url_of(other))[0]
    page = get_page(url_of(todo_list), scope="host", **{BRANCH_HEADER: "wip@pinned"})
    assert 'data-scrud-branch="wip@pinned"' in frames(page, url_of(other))[0]
    assert "<i>Sweep</i>" in page


@pytest.mark.django_db
@test_context
def test_embed_in_preview_has_no_write_attributes(todo_list, todo_owner_login):
    other = make_list("Chores", items=["Sweep"])
    lists = reverse("todo-lists-list")
    host(
        '<section class="host">{{ embed("' + url_of(other) + '") }}'
        '{{ embed("' + lists + '") }}</section>'
    )
    Given.http.force_authenticate(todo_owner_login)
    # Outside a preview the owner's embeds are edit forms: the resource, and
    # it as an item of the list.
    outside = frames(get_page(url_of(todo_list), scope="host"), url_of(other))
    assert len(outside) == 2 and all("hx-put" in frame for frame in outside)
    When.http.get(
        reverse("scrud-studio:preview", args=["todo-lists"]),
        {"mode": "edit", "instance": todo_list.pk, "scope": "host"},
    )
    Then.http.status_code_is(200)
    page = html()
    assert "Sweep" in page and '<section class="host">' in page
    for attribute in ("hx-put", "hx-ext", "hx-trigger", "hx-delete"):
        assert attribute not in page
    assert "<form" not in page and ">Delete</button>" not in page
    inside = frames(page, url_of(other))
    assert len(inside) == 2
    assert all(f.startswith("<div ") and "data-scrud-branch" not in f for f in inside)


@pytest.mark.django_db
@test_context
def test_embed_in_safe_mode_stays_safe(todo_list, todo_reader_login):
    make_component("row", "<b>{{ value.text }}</b>")
    make_stylesheet("s", [("ex:TodoItem", "row")])
    request = RequestFactory().get("/somewhere/")
    request.user = todo_reader_login
    embed = embed_for(request)
    assert "<b>Buy milk</b>" in embed(url_of(todo_list))
    with using_safe_mode():
        safe = embed(url_of(todo_list))
    assert "Buy milk" in safe and "<b>Buy milk</b>" not in safe
    # Safe mode is not reset by the embedded request.
    assert "<b>Buy milk</b>" in embed(url_of(todo_list))


@pytest.mark.django_db
@test_context
def test_embed_view_mode(todo_list, todo_owner_login):
    other = make_list("Chores", items=["Sweep"])
    url = url_of(other)
    host(
        '<p class="v">{{ embed("' + url + '", mode="view") }}</p>'
        '<p class="e">{{ embed("' + url + '", "plain") }}</p>'
        '<p class="x">{{ embed("' + url + '", "other", mode="edit") }}</p>'
    )
    Given.http.force_authenticate(todo_owner_login)
    page = get_page(url_of(todo_list), scope="host")
    found = frames(page, url)
    assert len(found) == 3
    assert "hx-put" not in found[0]
    assert f'hx-put="{url}"' in found[1] and f'hx-put="{url}"' in found[2]
    # Only "view" is passed on; another mode is the user's own.
    assert 'data-scrud-scope="plain"' in found[1]


@pytest.mark.django_db
@test_context
def test_django_engine_has_no_embed(todo_list, todo_reader_login):
    other = make_list("Chores", items=["Sweep"])
    make_component("plain", '<p class="plain">[{{ embed }}]</p>')
    make_component("django-page", "<html><body>{{ body }}[{{ embed }}]</body></html>")
    make_jinja(
        "jinja-page",
        '<html><body>{{ body }}<aside>{{ embed("' + url_of(other) + '") }}</aside>'
        "</body></html>",
    )
    sheet = make_stylesheet(
        "s", [("plain ex:TodoList", "plain"), ("scrud:page", "django-page")]
    )
    Given.http.force_authenticate(todo_reader_login)
    page = get_page(url_of(todo_list), scope="plain")
    # Neither a Django node component nor a Django page component has one.
    assert '<p class="plain">[]</p>' in page
    assert page.startswith("<html><body>") and page.endswith("[]</body></html>")
    assert "Sweep" not in page
    # A Jinja page component embeds.
    sheet.content = {
        **sheet.content,
        "rules": [
            {"select": "plain ex:TodoList", "component": "plain"},
            {"select": "scrud:page", "component": "jinja-page"},
        ],
    }
    sheet.save()
    page = get_page(url_of(todo_list), scope="plain")
    aside = re.search(r"<aside>(.*?)</aside>", page, re.S).group(1)
    assert len(frames(aside, url_of(other))) == 1 and "Sweep" in aside


def test_studio_dispatch_still_works():
    """The studio's module re-exports the moved `call_view`
    (``tests/test_studio_dispatch.py`` runs unchanged against it)."""
    assert studio_dispatch.call_view is ui_dispatch.call_view


@pytest.mark.django_db
@test_context
def test_embed_of_an_api_path_under_a_published_prefix(
    todo_list, todo_reader_login, monkeypatch
):
    other = make_list("Chores", items=["Sweep"])
    make_component("title", "<h1>{{ value.title }}</h1>")
    make_stylesheet("s", [("ex:TodoList", "title")])
    host('<section class="host">{{ embed("' + url_of(other) + '") }}</section>')
    publish("admin", ADMIN)
    put_item("ui-components", "title", "<h2>{{ value.title }}</h2>", "admin")
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    # At the API's URLs, main's presentation.
    page = get_page(url_of(todo_list), scope="host")
    assert "<h1>Chores</h1>" in page and len(frames(page, url_of(other))) == 1
    # Under the prefix, the API path is embedded under it: on its branch,
    # with its links.
    page = get_page(mounted(ADMIN, todo_list), scope="host")
    assert "<h2>Chores</h2>" in page and "<h1>Chores" not in page
    assert len(frames(page, mounted(ADMIN, other))) == 1
    assert not frames(page, url_of(other))
    # check_mount gates the embed's own request.
    checked = []

    def may_get_mounted(request, mount):
        checked.append(request.path)
        return request.path != mounted(ADMIN, other)

    monkeypatch.setattr(middleware, "_may_get_mounted", may_get_mounted)
    page = get_page(mounted(ADMIN, todo_list), scope="host")
    assert mounted(ADMIN, other) in checked
    assert "Chores" not in page and not frames(page, mounted(ADMIN, other))
    assert '<section class="host"><div class="scrud-notices">' in page


@pytest.mark.django_db
@test_context
def test_embed_of_a_same_host_absolute_url(todo_list, todo_reader_login, caplog):
    other = make_list("Chores", items=["Sweep"])
    url = url_of(other)
    host(
        '<p class="a">{{ embed("http://testserver' + url + '") }}</p>'
        '<p class="a">{{ embed("https://TestServer' + url + '") }}</p>'
        '<p class="r">{{ embed("http://testserver.example' + url + '") }}</p>'
        '<p class="r">{{ embed("ftp://testserver' + url + '") }}</p>'
        '<p class="r">{{ embed("http://user@testserver' + url + '") }}</p>'
        '<p class="r">{{ embed("http:' + url + '") }}</p>'
    )
    Given.http.force_authenticate(todo_reader_login)
    with caplog.at_level(logging.WARNING, logger="scrud_django.ui.embed"):
        page = get_page(url_of(todo_list), scope="host")
    assert len(frames(page, url)) == 2
    assert page.count(f'<p class="r">{REFUSED}</p>') == 4
    assert len(embed_warnings(caplog)) == 4
    # A list item's href is absolute: its component embeds it.
    make_component("card", '<p class="card">{{ value.title }}</p>')
    make_jinja("item", "{{ embed(resource.href, 'card') }}")
    make_stylesheet(
        "cards", [("items ex:TodoList", "item"), ("card ex:TodoList", "card")]
    )
    lists = reverse("todo-lists-list")
    other.content = {**other.content, "items": [{"text": lists}]}
    other.save()
    make_jinja("list-host", "{{ embed(value['items'][0].text, 'items') }}")
    make_stylesheet("list-hosts", [("lists ex:TodoList", "list-host")])
    page = get_page(url, scope="lists")
    assert '<p class="card">Groceries</p>' in page
    assert '<p class="card">Chores</p>' in page
    assert REFUSED_MESSAGE not in page


@pytest.mark.django_db
@test_context
def test_embed_path_parsed_once(todo_list, todo_owner_login, caplog):
    other = make_list("Chores", items=["Sweep"])
    url = url_of(other)
    slug, pk = "todo-lists", other.pk
    refused = [
        f"/api/{slug}/../{slug}/{pk}/",  # not normalized: no route
        f"/api/../shop/{slug}/",
        f"/api/{slug}/{pk}%2F/",  # decoded as the server does: no route
        f"/\\evil.example/api/{slug}/{pk}/",  # a backslash is no host
        f"\\\\evil.example/api/{slug}/{pk}/",
    ]
    rendered = {
        "decoded": f"/api/{slug}%2F{pk}/",  # the server serves it decoded
        "query": url + "?ui-mode=view#top",
        "fragment": url + "#top?ui-mode=view",  # no query: the fragment's
        "space": " " + url,  # stripped, as urlsplit does
    }
    host(
        "".join(f'<p class="r">{{{{ embed({p!r}) }}}}</p>' for p in refused)
        + "".join(
            f'<p class="{name}">{{{{ embed({p!r}) }}}}</p>'
            for name, p in rendered.items()
        )
    )
    Given.http.force_authenticate(todo_owner_login)
    with caplog.at_level(logging.WARNING, logger="scrud_django.ui.embed"):
        page = get_page(url_of(todo_list), scope="host")
    assert page.count(f'<p class="r">{REFUSED}</p>') == len(refused)
    assert len(embed_warnings(caplog)) == len(refused)

    def embedded(name):
        found = re.search(rf'<p class="{name}">(.*?)</p>', page, re.S).group(1)
        [frame] = frames(found, url)
        return frame

    assert f'hx-put="{url}"' in embedded("decoded")
    # The query is dispatched; the fragment is dropped.
    assert "hx-put" not in embedded("query")
    assert f'hx-put="{url}"' in embedded("fragment")
    assert f'hx-put="{url}"' in embedded("space")


def component_frames(page):
    """The pks of the ui-components frames in `page`."""
    return {
        int(pk)
        for pk in re.findall(
            r'class="scrud-resource" data-scrud-href="(?:http://testserver)?'
            r'/api/ui-components/(\d+)/"',
            page,
        )
    }


@pytest.mark.django_db
@test_context
def test_embedded_item_list_in_safe_mode_lists_stored_items(ui_author_login):
    """Safe mode is render-only: an embedded list of UI items reads the
    stored items as its URL answers them, not the packaged layers alone."""
    stored = make_component("row", "<b>{{ value.text }}</b>")
    request = RequestFactory().get("/somewhere/")
    request.user = ui_author_login
    embed = embed_for(request)
    path = reverse("ui-components-list")
    assert stored.pk in component_frames(str(embed(path)))
    with using_safe_mode():
        safe = str(embed(path))
    assert stored.pk in component_frames(safe)


@pytest.mark.django_db
@test_context
def test_embedded_item_list_in_live_preview_lists_drafted_item():
    """A live preview's drafts are render-only: an embedded list of the
    branch's items still lists the stored item a draft replaces."""
    make_branch("wip", "project-a")
    own = put_item("ui-components", "row", "<b>{{ value.text }}</b>", "wip")
    user = assign_ui_role((UI_BRANCH_TYPE_URI, UI_COMPONENT_TYPE_URI), "project-a")
    request = RequestFactory().get("/somewhere/")
    request.user = user
    embed = embed_for(request)
    path = reverse("ui-components-list")
    draft = Draft(
        "component",
        {
            "name": "row",
            "engine": "django",
            "mode": "view",
            "template": "<i>{{ value.text }}</i>",
            "rbac_context": "project-a",
        },
    )
    with using_selection(Selection(branch="wip")):
        assert own.pk in component_frames(str(embed(path)))
        with using_drafts("wip", [draft]):
            drafted = str(embed(path))
    assert own.pk in component_frames(drafted)
