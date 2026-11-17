"""``scrud_django.urls`` is the ninja API (0.17.0): under ``/api/``
(`tests.default_urls`) its services, built-in types, UI types, roles and
scoped-rbac's semantic documents answer from ninja's routes, in the
namespace ``scrud``, while ``scrud_django.drf.urls``
(`tests.default_drf_urls`) answers as DRF. Two scrud APIs in one namespace are a system check warning
(``scrud_django.W008``)."""

from urllib.parse import urlsplit

import pytest
from django.contrib.auth.models import User
from django.core import checks
from django.urls import resolve

from scrud_django.checks import check_api_namespaces

from .front_ends import split_links

JSON = "application/json"


def _links(response):
    """`response`'s Link targets, by relation."""
    links = {}
    for link in split_links(response["Link"]):
        rel = link.split('rel="', 1)[1].split('"', 1)[0]
        links[rel] = link[1 : link.index(">")]
    return links


def assert_ninja_route(path):
    """`path` resolves to a route of scrud's ninja API: in ninja's
    application namespace, under ``scrud_django.urls``'s namespace
    ``scrud``."""
    match = resolve(path)
    assert match.app_names == ["ninja"], (path, match)
    assert match.namespaces == ["scrud"], (path, match)
    assert not hasattr(match.func, "cls"), (path, match)


def assert_drf_route(path):
    """`path` resolves to a DRF view (one ``as_view()`` built, outside any
    namespace)."""
    match = resolve(path)
    assert match.namespaces == [], (path, match)
    assert hasattr(match.func, "cls"), (path, match)


@pytest.fixture
def admin(client):
    client.force_login(
        User.objects.create(username="admin", is_staff=True, is_superuser=True)
    )
    return client


@pytest.mark.django_db
@pytest.mark.urls("tests.default_urls")
@pytest.mark.parametrize(
    "path",
    [
        "/api/services/",
        "/api/json-schema/",
        "/api/json-ld/",
        "/api/indexing-policies/",
        "/api/workflow-policies/",
        "/api/ui-components/",
        "/api/ui-stylesheets/",
        "/api/ui-branches/",
        "/api/roles/",
        "/api/role-assignments/",
        "/api/user-rbac-policy/",
    ],
)
def test_scrud_django_urls_serves_ninja(admin, path):
    assert_ninja_route(path)
    response = admin.get(path)
    assert response.status_code == 200, (path, response.content)
    # ninja answers an HTML Accept with JSON (no browsable API).
    response = admin.get(path, HTTP_ACCEPT="text/html")
    assert response.status_code == 200, (path, response.content)
    assert response["Content-Type"].startswith(JSON), (path, response)


@pytest.mark.django_db
@pytest.mark.urls("tests.default_urls")
def test_scrud_django_urls_serves_the_semantic_documents_roles_link_to(admin):
    """``scrud_django.urls`` includes ``scoped_rbac.semantic_urls``: the
    schema and context the ninja role list links to are served, under
    ``/api/semantic/`` (follow-up ``ninja-semantic-urls-include``)."""
    response = admin.get("/api/roles/")
    assert response.status_code == 200, response.content
    links = _links(response)
    schema = links["describedby"]
    context = links["http://www.w3.org/ns/json-ld#context"]
    for url in (schema, context):
        path = urlsplit(url).path
        assert path.startswith("/api/semantic/"), url
        assert resolve(path).url_name.startswith("scoped-rbac-"), url
        document = admin.get(url)
        assert document.status_code == 200, (url, document.content)


@pytest.mark.django_db
@pytest.mark.urls("tests.default_drf_urls")
@pytest.mark.parametrize(
    "path", ["/api/services/", "/api/json-schema/", "/api/ui-components/"]
)
def test_scrud_django_drf_urls_serves_drf(admin, path):
    assert_drf_route(path)
    response = admin.get(path, HTTP_ACCEPT="text/html")
    assert response.status_code == 200, (path, response.content)
    # DRF's browsable API answers an HTML Accept.
    assert response["Content-Type"].startswith("text/html"), (path, response)
    assert "Django REST framework" in response.content.decode()


@pytest.mark.django_db
@pytest.mark.urls("tests.colliding_api_urls")
def test_two_scrud_apis_in_one_namespace_are_warned_of():
    warnings = check_api_namespaces(None)
    assert [w.id for w in warnings] == ["scrud_django.W008"], warnings
    (warning,) = warnings
    assert "'scrud'" in warning.msg, warning.msg
    assert "urls_namespace" in warning.hint, warning.hint
    assert "scrud_django.urls" in warning.hint, warning.hint
    # Registered with Django's urls tag: ``manage.py check`` reports it.
    ran = checks.run_checks(tags=[checks.Tags.urls])
    assert "scrud_django.W008" in {message.id for message in ran}, ran


@pytest.mark.django_db
@pytest.mark.urls("tests.distinct_api_urls")
def test_scrud_apis_in_distinct_namespaces_are_not_warned_of():
    assert check_api_namespaces(None) == []


@pytest.mark.django_db
@pytest.mark.urls("tests.default_urls")
def test_the_default_urls_alone_are_not_warned_of():
    assert check_api_namespaces(None) == []
