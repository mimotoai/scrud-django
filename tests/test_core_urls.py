"""`absolute_reverse` reverses within the namespace the request was resolved
under (R5), falling back to the global route, then to the namespaces the
APIs noted; `api_route_name` and `api_reverse` find a type's API routes by
their bare names, then in those namespaces (follow-up ``ui-ninja-routes``)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.test import RequestFactory
from django.urls import NoReverseMatch, resolve, reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.core.urls import absolute_reverse

pytestmark = [pytest.mark.django_db, pytest.mark.urls("tests.namespaced_urls")]


def resolved_request(path):
    request = RequestFactory().get(path)
    request.resolver_match = resolve(path)
    return request


def test_namespaced_request_reverses_in_its_namespace():
    request = resolved_request("/ns/partner-profiles/a/")
    assert request.resolver_match.namespace == "ns"
    url = absolute_reverse(
        "partner-profiles-detail", kwargs={"slug": "b"}, request=request
    )
    assert url == "http://testserver/ns/partner-profiles/b/"


def test_plain_request_reverses_globally():
    request = resolved_request("/services/")
    assert request.resolver_match.namespace == ""
    url = absolute_reverse(
        "partner-profiles-detail", kwargs={"slug": "b"}, request=request
    )
    assert url == "http://testserver/partner-profiles/b/"


def test_request_without_resolver_match_reverses_globally():
    request = RequestFactory().get("/ns/partner-profiles/a/")
    assert absolute_reverse("services", request=request) == (
        "http://testserver/services/"
    )


def test_namespace_without_the_name_falls_back():
    request = resolved_request("/ns/partner-profiles/a/")
    assert absolute_reverse("services", request=request) == (
        "http://testserver/services/"
    )


def test_namespace_reverses_a_link_without_a_request_in_it():
    # A link described without a request (scrudful's HTTP Schema Resource)
    # points at the routes of the API describing it.
    url = absolute_reverse(
        "partner-profiles-detail", kwargs={"slug": "b"}, namespace="ns"
    )
    assert url == "/ns/partner-profiles/b/"


def test_namespace_without_the_name_falls_back_without_a_request():
    assert absolute_reverse("services", namespace="ns") == "/services/"


def test_the_requests_namespace_wins_over_the_given_one():
    request = resolved_request("/services/")
    # Resolved under no namespace: the given one is asked first.
    assert absolute_reverse(
        "partner-profiles-detail", kwargs={"slug": "b"}, request=request, namespace="ns"
    ) == ("http://testserver/ns/partner-profiles/b/")
    request = resolved_request("/ns/partner-profiles/a/")
    assert absolute_reverse(
        "partner-profiles-detail",
        kwargs={"slug": "b"},
        request=request,
        namespace="other",
    ) == ("http://testserver/ns/partner-profiles/b/")


def test_first_absolute_reverse_refuses_a_bare_name():
    """A bare string is one name, not a sequence of its characters
    (`types.md` S8)."""
    from scrud_django.core.urls import first_absolute_reverse

    with pytest.raises(TypeError):
        first_absolute_reverse("services")


@pytest.fixture
def api_routes(monkeypatch):
    """A registry of the API routes of its own (`note_api_routes`), so that
    what a test notes is forgotten after it, the process's restored. The
    test's URLconf is imported first, into the process's registry: its APIs
    note their routes once, when their module is first imported, and an
    import inside the test would note them in the test's registry alone,
    lost for every later test (whose links would then not follow those APIs'
    namespaces)."""
    from django.urls import get_resolver

    from scrud_django.core import urls

    get_resolver().url_patterns  # noqa: B018 (imports the URLconf)
    monkeypatch.setattr(urls, "_api_routes", {})
    return urls


def test_the_bare_name_wins_then_each_noted_namespace(api_routes):
    """`api_route_name` tries a type's bare route name first, then the name in
    each namespace an API noted (in the order noted), and takes the first the
    URLconf reverses (a detail with one key)."""
    api_routes.note_api_routes(["partner-profiles"], namespace=None)
    assert api_routes.api_namespaces() == (None,)
    assert api_routes.api_route_name("partner-profiles", "detail") == (
        "partner-profiles-detail"
    )
    # ``ns`` routes a detail of ``partner-profiles`` named so; the bare name
    # does not reverse for ``ns-only``.
    assert api_routes.api_route_name("ns-only", "detail") is None
    api_routes.note_api_routes(["partner-profiles"], namespace="absent")
    api_routes.note_api_routes(["partner-profiles", "other"], namespace="ns")
    assert api_routes.api_namespaces() == (None, "absent", "ns")
    assert api_routes.api_slugs() == ("partner-profiles", "other")
    # Or the slugs noted in one namespace (``None``: outside any).
    assert api_routes.api_slugs(namespace=None) == ("partner-profiles",)
    assert api_routes.api_slugs(namespace="ns") == ("partner-profiles", "other")
    assert api_routes.api_slugs(namespace="never-noted") == ()
    assert api_routes.api_route_name("partner-profiles", "detail") == (
        "partner-profiles-detail"
    )
    assert api_routes.api_route_name("partner-profiles", "list") == (
        "partner-profiles-list"
    )
    assert api_routes.api_reverse("partner-profiles", "detail", args=["b"]) == (
        "/partner-profiles/b/"
    )
    with pytest.raises(NoReverseMatch):
        api_routes.api_reverse("never-routed", "list")
    with pytest.raises(ValueError):
        api_routes.api_route_name("partner-profiles", "items")


@pytest.mark.urls("tests.ninja_ui_urls")
def test_a_type_routed_in_a_noted_namespace_only(api_routes):
    """On ``tests.ninja_ui_urls`` (the ninja API in ``scrud-ninja``, no DRF
    route) a type's routes are found in the namespace its API noted, and a
    link a request outside it (a UI page's, or none) asks for reverses
    there."""
    from .ninja_api import API

    # The URLconf imported (its first import notes its API's routes), then
    # the registry emptied.
    assert reverse("scrud-ninja:todo-lists-list") == "/api/todo-lists/"
    api_routes._api_routes.clear()
    api_routes.note_api_routes(["todo-lists"], namespace=None)
    assert api_routes.api_route_name("todo-lists", "list") is None
    api_routes.note_api_routes(["todo-lists"], namespace=API.urls_namespace)
    assert api_routes.api_route_name("todo-lists", "list") == (
        "scrud-ninja:todo-lists-list"
    )
    assert api_routes.api_reverse("todo-lists", "detail", args=["7"]) == (
        "/api/todo-lists/7/"
    )
    request = RequestFactory().get("/todo-lists/")
    assert absolute_reverse("todo-lists-detail", args=["7"], request=request) == (
        "http://testserver/api/todo-lists/7/"
    )
    # The API serves the UI's types too (``api(ui_types=True)``, the
    # default): their links reverse there.
    assert absolute_reverse("ui-components-list", request=request) == (
        "http://testserver/api/ui-components/"
    )
    # A type it does not route (``todo-summaries``, DRF's alone) reverses
    # nowhere.
    with pytest.raises(NoReverseMatch):
        absolute_reverse("todo-summaries-list", request=request)


@pytest.mark.urls("tests.two_ninja_apis_urls")
def test_a_link_follows_the_api_serving_the_request():
    """Of two ninja APIs, a request one serves links within it, never into
    the other (R5): a name its API lacks reverses globally, else nowhere
    (``NoReverseMatch``), though the other routes it. A request in neither
    (a UI page's, or none) links into the API that routes the name."""
    request = resolved_request("/b/todos/")
    assert request.resolver_match.namespace == "ninja-b"
    assert absolute_reverse("todos-detail", args=["7"], request=request) == (
        "http://testserver/b/todos/7/"
    )
    assert reverse("ninja-a:partner-profiles-list") == "/a/partner-profiles/"
    with pytest.raises(NoReverseMatch):
        absolute_reverse("partner-profiles-list", request=request)
    # Without a request, the namespace given is the serving API's.
    with pytest.raises(NoReverseMatch):
        absolute_reverse("partner-profiles-list", namespace="ninja-b")
    # Outside both APIs.
    page = resolved_request("/partner-profiles/")
    assert page.resolver_match.namespace == "scrud_ui"
    assert absolute_reverse("partner-profiles-list", request=page) == (
        "http://testserver/a/partner-profiles/"
    )
    assert absolute_reverse("todos-list", request=RequestFactory().get("/")) == (
        "http://testserver/b/todos/"
    )


@pytest.mark.urls("tests.two_ninja_apis_urls")
def test_a_type_route_follows_the_api_serving_the_request():
    """`api_route_name` and `api_reverse` given a request an API serves look
    in that API alone (an answer it computes, a studio computed type's,
    links to its own routes, R5); without one, or for a request outside
    every API (a UI page's), in the first noted API routing it."""
    from scrud_django.core.urls import api_reverse, api_route_name

    assert reverse("ninja-b:json-schema-list") == "/b/json-schema/"
    request = resolved_request("/b/todos/")
    assert api_route_name("json-schema", "list", request=request) == (
        "ninja-b:json-schema-list"
    )
    assert api_reverse("json-schema", "detail", args=["7"], request=request) == (
        "/b/json-schema/7/"
    )
    assert api_route_name("json-schema", "list") == "ninja-a:json-schema-list"
    page = resolved_request("/json-schema/")
    assert api_route_name("json-schema", "list", request=page) == (
        "ninja-a:json-schema-list"
    )
    # A type the serving API lacks is not looked for in another API, which
    # routes it (R5, strictly: the coordinator's fix-round ruling).
    assert api_route_name("partner-profiles", "list", request=request) is None
    with pytest.raises(NoReverseMatch):
        api_reverse("partner-profiles", "list", request=request)
    assert api_route_name("partner-profiles", "list", request=page) == (
        "ninja-a:partner-profiles-list"
    )


@pytest.mark.urls("tests.drf_and_ninja_urls")
def test_a_link_from_a_drf_route_stays_on_drf():
    """A request DRF's routes serve (outside any namespace, which DRF's
    registration notes) links within DRF alone: a type only the ninja API
    beside it routes reverses nowhere from it, through either helper, though
    a UI page, or a request not resolved, finds it there."""
    from scrud_django.core.urls import api_reverse, api_route_name

    assert reverse("ninja-beside-drf:ninja-only-things-list") == (
        "/ninja/ninja-only-things/"
    )
    drf = resolved_request("/partner-profiles/")
    assert drf.resolver_match.namespace == ""
    assert api_route_name("partner-profiles", "list", request=drf) == (
        "partner-profiles-list"
    )
    assert api_route_name("ninja-only-things", "list", request=drf) is None
    with pytest.raises(NoReverseMatch):
        api_reverse("ninja-only-things", "list", request=drf)
    with pytest.raises(NoReverseMatch):
        absolute_reverse("ninja-only-things-list", request=drf)
    # Its own routes reverse as before.
    assert absolute_reverse("partner-profiles-list", request=drf) == (
        "http://testserver/partner-profiles/"
    )
    # A studio page, and a request not resolved, fall back to the ninja API.
    page = resolved_request(reverse("scrud-studio:live-preview"))
    assert page.resolver_match.namespace == "scrud-studio"
    unresolved = RequestFactory().get("/")
    for request in (page, unresolved):
        assert api_route_name("ninja-only-things", "list", request=request) == (
            "ninja-beside-drf:ninja-only-things-list"
        )
        assert absolute_reverse("ninja-only-things-list", request=request) == (
            "http://testserver/ninja/ninja-only-things/"
        )
    # The ninja API's request: its own routes alone, DRF's not.
    ninja = resolved_request("/ninja/ninja-only-things/")
    assert ninja.resolver_match.namespace == "ninja-beside-drf"
    assert api_route_name("partner-profiles", "list", request=ninja) is None


def test_a_registry_of_its_own_loses_no_api_for_later_tests():
    """The `api_routes` tests, first in a fresh process (their URLconfs not
    imported yet), then a parity case whose links follow the serving API
    (each front end's studio links its own, so their ETags differ): the
    APIs those URLconfs build are noted in the process's registry, not in
    the tests' own, which would lose them for the parity case."""
    tests = [
        "tests/test_core_urls.py::test_the_bare_name_wins_then_each_noted_namespace",
        "tests/test_core_urls.py::test_a_type_routed_in_a_noted_namespace_only",
        "tests/test_front_end_parity.py::test_studio_computed_types_answer_alike"
        "[ui-selectable-branches-mine]",
    ]
    # On a database server, a test database of its own: this session's is
    # open, and a second session creating it would clash.
    env = dict(os.environ)
    for engine in ("PG", "MYSQL"):
        name = os.environ.get(f"SCRUD_TEST_{engine}_NAME", "scrud")
        env[f"SCRUD_TEST_{engine}_NAME"] = f"{name}_registry_of_its_own"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--create-db",
            *tests,
        ],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 passed" in result.stdout, result.stdout
