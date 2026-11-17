"""``is_resource_view`` (and so ``embed.api_url``) decides by the route
marks (`scrud_django.core.types.mark_route`): a view marked as built for the
type's slug, not a class a view inherits."""

import pytest
from django.urls import resolve

from scrud_django.core.types import mark_route, route_slug
from scrud_django.ui.branches.middleware import is_resource_view
from scrud_django.ui.embed import api_url

from .front_ends import api_url as front_end_api_url


def view(request):  # pragma: no cover
    return None


def test_an_unmarked_view_is_refused():
    assert not is_resource_view(view, "todos")


def test_a_view_marked_for_another_type_is_refused():
    marked = mark_route(lambda request: None, slug="notes")
    assert route_slug(marked) == "notes"
    assert is_resource_view(marked, "notes")
    assert not is_resource_view(marked, "todos")


def test_a_view_marked_without_a_slug_is_refused():
    assert not is_resource_view(mark_route(lambda request: None), "todos")


@pytest.mark.django_db
def test_api_url_serves_a_drf_list_a_detail_and_a_computed_detail():
    assert api_url("todo-lists") == "/todo-lists/"
    assert api_url("todo-lists", "x") == "/todo-lists/x/"
    # A computed type is a resource view too, as it was by class.
    assert api_url("summaries", "project-a") == "/summaries/project-a/"


@pytest.mark.django_db
def test_a_drf_view_is_a_resource_view_of_its_own_type_only():
    match = resolve(api_url("todo-lists"))
    assert is_resource_view(match.func, "todo-lists")
    assert not is_resource_view(match.func, "summaries")


@pytest.mark.django_db
def test_a_ninja_built_view_is_a_resource_view_of_its_type(front_end):
    if front_end != "ninja":
        pytest.skip("the ninja API's views")
    match = resolve(front_end_api_url(front_end, "todos-list"))
    assert is_resource_view(match.func, "todos")
    assert not is_resource_view(match.func, "notes")
