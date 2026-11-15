"""Who may write UI components and stylesheets: a global author role only;
a role in one context writes stylesheets, in its own context only."""

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.models import Resource

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_http import make_component, make_stylesheet

HTML = "text/html"


def component(**overrides):
    return {
        "name": "todo-row",
        "engine": "django",
        "mode": "view",
        "template": "<b>{{ value }}</b>",
        **overrides,
    }


def stylesheet(**overrides):
    return {
        "name": "theme",
        "rules": [{"select": "array", "component": "todo-row"}],
        **overrides,
    }


def create(slug, body):
    When.http.post(reverse(f"{slug}-list"), data=body, format="json")


def update(url, etag, body):
    When.http.put(url, data=body, format="json", HTTP_IF_MATCH=etag)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "slug,make,changed",
    [
        ("ui-components", component, {"template": "<i>{{ value }}</i>"}),
        ("ui-stylesheets", stylesheet, {"order": 5}),
    ],
)
@test_context
def test_author_creates_updates_and_deletes_in_the_default_context(
    ui_author_login, slug, make, changed
):
    Given.http.force_authenticate(ui_author_login)
    create(slug, make())
    Then.http.status_code_is(201)
    url, etag = Http.response["Location"], Http.response["ETag"]
    update(url, etag, make(**changed))
    Then.http.status_code_is(200)
    assert Http.response.json()[next(iter(changed))] == next(iter(changed.values()))
    etag = Http.response["ETag"]
    When.http.delete(url, HTTP_IF_MATCH=etag)
    Then.http.status_code_is(204)
    When.http.get(url)
    Then.http.status_code_is(404)


@pytest.mark.django_db
@test_context
def test_author_cannot_write_outside_the_default_context(ui_author_login):
    Given.http.force_authenticate(ui_author_login)
    create("ui-components", component(rbac_context="project-a"))
    Then.http.status_code_is(403)
    create("ui-stylesheets", stylesheet(rbac_context="project-a"))
    Then.http.status_code_is(403)
    assert not Resource.objects.filter(content__name__in=["todo-row", "theme"])


@pytest.mark.django_db
@test_context
def test_context_stylesheet_role_writes_only_its_own_stylesheets(
    ui_stylesheet_author_login,
):
    Given.http.force_authenticate(ui_stylesheet_author_login)
    create("ui-stylesheets", stylesheet(rbac_context="project-a"))
    Then.http.status_code_is(201)
    url, etag = Http.response["Location"], Http.response["ETag"]
    update(url, etag, stylesheet(rbac_context="project-a", order=3))
    Then.http.status_code_is(200)
    # not into another context, nor out to the default one
    update(url, Http.response["ETag"], stylesheet(name="x"))
    Then.http.status_code_is(403)
    create("ui-stylesheets", stylesheet(name="b", rbac_context="project-b"))
    Then.http.status_code_is(403)
    create("ui-stylesheets", stylesheet(name="d"))
    Then.http.status_code_is(403)
    # never a component, in any context
    for context in ("", "project-a", "project-b"):
        create("ui-components", component(rbac_context=context))
        Then.http.status_code_is(403)
    # and not another context's stylesheet (a delete of which is a 404)
    other = make_stylesheet("elsewhere", [("array", "scrud:json")], context="project-b")
    other_url = reverse("ui-stylesheets-detail", args=[other.pk])
    update(
        other_url,
        f'"{other.etag}"',
        stylesheet(name="elsewhere", rbac_context="project-b"),
    )
    Then.http.status_code_is(403)
    When.http.delete(other_url, HTTP_IF_MATCH=f'"{other.etag}"')
    Then.http.status_code_is(404)
    assert Resource.objects.filter(pk=other.pk).exists()


@pytest.mark.django_db
@test_context
def test_context_stylesheet_role_can_delete_its_own(ui_stylesheet_author_login):
    Given.http.force_authenticate(ui_stylesheet_author_login)
    create("ui-stylesheets", stylesheet(rbac_context="project-a"))
    url, etag = Http.response["Location"], Http.response["ETag"]
    When.http.delete(url, HTTP_IF_MATCH=etag)
    Then.http.status_code_is(204)


@pytest.mark.django_db
@pytest.mark.parametrize("who", ["admin_login", "ui_author_login"])
@test_context
def test_put_moving_a_component_out_of_the_default_context(request, who):
    made = make_component("movable", "<i>{{ value }}</i>")
    user = request.getfixturevalue(who)
    Given.http.force_authenticate(user)
    url = reverse("ui-components-detail", args=[made.pk])
    body = component(name="movable", rbac_context="project-a")
    update(url, f'"{made.etag}"', body)
    # a superuser reaches the schema (400); the author is refused outright
    Then.http.status_code_is(400 if who == "admin_login" else 403)
    if who == "admin_login":
        # On main a component is authorized in main's context, "".
        assert Http.response.json()["type"].endswith("/ui-branch-context")
    made.refresh_from_db()
    assert made.content["rbac_context"] == ""


@pytest.mark.django_db
@test_context
def test_reader_without_ui_grants_sees_styled_pages_but_no_components(
    todo_list, ui_reader_login
):
    made = make_component("styled-list", '<div class="styled">{{ value.title }}</div>')
    sheet = make_stylesheet("pages", [("ex:TodoList", "styled-list")])
    Given.http.force_authenticate(ui_reader_login)
    for resource, slug in ((made, "ui-components"), (sheet, "ui-stylesheets")):
        When.http.get(
            reverse(f"{slug}-detail", args=[resource.pk]),
            HTTP_ACCEPT="application/json",
        )
        assert Http.response.status_code in (403, 404)
        assert "styled" not in Http.response.content.decode()
        When.http.get(reverse(f"{slug}-list"), HTTP_ACCEPT="application/json")
        assert Http.response.status_code in (200, 403, 404)
        assert "styled" not in Http.response.content.decode()
    When.http.get(
        reverse("todo-lists-detail", args=[todo_list.pk]),
        HTTP_ACCEPT=HTML,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(200)
    assert '<div class="styled">Groceries</div>' in Http.response.content.decode()
