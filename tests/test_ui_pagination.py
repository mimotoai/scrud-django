"""A paginated list as HTML shows the JSON list's page, count and links."""

import html as htmllib
import re

import pytest
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from .fixtures import *  # noqa
from .steps import *  # noqa: F403, F401
from .test_ui_http import frame_hrefs, make_list

HTML = "text/html"
JSON = "application/json"


def link(rel, output):
    found = re.search(rf'<a rel="{rel}" href="([^"]*)"', output)
    return htmllib.unescape(found.group(1)) if found else None


def get(url, accept, **headers):
    When.http.get(url, HTTP_ACCEPT=accept, **headers)
    Then.http.status_code_is(200)
    return Http.response


@pytest.mark.django_db
@test_context
def test_html_list_pages_match_the_json_pages(todo_list, todo_reader_login):
    make_list("Second", context="project-a")
    make_list("Hidden", context="project-b")
    Given.http.force_authenticate(todo_reader_login)
    base = reverse("todo-lists-list") + "?page_size=1"

    first_json = get(base, JSON).json()
    assert first_json["count"] == 2 and len(first_json["content"]) == 1
    assert first_json["previous"] is None and first_json["next"]
    first = get(base, HTML, HTTP_HX_REQUEST="true").content.decode()
    assert frame_hrefs(first) == [i["href"] for i in first_json["content"]]
    assert re.search(r'class="scrud-count">2<', first)
    assert link("next", first) == first_json["next"]
    assert link("prev", first) is None
    assert "Hidden" not in first

    # follow the HTML link, as a browser does: the next page, with a way back
    next_url = link("next", first)
    second_json = get(next_url, JSON).json()
    assert second_json["next"] is None and second_json["previous"]
    second = get(next_url, HTML, HTTP_HX_REQUEST="true").content.decode()
    assert frame_hrefs(second) == [i["href"] for i in second_json["content"]]
    assert frame_hrefs(second) != frame_hrefs(first)
    assert re.search(r'class="scrud-count">2<', second)
    assert link("prev", second) == second_json["previous"]
    assert link("next", second) is None
    assert "Hidden" not in second

    # the two pages together are the readable lists, once each
    both = frame_hrefs(first) + frame_hrefs(second)
    assert len(set(both)) == 2

    # the previous link leads back to the first page
    back = get(link("prev", second), HTML, HTTP_HX_REQUEST="true")
    assert frame_hrefs(back.content.decode()) == frame_hrefs(first)


@pytest.mark.django_db
@test_context
def test_html_list_page_wraps_the_collection_and_keeps_links(
    todo_list, todo_reader_login
):
    make_list("Second", context="project-a")
    Given.http.force_authenticate(todo_reader_login)
    url = reverse("todo-lists-list") + "?page_size=1"
    page = get(url, HTML).content.decode()
    assert page.lower().startswith("<!doctype html>")
    json_next = get(url, JSON).json()["next"]
    assert link("next", page) == json_next
