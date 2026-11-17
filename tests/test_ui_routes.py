"""Which API routes the UI app serves pages of, and what the system checks
say of those it does not: the UI included before the API at the same URLs,
a type routed only under a URL namespace (scrud's ninja ``api()``), mixed
marked and hand-built routes, a type stored but not registered in this
process, and type slugs the UI's own routes shadow."""

from uuid import uuid4

import pytest
from django.urls import resolve, reverse
from django.utils.timezone import now
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.core.types import UNRECORDED_ROUTE
from scrud_django.models import ResourceType
from scrud_django.registration import ResourceRegistration
from scrud_django.ui import STUDIO_APP_NAME, UI_NAMESPACE, pages, reserved_slugs
from scrud_django.ui.checks import (
    DRF_ONLY_ID,
    RESERVED_SLUG_ID,
    UNROUTED_ID,
    check_drf_only_authorization,
    check_reserved_slugs,
    check_unrouted_types,
)

from .fixtures import *  # noqa
from .resource_types import (
    MARKED_DETAIL_SLUG,
    MARKED_LIST_SLUG,
    UNREGISTERED_SLUG,
    UNREGISTERED_TYPE_URI,
    UNRESOLVED_DETAIL_SLUG,
    register_mixed_types,
)
from .steps import *  # noqa: F403, F401

JSON = "application/json"
MIXED_URLCONF = "tests.mixed_routes_urls"
MIXED_SLUGS = (MARKED_LIST_SLUG, MARKED_DETAIL_SLUG, UNRESOLVED_DETAIL_SLUG)


@pytest.fixture
def mixed_urlconf(settings, db):
    """``tests.mixed_routes_urls``, its types registered again (a test's
    rollback removed the rows its import stored)."""
    settings.ROOT_URLCONF = MIXED_URLCONF
    reverse(f"{MARKED_LIST_SLUG}-list")
    register_mixed_types()


def store_unregistered_type():
    """The row of ``unregistered-notes``, stored as another process (or a
    migration) would have, without registering it in this one."""
    return ResourceType.objects.create(
        type_uri=UNREGISTERED_TYPE_URI,
        slug=UNREGISTERED_SLUG,
        revision="1",
        etag=uuid4().hex,
        modified_at=now(),
    )


def assert_no_ui_page(slug, pk):
    """The UI's pages of `slug` (its list, and `pk`'s) are the 404 of a slug
    it does not serve, showing nothing of the type."""
    for url in (f"/{slug}/", f"/{slug}/{pk}/"):
        for method in ("get", "head"):
            getattr(When.http, method)(url)
            Then.http.status_code_is(404)
            assert b"A mixed note" not in Http.response.content, url
            assert b"scrud-" not in Http.response.content, url


@pytest.mark.django_db
@test_context
def test_the_ui_included_before_the_api_does_not_shadow_it(
    settings, todo_list, todo_owner_login
):
    """``tests.ui_first_urls``: the UI's patterns come first at the API's
    URLs. Deciding whether the UI serves a type resolves its API routes,
    which reach the UI's own pattern for it: that does not recurse, and the
    UI does not serve the type there, so the API's routes answer."""
    settings.ROOT_URLCONF = "tests.ui_first_urls"
    list_url = reverse("todo-lists-list")
    detail_url = reverse("todo-lists-detail", args=[todo_list.pk])
    assert (list_url, detail_url) == ("/todo-lists/", f"/todo-lists/{todo_list.pk}/")
    assert pages.served("todo-lists") is None
    with pages.served_memo():
        assert pages.served("todo-lists") is None
        assert pages.served("todo-lists") is None
    for url in (list_url, detail_url):
        match = resolve(url)
        assert UI_NAMESPACE not in match.namespaces, url
        assert match.func is pages.route_views("todo-lists")[url == detail_url]
    Given.http.force_login(todo_owner_login)
    When.http.get(detail_url, HTTP_ACCEPT=JSON)
    Then.http.status_code_is(200)
    assert Http.response["Content-Type"] == JSON
    assert "Groceries" in Http.response.content.decode()
    When.http.get(list_url, HTTP_ACCEPT=JSON)
    Then.http.status_code_is(200)
    assert Http.response["Content-Type"] == JSON
    # The UI's own routes still answer.
    assert resolve("/studio/").namespaces == ["scrud-studio"]


@pytest.mark.django_db
def test_deciding_a_slug_resolves_its_routes_once(settings, monkeypatch, todo_list):
    settings.ROOT_URLCONF = "tests.mounted_urls"
    calls = []
    route_views = pages.route_views

    def counted(slug):
        calls.append(slug)
        return route_views(slug)

    monkeypatch.setattr(pages, "route_views", counted)
    assert pages.served("todo-lists") is not None
    assert calls == ["todo-lists"]


@pytest.mark.django_db
@test_context
def test_mixed_routes_are_refused_and_named(mixed_urlconf, admin_login):
    """A marked list with a detail routed by hand, the reverse, and a
    detail that reverses but resolves to nothing: the UI serves none of
    them (a 404, for a superuser too) and W003 names each."""
    Given.http.force_login(admin_login)
    views = {slug: pages.route_views(slug) for slug in MIXED_SLUGS}
    assert views[UNRESOLVED_DETAIL_SLUG][1] is None
    assert all(view is not None for view in views[MARKED_LIST_SLUG])
    assert all(view is not None for view in views[MARKED_DETAIL_SLUG])
    for slug in MIXED_SLUGS:
        assert pages.refusal(slug) == UNRECORDED_ROUTE, slug
        assert pages.served(slug) is None, slug
        resource = ResourceRegistration.register(
            {"text": "A mixed note", "rbac_context": "project-a"}, slug
        )
        assert_no_ui_page(slug, resource.pk)
    named = {w.obj: w for w in check_drf_only_authorization(None)}
    for slug in MIXED_SLUGS:
        assert named[slug].id == DRF_ONLY_ID
        assert f"{slug!r}" in named[slug].msg
        assert "not built by registration" in named[slug].msg


@pytest.mark.django_db
@test_context
def test_a_stored_type_routed_by_hand_without_registration_is_named(
    mixed_urlconf, admin_login
):
    """W003 names a type the UI would refuse though no registration in this
    process noted it: its routes are in the URLconf, its row is stored."""
    from scrud_django.core.types import type_slugs

    assert UNREGISTERED_SLUG not in type_slugs()
    assert UNREGISTERED_SLUG not in {w.obj for w in check_drf_only_authorization(None)}
    store_unregistered_type()
    assert pages.refusal(UNREGISTERED_SLUG) == UNRECORDED_ROUTE
    Given.http.force_login(admin_login)
    assert_no_ui_page(UNREGISTERED_SLUG, "1")
    named = [
        w for w in check_drf_only_authorization(None) if w.obj == UNREGISTERED_SLUG
    ]
    assert len(named) == 1 and named[0].id == DRF_ONLY_ID
    assert "not built by registration" in named[0].msg
    # A route named as a type's whose slug names no stored type (scoped-
    # rbac's, a project's own) is not asked about.
    assert reverse("role-list")
    assert "role" not in {w.obj for w in check_drf_only_authorization(None)}


@pytest.mark.django_db
def test_a_type_routed_only_in_a_namespace_is_named(settings, monkeypatch):
    """``tests.ninja_ui_urls``: scrud's ninja ``api()`` routes its types in
    its URL namespace, where the UI does not look for them (follow-up
    ``ui-ninja-routes``): the UI serves none of them, and W004 says so."""
    from django.core import checks

    from .front_ends import serve_ninja

    settings.ROOT_URLCONF = "tests.ninja_ui_urls"
    assert reverse("scrud-ninja:partner-profiles-list")
    # Its types stored again (an earlier test's rollback may have removed
    # the rows the API's import stored).
    serve_ninja()
    assert pages.route_views("partner-profiles") is None
    assert pages.served("partner-profiles") is None
    named = {w.obj: w for w in check_unrouted_types(None)}
    warning = named["partner-profiles"]
    assert warning.id == UNROUTED_ID
    assert "'partner-profiles-list'" in warning.msg
    assert "'partner-profiles-detail'" in warning.msg
    assert "outside a URL namespace" in warning.msg
    assert "ninja api()" in warning.hint
    # A computed type with a compute= has a detail route only: no list.
    assert "summaries" in named
    assert [w for w in checks.run_checks() if w.id == UNROUTED_ID] == list(
        named.values()
    )
    # Where the DRF routes are, unnamespaced, it is not named.
    settings.ROOT_URLCONF = "tests.mounted_urls"
    named = {w.obj for w in check_unrouted_types(None)}
    assert "partner-profiles" not in named and "todo-lists" not in named
    assert "summaries" not in named
    # Nor a type registered in this process but not stored (another test's
    # rolled back): the UI would not be asked to serve it.
    from scrud_django.core import types

    monkeypatch.setattr(types, "_TYPE_SLUGS", types._TYPE_SLUGS | {"never-stored"})
    settings.ROOT_URLCONF = "tests.ninja_ui_urls"
    assert "never-stored" not in {w.obj for w in check_unrouted_types(None)}
    # Nor when the project does not include the UI app.
    settings.ROOT_URLCONF = "tests.urls"
    assert check_unrouted_types(None) == []


@pytest.mark.django_db
def test_a_type_slug_the_ui_routes_itself_is_named(settings, monkeypatch):
    from scrud_django.core import types

    settings.ROOT_URLCONF = "tests.mounted_urls"
    assert reserved_slugs() == {"ui-css-files", "ui-js-files", "studio"}
    assert check_reserved_slugs(None) == []
    monkeypatch.setattr(
        types, "_TYPE_SLUGS", types._TYPE_SLUGS | {"studio", "ui-js-files"}
    )
    found = check_reserved_slugs(None)
    assert [w.obj for w in found] == ["studio", "ui-js-files"]
    assert {w.id for w in found} == {RESERVED_SLUG_ID}
    assert "'studio'" in found[0].msg and "ui-css-files" in found[0].hint
    # The UI's routes do answer first at the slug's list URL.
    assert resolve("/studio/").namespaces == ["scrud-studio"]
    # Without the studio app, ``studio`` is a slug like any other.
    installed = [a for a in settings.INSTALLED_APPS if a != STUDIO_APP_NAME]
    settings.INSTALLED_APPS = installed
    assert reserved_slugs() == {"ui-css-files", "ui-js-files"}
    assert [w.obj for w in check_reserved_slugs(None)] == ["ui-js-files"]
