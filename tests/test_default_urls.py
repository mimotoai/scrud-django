"""``scrud_django.urls`` is the ninja API (0.17.0): under ``/api/``
(`tests.default_urls`) its services, built-in types, UI types, roles and
scoped-rbac's semantic documents answer from ninja's routes, in the
namespace ``scrud``, while ``scrud_django.drf.urls``
(`tests.default_drf_urls`) answers as DRF. Two scrud APIs in one namespace are a system check error
(``scrud_django.E010``: the second fails every scrudful request), and so are
DRF registrations beside the ninja ``scrud_django.urls``
(``scrud_django.E011``: their links' built-in routes are DRF's). Routing
a scrud ninja API warns about the ``REST_FRAMEWORK`` settings it does not
read (``scrud_django.W008``: throttles, authentication classes) and about
an unset ``SCRUDFUL["BASE_URL"]`` (``scrud_django.W009``: its HTTP Schema
Resources fail)."""

from urllib.parse import urlsplit

import pytest
from django.contrib.auth.models import User
from django.core import checks
from django.core.cache import cache
from django.urls import NoReverseMatch, resolve
from ninja.errors import ConfigError

from scrud_django.checks import (
    check_api_namespaces,
    check_base_url_for_ninja,
    check_drf_registrations_routed,
    check_drf_settings_on_ninja,
)

from .front_ends import split_links

JSON = "application/json"

#: The relation of the Link to a route's HTTP Schema Resource.
HSR_REL = "https://w3id.org/semantic-http/ns#http-schema-resource-document"


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


#: A token-only API's authentication, and DRF's throttles (``tests``'
#: ``REST_FRAMEWORK`` sets neither).
TOKEN_ONLY = ["rest_framework.authentication.TokenAuthentication"]
THROTTLES = ["rest_framework.throttling.UserRateThrottle"]


def _rest_framework(settings, **values):
    """The settings' ``REST_FRAMEWORK`` (none without DRF) with `values`."""
    settings.REST_FRAMEWORK = {**getattr(settings, "REST_FRAMEWORK", {}), **values}


def _warned(messages, setting):
    """The ``scrud_django.W008`` of `messages` that names `setting`."""
    return [
        message
        for message in messages
        if message.id == "scrud_django.W008" and setting in message.msg
    ]


@pytest.mark.django_db
@pytest.mark.urls("tests.default_urls")
def test_drf_throttles_beside_the_ninja_urls_are_a_warning(settings):
    """``REST_FRAMEWORK["DEFAULT_THROTTLE_CLASSES"]`` limited scrud's DRF
    routes; the ninja API ``include("scrud_django.urls")`` routes since
    0.17.0 does not read it, so its requests are not rate limited."""
    _rest_framework(settings, DEFAULT_THROTTLE_CLASSES=THROTTLES)
    warnings = check_drf_settings_on_ninja(None)
    assert [w.id for w in warnings] == ["scrud_django.W008"], warnings
    (warning,) = warnings
    assert not warning.is_serious(), warning
    assert "DEFAULT_THROTTLE_CLASSES" in warning.msg, warning.msg
    assert "'scrud'" in warning.msg, warning.msg
    assert "not rate limited" in warning.msg, warning.msg
    assert "throttle=[...]" in warning.hint, warning.hint
    assert "scrud_django.drf.urls" in warning.hint, warning.hint
    # Registered with Django's urls and security tags.
    for tag in (checks.Tags.urls, checks.Tags.security):
        ran = checks.run_checks(tags=[tag])
        assert _warned(ran, "DEFAULT_THROTTLE_CLASSES"), (tag, ran)


@pytest.mark.django_db
@pytest.mark.urls("tests.default_urls")
def test_drf_authentication_beside_the_ninja_urls_is_a_warning(settings):
    """A token-only ``DEFAULT_AUTHENTICATION_CLASSES``: on ninja, without a
    ``SCOPED_RBAC["NINJA_USER"]`` hook, a token client is anonymous and the
    session's user is the user."""
    _rest_framework(settings, DEFAULT_AUTHENTICATION_CLASSES=TOKEN_ONLY)
    warnings = check_drf_settings_on_ninja(None)
    assert [w.id for w in warnings] == ["scrud_django.W008"], warnings
    (warning,) = warnings
    assert "DEFAULT_AUTHENTICATION_CLASSES" in warning.msg, warning.msg
    assert "anonymous" in warning.msg, warning.msg
    assert "session" in warning.msg, warning.msg
    assert "scrud_django.drf.auth.drf_user" in warning.hint, warning.hint
    assert "NINJA_USER" in warning.hint, warning.hint
    ran = checks.run_checks(tags=[checks.Tags.security])
    assert _warned(ran, "DEFAULT_AUTHENTICATION_CLASSES"), ran


@pytest.mark.django_db
@pytest.mark.urls("tests.default_urls")
def test_drf_throttles_and_authentication_are_a_warning_each(settings):
    _rest_framework(
        settings,
        DEFAULT_THROTTLE_CLASSES=THROTTLES,
        DEFAULT_AUTHENTICATION_CLASSES=TOKEN_ONLY,
    )
    warnings = check_drf_settings_on_ninja(None)
    assert [w.id for w in warnings] == ["scrud_django.W008"] * 2, warnings
    assert _warned(warnings, "DEFAULT_THROTTLE_CLASSES"), warnings
    assert _warned(warnings, "DEFAULT_AUTHENTICATION_CLASSES"), warnings


@pytest.mark.django_db
@pytest.mark.urls("tests.default_urls")
def test_drf_authentication_with_a_ninja_user_hook_is_not_a_warning(settings):
    """The hook decides who the user is on ninja (``drf_user`` runs the DRF
    authentication classes): nothing to warn about. Throttles still are."""
    _rest_framework(settings, DEFAULT_AUTHENTICATION_CLASSES=TOKEN_ONLY)
    settings.SCOPED_RBAC = {
        **settings.SCOPED_RBAC,
        "NINJA_USER": "scrud_django.drf.auth.drf_user",
    }
    assert check_drf_settings_on_ninja(None) == []
    _rest_framework(settings, DEFAULT_THROTTLE_CLASSES=THROTTLES)
    warnings = check_drf_settings_on_ninja(None)
    assert [w.id for w in warnings] == ["scrud_django.W008"], warnings
    assert _warned(warnings, "DEFAULT_THROTTLE_CLASSES"), warnings


@pytest.mark.django_db
@pytest.mark.urls("tests.default_urls")
@pytest.mark.parametrize(
    "classes",
    [
        [
            "rest_framework.authentication.SessionAuthentication",
            "rest_framework.authentication.BasicAuthentication",
        ],
        [
            "rest_framework.authentication.BasicAuthentication",
            "rest_framework.authentication.SessionAuthentication",
        ],
    ],
)
def test_drfs_default_authentication_is_not_a_warning(settings, classes):
    _rest_framework(settings, DEFAULT_AUTHENTICATION_CLASSES=classes)
    assert check_drf_settings_on_ninja(None) == []


@pytest.mark.django_db
@pytest.mark.urls("tests.default_urls")
def test_the_ninja_urls_without_rest_framework_are_not_a_warning(settings):
    if hasattr(settings, "REST_FRAMEWORK"):
        del settings.REST_FRAMEWORK
    assert check_drf_settings_on_ninja(None) == []


@pytest.mark.drf
@pytest.mark.django_db
@pytest.mark.urls("tests.default_drf_urls")
def test_drf_settings_of_a_drf_only_urlconf_are_not_a_warning(settings):
    """``scrud_django.drf.urls`` reads them: no scrud ninja API is routed
    here (other URLconfs of the process built some)."""
    _rest_framework(
        settings,
        DEFAULT_THROTTLE_CLASSES=THROTTLES,
        DEFAULT_AUTHENTICATION_CLASSES=TOKEN_ONLY,
    )
    assert check_drf_settings_on_ninja(None) == []


@pytest.mark.django_db
@pytest.mark.urls("tests.default_urls")
def test_an_unset_base_url_beside_the_ninja_urls_is_a_warning(admin, settings):
    """scrudful builds a ninja route's HTTP Schema Resource without a
    request: without ``SCRUDFUL["BASE_URL"]``, those whose links are relative
    (a type's list's) fail (a 500 in production), and
    ``scrud_django.W009`` says so."""
    url = _links(admin.options("/api/indexing-policies/"))[HSR_REL]
    assert admin.get(url).status_code == 200
    assert check_base_url_for_ninja(None) == []
    settings.SCRUDFUL = {}
    with pytest.raises(ConfigError, match="set a base URL"):
        admin.get(url)
    warnings = check_base_url_for_ninja(None)
    assert [w.id for w in warnings] == ["scrud_django.W009"], warnings
    (warning,) = warnings
    assert not warning.is_serious(), warning
    assert 'SCRUDFUL["BASE_URL"]' in warning.msg, warning.msg
    assert "'scrud'" in warning.msg, warning.msg
    assert "BASE_URL" in warning.hint, warning.hint
    ran = checks.run_checks(tags=[checks.Tags.urls])
    assert "scrud_django.W009" in {message.id for message in ran}, ran
    # No SCRUDFUL at all: the same.
    del settings.SCRUDFUL
    assert [w.id for w in check_base_url_for_ninja(None)] == ["scrud_django.W009"]


@pytest.mark.drf
@pytest.mark.django_db
@pytest.mark.urls("tests.default_drf_urls")
def test_an_unset_base_url_of_a_drf_only_urlconf_is_not_a_ninja_warning(settings):
    """DRF's HTTP Schema Resources resolve against the request (scrudful's
    own checks cover the DRF routes)."""
    settings.SCRUDFUL = {}
    assert check_base_url_for_ninja(None) == []
