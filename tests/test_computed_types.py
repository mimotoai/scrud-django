"""Computed, read-only resource types (`scrud_django.computed`): content a
view computes for the requesting user, served with an ETag, rendered through
the same cascade as stored resources, and never writable."""

import hashlib
import json

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.registration import get_resource_type_for

from .fixtures import *  # noqa
from .fixtures import assign_role
from .steps import *  # noqa: F403, F401
from .test_ui_branch_prefixes import ADMIN, publish
from .test_ui_branch_render import may_select_branches
from .test_ui_http import HTML, assert_html, html, make_component, make_stylesheet

JSON = "application/json"
SUMMARY_TEMPLATE = "<section>{{ value.context }}:{{ value.titles|join:',' }}|{{ resource.decisions.GET }}|{{ resource.decisions.PUT }}|{{ resource.decisions.DELETE }}</section>"


@pytest.fixture(autouse=True)
def api_at_api_path(settings):
    """The API at ``/api/``, for the published prefix."""
    settings.ROOT_URLCONF = "tests.mounted_urls"


def summary_url(key="project-a"):
    return reverse("todo-summaries-detail", args=[key])


def canonical_etag(body):
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return '"' + hashlib.sha256(canonical.encode()).hexdigest() + '"'


@pytest.mark.django_db
@test_context
def test_computed_type_json_and_etag_304(todo_list, todo_reader_login):
    Given.http.force_authenticate(todo_reader_login)
    When.http.get(summary_url(), HTTP_ACCEPT=JSON)
    Then.http.status_code_is(200)
    body = Http.response.json()
    assert body == {"context": "project-a", "titles": ["Groceries"]}
    etag = Http.response["ETag"]
    assert etag == canonical_etag(body)
    When.http.get(summary_url(), HTTP_ACCEPT=JSON, HTTP_IF_NONE_MATCH=etag)
    Then.http.status_code_is(304)
    assert Http.response["ETag"] == etag and not Http.response.content
    When.http.get(summary_url(), HTTP_ACCEPT=JSON, HTTP_IF_NONE_MATCH='"other"')
    Then.http.status_code_is(200)
    When.http.get(summary_url(), HTTP_ACCEPT=JSON, HTTP_IF_NONE_MATCH="*")
    Then.http.status_code_is(304)
    # A changed content has another ETag.
    todo_list.content = {**todo_list.content, "title": "Chores"}
    todo_list.save()
    When.http.get(summary_url(), HTTP_ACCEPT=JSON, HTTP_IF_NONE_MATCH=etag)
    Then.http.status_code_is(200)
    assert Http.response["ETag"] != etag


@pytest.mark.django_db
@test_context
def test_computed_type_renders_through_cascade(todo_list, todo_reader_login):
    make_component("summary", SUMMARY_TEMPLATE)
    make_stylesheet("s", [("ex:TodoSummary", "summary")])
    Given.http.force_authenticate(todo_reader_login)
    When.http.get(summary_url(), HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    assert_html()
    # View-only decisions, whatever the user's grants.
    assert "<section>project-a:Groceries|allowed|denied|denied</section>" in html()


@pytest.mark.django_db
@test_context
def test_computed_type_refuses_writes_405(todo_list, todo_owner_login):
    Given.http.force_authenticate(todo_owner_login)
    for method in ("put", "post", "delete"):
        getattr(When.http, method)(
            summary_url(),
            data=json.dumps({"context": "x"}),
            content_type=JSON,
            HTTP_ACCEPT=JSON,
        )
        Then.http.status_code_is(405)
        assert Http.response.json()["status"] == 405
        assert "problem" in Http.response["Content-Type"]
    assert get_resource_type_for("todo-summaries").type_uri == "tests://TodoSummary"
    # No list route.
    When.http.get("/api/todo-summaries/", HTTP_ACCEPT=JSON)
    Then.http.status_code_is(404)


@pytest.mark.django_db
@test_context
def test_computed_type_shows_only_readable(todo_list, todo_reader_login):
    other = assign_role({"http.GET": ["tests://TodoList"]}, "project-b")
    Given.http.force_authenticate(todo_reader_login)
    When.http.get(summary_url(), HTTP_ACCEPT=JSON)
    seen = Http.response.json()
    Given.http.force_authenticate(other)
    When.http.get(summary_url(), HTTP_ACCEPT=JSON)
    Then.http.status_code_is(200)
    assert seen["titles"] == ["Groceries"]
    assert Http.response.json()["titles"] == []


@pytest.mark.django_db
@test_context
def test_computed_type_under_published_prefix(todo_list, todo_reader_login):
    make_component("summary", SUMMARY_TEMPLATE)
    make_stylesheet("s", [("ex:TodoSummary", "summary")])
    publish("admin", ADMIN)
    Given.http.force_authenticate(may_select_branches(todo_reader_login))
    url = f"{ADMIN}todo-summaries/project-a/"
    When.http.get(url, HTTP_ACCEPT=JSON)
    Then.http.status_code_is(200)
    assert Http.response.json() == {"context": "project-a", "titles": ["Groceries"]}
    When.http.get(url, HTTP_ACCEPT=HTML)
    Then.http.status_code_is(200)
    assert "<section>project-a:Groceries|allowed|denied|denied</section>" in html()


@pytest.mark.django_db
@test_context
def test_anonymous_refused(todo_list):
    Given.http.force_authenticate(None)
    When.http.get(summary_url(), HTTP_ACCEPT=JSON)
    assert Http.response.status_code in (401, 403)
    assert "Groceries" not in Http.response.content.decode()


@pytest.mark.django_db
@test_context
def test_computed_type_listed_in_services(todo_reader_login):
    Given.http.force_authenticate(todo_reader_login)
    When.http.get(reverse("services"), HTTP_ACCEPT=JSON)
    Then.http.status_code_is(200)
    assert Http.response.json()["tests://TodoSummary"].endswith("/api/todo-summaries/")
