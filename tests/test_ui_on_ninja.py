"""The UI app serves the types of a ninja-only project (follow-up
``ui-ninja-routes``): ``tests.ninja_ui_urls`` routes the test types' ninja
API (in its URL namespace, ``scrud-ninja``) under ``/api/`` and the UI at
``/``, with no DRF route. The UI finds a type's routes there
(`scrud_django.core.urls.api_route_name`) as it finds DRF's outside any
namespace, so each assertion shared with ``tests.mounted_urls`` (the DRF
routes under ``/api/``, the UI at ``/``) runs on both URLconfs, for the same
user, with the same status: a page's GET, its writes (and a create's
``Location`` under the UI), an embed of an API path, the API's base and its
hrefs mapped to the UI, the branch cookie's path, a published prefix's page,
a studio preview of an instance, and the UI's system checks."""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from safetydance_django.test import *  # noqa: F403, F401

from scrud_django.core.types import UNRECORDED_ROUTE
from scrud_django.core.urls import (
    api_namespaces,
    api_reverse,
    api_route_name,
)
from scrud_django.models import Resource
from scrud_django.ninja.api import ui_specs
from scrud_django.registration import ResourceRegistration
from scrud_django.ui import pages
from scrud_django.ui.branches.middleware import find_mount
from scrud_django.ui.branches.selection import (
    UI_BASE_ATTR,
    api_base,
    api_base_info,
    cookie_path,
    in_mount,
)
from scrud_django.ui.checks import (
    check_drf_only_authorization,
    check_unrouted_types,
)
from scrud_django.ui.embed import REFUSED_MESSAGE
from scrud_django.ui.embed import api_url as embed_api_url

from .fixtures import *  # noqa
from .fixtures import assign_role
from .front_ends import NINJA_NAMESPACE, serve_ninja, served_specs
from .steps import *  # noqa: F403, F401
from .test_ui_branch_render import may_select_branches
from .test_ui_http import PREFIXES, assert_html, frame_hrefs, html, put_json

DRF_UI_URLCONF = "tests.mounted_urls"
NINJA_UI_URLCONF = "tests.ninja_ui_urls"
JSON = "application/json"
ADMIN = "/admin/resources/"


@pytest.fixture(params=(DRF_UI_URLCONF, NINJA_UI_URLCONF))
def ui_urlconf(request, settings, db):
    """The URLconf a test runs on: the DRF routes or the ninja API under
    ``/api/``, the UI at ``/``. The ninja API's types are stored again (an
    earlier test's rollback may have removed the rows its import stored)."""
    settings.ROOT_URLCONF = request.param
    if request.param == NINJA_UI_URLCONF:
        serve_ninja()
    return request.param


def register(slug, content):
    """A resource of `slug` on main, stored as is (not through a route, so
    that each URLconf's test starts from the same rows)."""
    return ResourceRegistration.register(content, slug)


def make_jinja(name, template, mode="view"):
    return register(
        "ui-components",
        {
            "name": name,
            "engine": "jinja2",
            "mode": mode,
            "template": template,
            "rbac_context": "",
        },
    )


def make_stylesheet(name, rules):
    return register(
        "ui-stylesheets",
        {
            "name": name,
            "prefixes": PREFIXES,
            "rules": [{"select": s, "component": c} for s, c in rules],
            "rbac_context": "",
        },
    )


def make_list(title, items=()):
    return register(
        "todo-lists",
        {
            "title": title,
            "rbac_context": "project-a",
            "items": [{"text": text} for text in items],
        },
    )


def path_of(url):
    return urlsplit(url).path


@pytest.mark.django_db
def test_the_registry_names_each_front_ends_routes(ui_urlconf):
    """Each front end's routes are found where it named them: DRF's outside
    any namespace, the ninja API's in its ``urls_namespace``; a type the
    URLconf does not route has none."""
    assert None in api_namespaces()
    if ui_urlconf == NINJA_UI_URLCONF:
        assert NINJA_NAMESPACE in api_namespaces()
    prefix = "" if ui_urlconf == DRF_UI_URLCONF else f"{NINJA_NAMESPACE}:"
    assert api_route_name("todo-lists", "list") == f"{prefix}todo-lists-list"
    assert api_route_name("todo-lists", "detail") == f"{prefix}todo-lists-detail"
    assert api_reverse("todo-lists", "detail", args=["7"]) == "/api/todo-lists/7/"
    assert api_reverse("todo-lists", "list") == "/api/todo-lists/"
    # A computed type with a compute= has a detail route only.
    assert api_route_name("summaries", "list") is None
    assert api_reverse("summaries", "detail", args=["a"]) == "/api/summaries/a/"
    # A type routed by its viewset class alone, on DRF only.
    found = api_route_name("todo-summaries", "detail")
    assert (found is None) == (ui_urlconf == NINJA_UI_URLCONF)
    assert api_route_name("never-routed", "list") is None
    with pytest.raises(ValueError):
        api_route_name("todo-lists", "item")


@pytest.mark.django_db
@test_context
def test_a_page_is_served(ui_urlconf, todo_list, todo_owner_login):
    served = pages.served("todo-lists")
    assert served == "todo-lists" and served.resource_type.slug == "todo-lists"
    views = pages.route_views("todo-lists")
    detail = api_reverse("todo-lists", "detail", args=[todo_list.pk])
    assert views[1] is not None
    assert pages.refusal("todo-lists") is None
    Given.http.force_login(todo_owner_login)
    When.http.get(f"/todo-lists/{todo_list.pk}/")
    Then.http.status_code_is(200)
    assert_html()
    assert "Groceries" in html()
    When.http.get("/todo-lists/")
    Then.http.status_code_is(200)
    assert_html()
    # The list's item hrefs (the API's, absolute) mapped to the UI's.
    hrefs = [path_of(href) for href in frame_hrefs(html())]
    assert f"/todo-lists/{todo_list.pk}/" in hrefs
    assert not [href for href in hrefs if href.startswith("/api/")]
    # The API's own route still answers JSON.
    When.http.get(detail, HTTP_ACCEPT=JSON)
    Then.http.status_code_is(200)
    assert Http.response["Content-Type"] == JSON


@pytest.mark.django_db
@test_context
def test_a_type_the_urlconf_does_not_route_is_not_served(
    ui_urlconf, admin_login, todo_list
):
    """``todo-summaries`` is routed by a viewset class on DRF and not at all
    on ninja: the UI serves neither (the DRF route's view authorizes beyond
    the core's checks; the ninja URLconf has no route), a 404 for a
    superuser too."""
    assert pages.served("todo-summaries") is None
    Given.http.force_login(admin_login)
    When.http.get("/todo-summaries/project-a/")
    Then.http.status_code_is(404)
    assert pages.served("never-routed") is None


@pytest.mark.django_db
@test_context
def test_page_writes_answer_at_the_ui(ui_urlconf, todo_list, todo_owner_login):
    Given.http.force_login(todo_owner_login)
    body = {**todo_list.content, "title": "Groceries, again"}
    put_json(
        f"/todo-lists/{todo_list.pk}/",
        body,
        HTTP_IF_MATCH=f'"{todo_list.etag}"',
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(200)
    assert_html()
    assert "Groceries, again" in html()
    created = {"title": "Created", "rbac_context": "project-a", "items": []}
    When.http.post(
        "/todo-lists/",
        data=json.dumps(created),
        content_type=JSON,
        HTTP_HX_REQUEST="true",
    )
    Then.http.status_code_is(201)
    assert_html()
    resource = Resource.objects.get(content__title="Created")
    # The created resource's URL in the UI, not the API's.
    assert path_of(Http.response["Location"]) == f"/todo-lists/{resource.pk}/"
    assert f'data-scrud-href="{path_of(Http.response["Location"])}"' in html()


@pytest.mark.django_db
@test_context
def test_an_api_path_is_embedded(ui_urlconf, todo_list, todo_reader_login):
    other = make_list("Chores", items=["Sweep"])
    api_path = api_reverse("todo-lists", "detail", args=[other.pk])
    assert api_path == f"/api/todo-lists/{other.pk}/"
    assert embed_api_url("todo-lists", other.pk) == api_path
    assert embed_api_url("summaries", "project-a") == "/api/summaries/project-a/"
    make_jinja(
        "host", '<section class="host">{{ embed("' + api_path + '") }}</section>'
    )
    make_stylesheet("hosts", [("host ex:TodoList", "host")])
    Given.http.force_login(todo_reader_login)
    When.http.get(f"/todo-lists/{todo_list.pk}/", HTTP_SCRUD_UI_SCOPE="host")
    Then.http.status_code_is(200)
    page = html()
    assert '<section class="host">' in page
    assert REFUSED_MESSAGE not in page
    assert "Sweep" in page
    assert f"/todo-lists/{other.pk}/" in [path_of(h) for h in frame_hrefs(page)]


@pytest.mark.django_db
def test_api_hrefs_map_to_the_ui(ui_urlconf):
    assert api_base() == "/api/"
    assert api_base_info() == "/api/"
    request = RequestFactory().get("/todo-lists/")
    setattr(request, UI_BASE_ATTR, "/")
    href = "http://testserver/api/todo-lists/5/?page=2"
    assert in_mount(request, href) == "http://testserver/todo-lists/5/?page=2"
    # A request that is no UI page's keeps the API's URLs.
    assert in_mount(RequestFactory().get("/api/"), href) == href


@pytest.mark.django_db
def test_the_branch_cookie_path(ui_urlconf):
    """The cookie is scoped to the API's base at its URLs, to the UI's base
    on a UI page."""
    assert cookie_path(RequestFactory().get("/api/todo-lists/")) == "/api/"
    request = RequestFactory().get("/todo-lists/")
    setattr(request, UI_BASE_ATTR, "/")
    assert cookie_path(request) == "/"


@pytest.mark.django_db
@test_context
def test_a_published_prefix_page(ui_urlconf, todo_list, todo_reader_login):
    register(
        "ui-branches",
        {
            "name": "admin",
            "rbac_context": "project-a",
            "parent": "main",
            "prefix": ADMIN,
        },
    )
    Given.http.force_login(may_select_branches(todo_reader_login))
    When.http.get(f"{ADMIN}todo-lists/{todo_list.pk}/")
    Then.http.status_code_is(200)
    assert_html()
    assert "Groceries" in html()
    When.http.get(f"{ADMIN}todo-lists/")
    Then.http.status_code_is(200)
    hrefs = [path_of(href) for href in frame_hrefs(html())]
    # The list's item hrefs (the API's) mapped into the prefix.
    assert f"{ADMIN}todo-lists/{todo_list.pk}/" in hrefs
    # A path under the API's base is never mounted, and reads no branch.
    with CaptureQueriesContext(connection) as queries:
        assert find_mount(f"/api/todo-lists/{todo_list.pk}/") is None
    assert len(queries) == 0


@pytest.mark.django_db
@test_context
def test_a_studio_preview_of_an_instance(ui_urlconf, todo_list, todo_owner_login):
    Given.http.force_login(todo_owner_login)
    url = reverse("scrud-studio:preview", args=["todo-lists"])
    When.http.get(url, {"instance": todo_list.pk})
    Then.http.status_code_is(200)
    assert "Groceries" in html()
    # The previewed frame's href is the instance's API URL, the whole path.
    expected = api_reverse("todo-lists", "detail", args=[todo_list.pk])
    assert expected == f"/api/todo-lists/{todo_list.pk}/"
    assert frame_hrefs(html()) == [expected]


@pytest.mark.django_db
def test_the_ui_checks_name_no_type_it_serves(ui_urlconf):
    """W003 and W004 name none of the types the ninja API routes (the test
    types, the UI's and the studio's computed ones): the UI serves each, on
    either URLconf. W004 names every stored type no API routes: none on
    the DRF routes; on the ninja API, ``todo-summaries`` alone, which DRF
    alone routes (a viewset class of its own, no spec)."""
    slugs = {spec.slug for spec in served_specs()}
    assert {spec.slug for spec in ui_specs()} <= slugs
    unrouted = {w.obj for w in check_unrouted_types(None)}
    refused = {w.obj for w in check_drf_only_authorization(None)}
    assert unrouted == (
        {"todo-summaries"} if ui_urlconf == NINJA_UI_URLCONF else set()
    ), unrouted
    assert not slugs & refused, slugs & refused
    for slug in slugs:
        assert pages.refusal(slug) != UNRECORDED_ROUTE, slug


#: A fresh process's first `api_base` on ``tests.ninja_no_ui_types_urls``,
#: before anything imported the URLconf: no API has noted its routes yet.
FIRST_API_BASE = """
import django
from django.conf import settings

from tests.settings import SETTINGS

settings.configure(**{**SETTINGS, "ROOT_URLCONF": "tests.ninja_no_ui_types_urls"})
django.setup()

from scrud_django.core.urls import api_slugs
from scrud_django.ui.branches.selection import api_base

assert api_slugs() == (), api_slugs()
print("api_base", api_base())
"""


def test_the_api_base_on_a_processs_first_call():
    """`api_base` imports the URLconf (asking for the UI components' list)
    before it reads the slugs the APIs noted: on a process's first call, on
    an API not routing the UI's types, it finds another type's list. In
    in-memory SQLite (the database variables unset): the API's types are
    not stored there (`register_or_defer` leaves them pending), as nothing
    here needs them."""
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("SCRUD_TEST_")
    }
    result = subprocess.run(
        [sys.executable, "-c", FIRST_API_BASE],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[-1] == "api_base /api/", result.stdout


@pytest.mark.django_db
@pytest.mark.urls("tests.ninja_no_ui_types_urls")
def test_an_api_not_serving_the_ui_types():
    """``api(ui_types=False)`` routes none of the UI's types nor the
    studio's computed ones, only its own (the built-in ones here): the UI
    finds the API's base from another type's list."""
    from .ninja_no_ui_types_urls import API

    for spec in ui_specs():
        for kind in ("list", "detail"):
            assert api_route_name(spec.slug, kind) is None, (spec.slug, kind)
    assert api_route_name("json-schema", "list") == (
        f"{API.urls_namespace}:json-schema-list"
    )
    assert api_base() == "/api/"


VERSION_TYPE_URI = "http://api.openteams.com/json-ld/UIBranchVersion"


@pytest.mark.django_db
@pytest.mark.parametrize("who", ["anonymous", "no-roles", "reader"])
def test_a_versions_write_is_a_405_at_its_page_and_api_alike(ui_urlconf, who):
    """A POST to the UI branch versions' list, and a PUT or DELETE of a
    version, is a 405 for anyone at its UI page as at its ``/api/`` route
    (the API answers it before any authorization, DRF's and ninja's alike;
    the page refuses a method it does not serve first, `pages._allowed`),
    each naming the methods it serves."""
    from django.contrib.auth.models import User
    from django.test import Client

    from scrud_django.core.urls import api_reverse

    make_jinja("row", "<b>{{ value }}</b>")  # main's versions recorded
    version = Resource.objects.filter(resource_type__type_uri=VERSION_TYPE_URI).latest(
        "pk"
    )
    client = Client()
    if who == "no-roles":
        client.force_login(User.objects.create(username="no-roles"))
    elif who == "reader":
        client.force_login(assign_role({"http.GET": [VERSION_TYPE_URI]}, ""))
    item = api_reverse("ui-branch-versions", "detail", args=[version.pk])
    collection = api_reverse("ui-branch-versions", "list")
    for method, api in (("POST", collection), ("PUT", item), ("DELETE", item)):
        # Both URLconfs serve the API under /api/ and the UI at /.
        page = "/" + api.removeprefix("/api/")
        for url in (api, page):
            response = client.generic(
                method, url, data=json.dumps({}), content_type=JSON
            )
            assert response.status_code == 405, (method, url, response.content)
            assert response["Allow"].startswith("GET"), (method, url)
