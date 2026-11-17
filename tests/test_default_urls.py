"""``scrud_django.urls`` is the ninja API (0.17.0): under ``/api/``
(`tests.default_urls`) its services, built-in types, UI types, roles and
scoped-rbac's semantic documents answer from ninja's routes, in the
namespace ``scrud``, while ``scrud_django.drf.urls``
(`tests.default_drf_urls`) answers as DRF. Two scrud APIs in one namespace are a system check error
(``scrud_django.E010``: the second fails every scrudful request), and so are
DRF registrations beside the ninja ``scrud_django.urls``
(``scrud_django.E011``: their links' built-in routes are DRF's)."""

from urllib.parse import urlsplit

import pytest
from django.contrib.auth.models import User
from django.core import checks
from django.core.cache import cache
from django.urls import NoReverseMatch, resolve
from ninja.errors import ConfigError

from scrud_django.checks import check_api_namespaces, check_drf_registrations_routed

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
        # The studio's computed types (a branch's items, as a published
        # prefix's JSON gave them before 0.17.0; the type catalog).
        "/api/ui-branch-items/main/",
        "/api/ui-type-catalog/all/",
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


@pytest.mark.drf
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
def test_two_scrud_apis_in_one_namespace_are_an_error():
    errors = check_api_namespaces(None)
    assert [e.id for e in errors] == ["scrud_django.E010"], errors
    (error,) = errors
    assert error.is_serious(), error
    assert "'scrud'" in error.msg, error.msg
    assert "urls_namespace" in error.hint, error.hint
    assert "scrud_django.urls" in error.hint, error.hint
    # Registered with Django's urls tag: ``manage.py check`` reports it.
    ran = checks.run_checks(tags=[checks.Tags.urls])
    assert "scrud_django.E010" in {message.id for message in ran}, ran


@pytest.mark.django_db
@pytest.mark.urls("tests.colliding_api_urls")
def test_the_second_scrud_api_in_a_namespace_fails(admin):
    """What ``scrud_django.E010`` reports: Django reverses ``scrud``'s names
    within its first include (``scrud_django.urls`` at ``/api/``) only, so
    the API at ``/mine/`` cannot reverse its scrudful routes' HTTP Schema
    Resource route: its requests fail (a 500 in production), while the
    first API answers."""
    assert admin.get("/api/services/").status_code == 200
    with pytest.raises(ConfigError, match="HTTP Schema Resource"):
        admin.get("/mine/services/")


@pytest.mark.drf
@pytest.mark.django_db
@pytest.mark.urls("tests.drf_types_beside_ninja_urls")
def test_drf_registrations_beside_the_ninja_urls_are_an_error(admin):
    """An upgrader's ``registration.resource_types(...)`` beside
    ``include("scrud_django.urls")``: the type's links reverse the bare
    ``json-schema-detail``, which only ``scrud_django.drf.urls`` routes, so
    its requests fail (a 500 in production); ``scrud_django.E011`` says
    so."""
    with pytest.raises(NoReverseMatch):
        admin.get("/api/partner-profiles/")
    errors = check_drf_registrations_routed(None)
    assert [e.id for e in errors] == ["scrud_django.E011"], errors
    (error,) = errors
    assert error.is_serious(), error
    assert "partner-profiles" in error.msg, error.msg
    assert "json-schema-detail" in error.msg, error.msg
    assert "scrud_django.drf.urls" in error.hint, error.hint
    assert "json_resource_type_spec" in error.hint, error.hint
    ran = checks.run_checks(tags=[checks.Tags.urls])
    assert "scrud_django.E011" in {message.id for message in ran}, ran


@pytest.mark.drf
@pytest.mark.django_db
@pytest.mark.urls("tests.drf_types_beside_drf_urls")
def test_drf_registrations_beside_the_drf_urls_are_not_an_error(admin):
    assert admin.get("/api/partner-profiles/").status_code == 200
    assert check_drf_registrations_routed(None) == []


@pytest.mark.django_db
@pytest.mark.urls("tests.default_urls")
def test_the_ninja_urls_alone_are_not_a_drf_registration_error():
    # DRF registrations of other URLconfs in the process (the test
    # session's) route nothing here.
    assert check_drf_registrations_routed(None) == []


@pytest.mark.django_db
@pytest.mark.urls("tests.throttled_api_urls")
def test_ninjas_throttling_limits_a_scrud_api(admin):
    """The README's remedy for ``DEFAULT_THROTTLE_CLASSES``, which the ninja
    API does not read: ninja's own throttles, given to ``api(throttle=)``."""
    cache.clear()
    try:
        statuses = [admin.get("/api/services/").status_code for _ in range(3)]
        assert statuses == [200, 200, 429], statuses
    finally:
        cache.clear()


@pytest.mark.django_db
@pytest.mark.urls("tests.distinct_api_urls")
def test_scrud_apis_in_distinct_namespaces_are_not_an_error():
    assert check_api_namespaces(None) == []


@pytest.mark.django_db
@pytest.mark.urls("tests.default_urls")
def test_the_default_urls_alone_are_not_an_error():
    assert check_api_namespaces(None) == []
