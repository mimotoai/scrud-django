"""``is_resource_view`` (and so ``embed.api_url``) decides by the route
marks (`scrud_django.core.types.mark_route`): a view marked as built for the
type's slug, not a class a view inherits."""

import pytest
from django.urls import resolve, reverse

from scrud_django import registration
from scrud_django.core.types import mark_route, route_slug
from scrud_django.ui.branches.middleware import is_resource_view
from scrud_django.ui.embed import api_url


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


@pytest.mark.drf
@pytest.mark.django_db
def test_api_url_serves_a_drf_list_a_detail_and_a_computed_detail():
    assert api_url("todo-lists") == "/todo-lists/"
    assert api_url("todo-lists", "x") == "/todo-lists/x/"
    # A computed type is a resource view too, as it was by class.
    assert api_url("summaries", "project-a") == "/summaries/project-a/"


@pytest.mark.drf
@pytest.mark.django_db
def test_a_drf_view_is_a_resource_view_of_its_own_type_only():
    match = resolve(api_url("todo-lists"))
    assert is_resource_view(match.func, "todo-lists")
    assert not is_resource_view(match.func, "summaries")


@pytest.mark.django_db
def test_a_ninja_built_view_is_a_resource_view_of_its_type(settings):
    from django.test import override_settings
    from django.urls import clear_url_caches, set_urlconf

    from .front_ends import NINJA_NAMESPACE, serve_ninja

    with override_settings(ROOT_URLCONF="tests.ninja_urls"):
        clear_url_caches()
        set_urlconf(None)
        try:
            serve_ninja()
            match = resolve(reverse(f"{NINJA_NAMESPACE}:todos-list"))
            assert is_resource_view(match.func, "todos")
            assert not is_resource_view(match.func, "notes")
        finally:
            clear_url_caches()
            set_urlconf(None)


@pytest.mark.django_db
def test_a_slug_ending_in_a_route_suffix_is_marked_with_its_own_slug():
    """``foo-detail``'s list route is ``foo-detail-list``: its slug is not
    derived from the route's name."""
    import scrud_django.ninja

    from .resource_types import TODOS_TYPE

    specs = [
        registration.json_resource_type_spec(
            **{
                **TODOS_TYPE,
                "slug": slug,
                "resource_type_uri": f"tests://{slug}",
            }
        )
        for slug in ("foo-detail", "bar-list")
    ]
    api = scrud_django.ninja.api(types=specs, urls_namespace="suffix-slugs")
    views = {p.name: p.callback for p in api.urls[0] if p.name}
    for slug in ("foo-detail", "bar-list"):
        for route in (f"{slug}-list", f"{slug}-detail"):
            assert route_slug(views[route]) == slug
            assert is_resource_view(views[route], slug)
    assert not is_resource_view(views["foo-detail-list"], "foo")
    assert not is_resource_view(views["bar-list-detail"], "bar")
    assert not is_resource_view(views["foo-detail-list"], "bar-list")
