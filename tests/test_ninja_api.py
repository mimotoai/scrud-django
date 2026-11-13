"""`scrud_django.ninja.api`, its error rendering (`scrud_django.ninja.errors`)
and its entry checks (`scrud_django.ninja.entry`), on probe routes of a
throwaway API mounted by this module (``/ninja/``), beside a DRF probe view
(``/drf/``)."""

import dataclasses
import inspect
import json
from contextlib import contextmanager

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings
from django.urls import path, resolve
from django.utils.http import quote_etag
from ninja import NinjaAPI, Router
from ninja.errors import AuthenticationError
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

import scrud_django.ninja
from scrud_django import registration
from scrud_django.core import lists, resources, writes
from scrud_django.core.lists import list_etag
from scrud_django.core.types import requirement_for
from scrud_django.exceptions import (
    QueryParameterError,
    RequestBodyTooDeepError,
    ResourceInUseError,
    drf_errors,
)
from scrud_django.mixins.view_mixins import ListValidatorsMixin
from scrud_django.models import Resource, ResourceType
from scrud_django.ninja import routers, services
from scrud_django.ninja.api import RESERVED_SLUGS
from scrud_django.ninja.entry import entry

from .front_ends import MOUNTS, serve_both
from .rbac_defaults import TYPE_URI_PARTNER_PROFILES as PP
from .resource_types import PARTNER_PROFILES_TYPE, ninja_type_specs
from .test_core_resources import user_with

pytestmark = [pytest.mark.urls(__name__), pytest.mark.django_db]

# The scrud errors the probes raise, by name. The query parameter error's
# text is not ASCII and holds U+2028, which DRF's JSON escapes.
ERRORS = {
    "query": lambda: QueryParameterError({"näme": ["Not a field  here: ü."]}),
    "deep": lambda: RequestBodyTooDeepError(32),
    "in-use": lambda: ResourceInUseError(
        Resource(pk=7), [ResourceType(type_uri="tests://Typed", schema_id=7)]
    ),
}

probes = Router()
probes.add_decorator(entry, mode="view")


@probes.get("/errors/{name}/")
def ninja_error_probe(request, name: str):
    raise ERRORS[name]()


@probes.get("/hand-built-link/")
def hand_built_link_probe(request):
    """An app's problem details error whose Link has a link-value with no
    ``>``."""
    body = {"type": "about:blank", "title": "Bad", "status": 400, "detail": "bad"}
    response = HttpResponse(
        json.dumps(body), status=400, content_type="application/problem+json"
    )
    response["Link"] = "<http://example.org/a>; rel=a, <unterminated"
    return response


@probes.put("/partner-profiles/{slug}/")
def detail_probe(request, slug: str):
    """A detail-shaped PUT that asks the core's request check."""
    resource_type = registration.get_resource_type_for("partner-profiles")
    resources.check_request(
        request,
        resource_type,
        requirement_for(resource_type),
        list_route=False,
        body=json.loads(request.body),
    )
    return HttpResponse(status=204)


@probes.api_operation(
    ["POST", "PUT", "PATCH", "DELETE"], "/write-then-answer/{pk}/{status}/"
)
def write_then_answer(request, pk: int, status: int):
    """Writes the user `pk`'s first name, then answers `status`."""
    User.objects.filter(pk=pk).update(first_name="written")
    return HttpResponse(status=status)


# A router an app adds to scrud's API, as the README says to: it passes
# scrud's entry checks (`scrud_django.ninja.entry.entry`), which the API does
# not give the routers it did not build.
added = Router()
added.add_decorator(entry, mode="view")


@added.api_operation(["GET", "POST"], "/whoami/")
def whoami(request):
    """The requester's username, as the entry checks resolved them."""
    return {"user": request.user.username}


@added.api_operation(["OPTIONS"], "/options-probe/")
def options_probe(request):
    """An app's OPTIONS operation, which resolves nothing itself."""
    return HttpResponse(status=204)


@api_view(["GET"])
@permission_classes([AllowAny])
@drf_errors
def drf_error_probe(request, name):
    raise ERRORS[name]()


def __getattr__(name):
    """This URLconf's ``urlpatterns`` (``/drf/``'s probe, and the probe API at
    ``/ninja/``), built on the first URL resolution, in a test: the API
    registers the built-in types as it is built, which needs the database."""
    if name != "urlpatterns":
        raise AttributeError(name)
    probe_api = scrud_django.ninja.api(urls_namespace="test-ninja-api")
    probe_api.add_router("", probes)
    probe_api.add_router("added/", added)
    patterns = globals()["urlpatterns"] = [
        path("drf/errors/<name>/", drf_error_probe),
        path("ninja/", probe_api.urls),
    ]
    return patterns


# An OPTIONS whose ``request.method`` middleware rewrote (tests.middleware).
REWRITING = override_settings(
    MIDDLEWARE=(
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "tests.middleware.MethodOverrideMiddleware",
    )
)


def test_api_refuses_auth():
    with pytest.raises(TypeError):
        scrud_django.ninja.api(auth=object())


def test_api_always_serves_the_built_in_types():
    # Every stored type's Links name its schema and context resources, which
    # the built-in types' routes serve: there is no API without them (a
    # ``built_ins`` argument goes to ``NinjaAPI``, which refuses it).
    with pytest.raises(TypeError):
        scrud_django.ninja.api(urls_namespace="test-no-built-ins", built_ins=False)
    client = Client()
    client.force_login(
        User.objects.create(username="admin", is_staff=True, is_superuser=True)
    )
    for slug in ("json-schema", "json-ld", "indexing-policies", "workflow-policies"):
        response = client.get(f"/ninja/{slug}/")
        assert response.status_code == 200, (slug, response.content)


@pytest.mark.parametrize("name", sorted(ERRORS))
def test_scrud_errors_render_as_on_drf(name):
    drf = Client().get(f"/drf/errors/{name}/")
    ninja = Client().get(f"/ninja/errors/{name}/")

    assert drf.status_code == ERRORS[name]().status_code, drf.content
    assert drf["Content-Type"] == "application/problem+json"
    assert (ninja.status_code, ninja["Content-Type"], ninja.get("Link")) == (
        drf.status_code,
        drf["Content-Type"],
        drf.get("Link"),
    )
    assert ninja.content == drf.content


def test_an_unterminated_link_is_kept_when_answered_natively():
    off = dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=False)
    with override_settings(SCOPED_RBAC=off):
        response = Client().get("/ninja/hand-built-link/")

    assert response.status_code == 400, response.content
    assert response.json() == {"detail": "bad"}
    assert response["Link"] == "<http://example.org/a>; rel=a, <unterminated"


@pytest.fixture
def check_request_calls(monkeypatch):
    """The calls of `resources.check_request`, which records them only."""
    calls = []
    monkeypatch.setattr(
        resources, "check_request", lambda *args, **kwargs: calls.append(args)
    )
    return calls


@pytest.fixture
def put_in_b():
    """A client of a user who may PUT partner profiles in context "b" only, the
    type registered (as the ninja front end registers it)."""
    registration.json_resource_type_spec(**PARTNER_PROFILES_TYPE).register()
    client = Client()
    client.force_login(user_with(({"http.PUT": [PP]}, "b")))
    return client


@REWRITING
def test_rewritten_options_is_refused_before_the_core(put_in_b, check_request_calls):
    url = "/ninja/partner-profiles/1/"
    body = json.dumps({"name": "x", "rbac_context": "a"})

    response = put_in_b.options(
        url,
        body,
        content_type="application/json",
        headers={"X-HTTP-Method-Override": "PUT"},
    )

    assert response.status_code == 403, response.content
    assert check_request_calls == []
    # The same PUT, not rewritten, reaches the core.
    response = put_in_b.put(url, body, content_type="application/json")
    assert response.status_code == 204, response.content
    assert len(check_request_calls) == 1


@pytest.mark.urls("tests.parity_urls")
@REWRITING
def test_rewritten_options_is_refused_on_the_real_detail_route(
    put_in_b, check_request_calls
):
    # The ninja API's own partner-profiles detail route (tests.ninja_api), an
    # item that exists: the rewritten OPTIONS is refused at entry, before
    # any core call; the same PUT, not rewritten, reaches the core.
    serve_both()
    item = registration.ResourceRegistration.register(
        {"name": "x", "rbac_context": "b"}, "partner-profiles"
    )
    url = f"{MOUNTS['ninja']}partner-profiles/{item.pk}/"
    body = json.dumps({"name": "y", "rbac_context": "a"})

    response = put_in_b.options(
        url,
        body,
        content_type="application/json",
        headers={"X-HTTP-Method-Override": "PUT", "If-Match": quote_etag(item.etag)},
    )

    assert response.status_code == 403, response.content
    assert check_request_calls == []
    put_in_b.put(
        url,
        body,
        content_type="application/json",
        headers={"If-Match": quote_etag(item.etag)},
    )
    assert check_request_calls


@pytest.mark.parametrize(
    "user, status",
    [(lambda: User.objects.create(username="rewriter"), 403), (AnonymousUser, 401)],
)
def test_entry_itself_refuses_a_rewritten_options(user, status, check_request_calls):
    # Past the URL patterns' own refusal (scoped-rbac's wrapping of the API's
    # views): ninja's view, where `entry` runs.
    url = "/ninja/partner-profiles/1/"
    match = resolve(url)
    view = inspect.unwrap(match.func)
    assert view is not match.func
    request = RequestFactory().options(url, "{}", content_type="application/json")
    request.method = "PUT"
    request.user = user()

    response = view(request, *match.args, **match.kwargs)

    assert response.status_code == status, response.content
    assert check_request_calls == []


def test_entry_resolves_the_user_with_the_session_csrf_check():
    row = User.objects.create(username="row", first_name="before")
    client = Client(enforce_csrf_checks=True)
    client.force_login(User.objects.create(username="writer"))

    response = client.put(f"/ninja/write-then-answer/{row.pk}/200/")

    assert response.status_code == 403, response.content
    assert "CSRF Failed" in response.json()["detail"]
    row.refresh_from_db()
    assert row.first_name == "before"


def _refusing_credentials(request):
    """A ``SCOPED_RBAC["NINJA_USER"]`` hook refusing the request's credentials
    (ninja's ``AuthenticationError``, not an access error)."""
    raise AuthenticationError()


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_entry_answers_a_hook_refusing_the_credentials(method):
    # As DRF answers the authentication it runs failing: a 401, before the
    # operation runs, not an error escaping the API.
    row = User.objects.create(username="row", first_name="before")
    url = {
        "GET": "/ninja/errors/query/",
        "PUT": f"/ninja/write-then-answer/{row.pk}/200/",
    }[method]
    refusing = {**settings.SCOPED_RBAC, "NINJA_USER": _refusing_credentials}
    with override_settings(SCOPED_RBAC=refusing):
        response = Client().generic(method, url)

    assert response.status_code == 401, response.content
    assert response.json()["code"] == "not_authenticated"
    row.refresh_from_db()
    assert row.first_name == "before"


@pytest.mark.parametrize(
    "method, headers, status, first_name",
    [
        ("PUT", {}, 400, "before"),
        ("PATCH", {}, 400, "before"),
        ("DELETE", {}, 400, "before"),
        ("POST", {"If-None-Match": "*"}, 400, "before"),
        ("POST", {"If-Match": '"x"'}, 412, "before"),
        (
            "POST",
            {"If-Unmodified-Since": "Thu, 01 Oct 2026 12:00:00 GMT"},
            400,
            "before",
        ),
        # A success commits.
        ("PUT", {}, 200, "written"),
        # A POST with no precondition runs in no transaction of the entry's,
        # as on DRF.
        ("POST", {}, 400, "written"),
    ],
)
def test_unsafe_methods_run_in_one_transaction(method, headers, status, first_name):
    row = User.objects.create(username="row", first_name="before")
    client = Client()
    client.force_login(User.objects.create(username="writer"))

    response = client.generic(
        method, f"/ninja/write-then-answer/{row.pk}/{status}/", headers=headers
    )

    assert response.status_code == status, response.content
    row.refresh_from_db()
    assert row.first_name == first_name


def _list_etag_header(name):
    """``{name: ETag}`` of the partner profiles list as a superuser gets it."""
    resource_type = registration.get_resource_type_for("partner-profiles")
    items = list(Resource.objects.filter(resource_type=resource_type))
    etag = list_etag(resource_type, items=items, count=len(items), query=[])
    return {name: quote_etag(etag)}


@pytest.mark.urls("tests.parity_urls")
@pytest.mark.parametrize("front_end", ["drf", "ninja"])
@pytest.mark.parametrize(
    "headers, status, events",
    [
        (
            lambda: _list_etag_header("If-Match"),
            201,
            ["enter", "evaluate", "save", "exit"],
        ),
        (
            lambda: {"If-None-Match": '"other"'},
            201,
            ["enter", "evaluate", "save", "exit"],
        ),
        # ``If-None-Match: *`` needs no validator: it fails on the list
        # existing, which it always does (core.lists.list_validators_needed),
        # so no lock is taken, as on DRF (LockedWriteMixin.is_conditional_create).
        (lambda: {"If-None-Match": "*"}, 412, []),
        (lambda: {}, 201, ["save"]),
    ],
    ids=["if-match", "if-none-match-other", "if-none-match-star", "plain"],
)
def test_conditional_create_holds_the_type_lock(
    monkeypatch, front_end, headers, status, events
):
    """A POST to the list whose preconditions need the list's validators
    enters the type's conditional-create lock (core.writes) once, reads the
    list's validators to evaluate them, then saves, while it holds it; one
    that needs none takes no lock and reads no validators."""
    serve_both()
    registration.ResourceRegistration.register(
        {"name": "x", "rbac_context": "a"}, "partner-profiles"
    )
    seen = []
    lock = writes.conditional_create_lock

    @contextmanager
    def recording_lock(resource_type, using):
        assert resource_type.slug == "partner-profiles"
        with lock(resource_type, using):
            seen.append("enter")
            yield
            seen.append("exit")

    register_instance = registration.ResourceRegistration.register_instance

    def recording_save(resource):
        seen.append("save")
        return register_instance(resource)

    # The list's validators, read to evaluate the preconditions: on DRF by
    # the view (once a request, cached), on ninja by the core.
    if front_end == "drf":
        drf_validators = ListValidatorsMixin.create_list_validators

        def recording_drf_validators(view, request):
            # Read once, when a precondition needs them: (None, None) else.
            cached = "_scrud_create_validators" in view.__dict__
            validators = drf_validators(view, request)
            if not cached and validators != (None, None):
                seen.append("evaluate")
            return validators

        monkeypatch.setattr(
            ListValidatorsMixin, "create_list_validators", recording_drf_validators
        )
    else:
        core_validators = lists.create_list_validators

        def recording_core_validators(*args, **kwargs):
            seen.append("evaluate")
            return core_validators(*args, **kwargs)

        monkeypatch.setattr(lists, "create_list_validators", recording_core_validators)
    monkeypatch.setattr(writes, "conditional_create_lock", recording_lock)
    monkeypatch.setattr(
        registration.ResourceRegistration,
        "register_instance",
        staticmethod(recording_save),
    )
    client = Client()
    client.force_login(
        User.objects.create(username="admin", is_staff=True, is_superuser=True)
    )

    response = client.post(
        f"{MOUNTS[front_end]}partner-profiles/",
        json.dumps({"name": "y", "rbac_context": "a"}),
        content_type="application/json",
        headers=headers(),
    )

    assert response.status_code == status, response.content
    assert seen == events


def test_the_api_serves_no_docs_or_openapi_by_default():
    # Nothing comparable is served on DRF; a caller turns them on.
    assert Client().get("/ninja/").status_code == 404
    for path_ in ("/ninja/docs", "/ninja/openapi.json"):
        assert Client().get(path_).status_code == 404, path_


def test_a_turned_on_openapi_schema_names_each_operation_once(capsys):
    api = scrud_django.ninja.api(
        types=ninja_type_specs(),
        urls_namespace="test-ninja-openapi",
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    assert (api.docs_url, api.openapi_url) == ("/docs", "/openapi.json")
    capsys.readouterr()

    schema = api.get_openapi_schema(path_prefix="/api/")

    # ninja prints a warning for an operation id used twice.
    assert "Warning" not in capsys.readouterr().out
    # ninja describes an operation serving GET and HEAD under both methods of
    # its path, with its one id: each id is that of one path's operation.
    paths_of = {}
    for path_, operations in schema["paths"].items():
        for operation in operations.values():
            paths_of.setdefault(operation["operationId"], set()).add(path_)
    assert {op_id for op_id, paths in paths_of.items() if len(paths) > 1} == set()
    assert {
        "partner-profiles_list",
        "partner-profiles_create",
        "partner-profiles_retrieve",
        "partner-profiles_replace",
        "partner-profiles_destroy",
        "summaries_computed",
        "services_list",
        "collections-json-schema_retrieve",
        "collections-json-ld_retrieve",
    } <= set(paths_of)


def _resolved_user(request):
    """A ``SCOPED_RBAC["NINJA_USER"]`` hook: the user ``resolved``."""
    return User.objects.get(username="resolved")


def test_a_router_added_with_the_entry_checks_resolves_and_csrf_checks():
    # The README's recipe for a router an app adds to scrud's API.
    session_user = User.objects.create(username="session")
    User.objects.create(username="resolved")
    client = Client(enforce_csrf_checks=True)
    client.force_login(session_user)

    # A session POST without a CSRF token is refused, as DRF refuses it.
    refused = client.post("/ninja/added/whoami/")
    assert refused.status_code == 403, refused.content
    assert "CSRF Failed" in refused.json()["detail"]
    # The requester is resolved as scrud resolves it (the NINJA_USER hook's),
    # not left as the session's.
    hooked = {**settings.SCOPED_RBAC, "NINJA_USER": _resolved_user}
    with override_settings(SCOPED_RBAC=hooked):
        response = client.get("/ninja/added/whoami/")
    assert response.status_code == 200, response.content
    assert response.json() == {"user": "resolved"}


# --- api() refuses what it cannot serve, before it registers anything ---

REFUSED_URI = "tests://Refused"
REFUSED_SLUG = "refused-things"


def _registered_state():
    from scrud_django.core import types as core_types

    return (
        set(ResourceType.objects.values_list("type_uri", flat=True)),
        dict(core_types._SLUG_IRIS),
        dict(core_types._OPTIONS),
    )


def _spec(uri=REFUSED_URI, slug=REFUSED_SLUG):
    return registration.json_resource_type_spec(uri, slug)


def _refused(types, match=None, error=ImproperlyConfigured):
    before = _registered_state()
    constructed = []
    original = NinjaAPI.__init__

    def recording(self, *args, **kwargs):
        constructed.append(self)
        original(self, *args, **kwargs)

    NinjaAPI.__init__ = recording
    try:
        with pytest.raises(error, match=match):
            scrud_django.ninja.api(urls_namespace="test-api-refused", types=types)
    finally:
        NinjaAPI.__init__ = original
    assert _registered_state() == before
    assert constructed == []  # no API, no routers, before the refusal


def test_a_slug_a_built_in_type_has_is_refused_naming_both_iris():
    # The spec is refused when it is built (`TypeSpec.__post_init__`), so
    # `api()` is never handed one.
    built_in = {s.slug: s for s in registration.built_in_type_specs()}["json-schema"]
    for build in (
        lambda: registration.json_resource_type_spec(REFUSED_URI, slug="json-schema"),
        lambda: registration.json_resource_type_spec(
            built_in.resource_type_uri, REFUSED_SLUG
        ),
    ):
        with pytest.raises(ImproperlyConfigured, match=built_in.resource_type_uri):
            build()


def test_an_empty_slug_is_refused_before_any_side_effect():
    # `types.md` N2 / `errors.md` N3: it would have shadowed the API's root.
    before = _registered_state()
    with pytest.raises(ImproperlyConfigured, match="slug"):
        registration.json_resource_type_spec(REFUSED_URI, "")
    assert _registered_state() == before


@pytest.mark.parametrize("slug", ["{pk}", "a/b", "a b", "a.b", "é"])
def test_a_slug_that_is_not_one_url_segment_is_refused(slug):
    _refused([_spec(slug=slug)], match="URL segment")
    spec = dataclasses.replace(_spec(), slug=slug)
    _refused([spec], match="URL segment")


def test_two_specs_with_one_slug_are_refused():
    _refused([_spec(), _spec("tests://RefusedToo")], match="tests://RefusedToo")


def test_two_specs_with_one_iri_are_refused():
    _refused([_spec(), _spec(slug="refused-twice")], match=REFUSED_URI)


@pytest.mark.parametrize("slug", sorted(RESERVED_SLUGS))
def test_a_slug_that_is_a_fixed_prefix_is_refused(slug):
    _refused([_spec(slug=slug)], match=slug)


def test_the_reserved_slugs_cover_every_fixed_route_prefix():
    # What scoped-rbac's router and the collection documents' mount.
    from scoped_rbac.ninja.api import router
    from scrudful.ninja import ScrudfulRouter

    probe = NinjaAPI(urls_namespace="test-reserved", default_router=ScrudfulRouter())
    probe.add_router("", router())
    probe.add_router("", routers.collections_router())
    probe.add_router("services/", services.services_router())
    first = {
        str(p.pattern).split("/")[0] for p in probe._get_urls() if hasattr(p, "pattern")
    } - {"", "docs", "openapi.json"}
    assert first <= RESERVED_SLUGS


@pytest.mark.parametrize("element", ["not-a-spec", object(), None])
def test_an_element_that_is_not_a_type_spec_is_refused(element):
    _refused([element], match="json_resource_type_spec", error=TypeError)


def test_entry_in_operation_mode_is_refused_at_decoration_time():
    router = Router()
    router.add_decorator(entry, mode="operation")
    with pytest.raises(TypeError, match='mode="view"'):

        @router.get("/x/")
        def x(request):
            return {}

        probe = NinjaAPI(urls_namespace="test-operation-mode")
        probe.add_router("", router)
        assert probe.urls


def test_a_real_options_on_an_entry_router_resolves_the_user():
    refusing = {**settings.SCOPED_RBAC, "NINJA_USER": _refusing_credentials}
    with override_settings(SCOPED_RBAC=refusing):
        response = Client().options("/ninja/added/options-probe/")
    assert response.status_code == 401, response.content


def test_a_preflight_on_an_entry_router_does_not_resolve_the_user():
    refusing = {**settings.SCOPED_RBAC, "NINJA_USER": _refusing_credentials}
    with override_settings(SCOPED_RBAC=refusing):
        response = Client().options(
            "/ninja/added/options-probe/",
            headers={
                "Origin": "http://example.org",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.status_code != 401, response.content
