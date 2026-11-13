"""`scrud_django.ninja.api`, its error rendering (`scrud_django.ninja.errors`)
and its entry checks (`scrud_django.ninja.entry`), on probe routes of a
throwaway API mounted by this module (``/ninja/``), beside a DRF probe view
(``/drf/``)."""

import inspect
import json
from contextlib import contextmanager

import pytest
from django.contrib.auth.models import AnonymousUser, User
from django.http import HttpResponse
from django.test import Client, RequestFactory, override_settings
from django.urls import path, resolve
from django.utils.http import quote_etag
from ninja import Router
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

import scrud_django.ninja
from scrud_django import registration
from scrud_django.core import resources, writes
from scrud_django.core.lists import list_etag
from scrud_django.core.types import requirement_for
from scrud_django.exceptions import (
    QueryParameterError,
    RequestBodyTooDeepError,
    ResourceInUseError,
    drf_errors,
)
from scrud_django.models import Resource, ResourceType
from scrud_django.ninja.entry import entry

from .front_ends import MOUNTS, serve_both
from .rbac_defaults import TYPE_URI_PARTNER_PROFILES as PP
from .resource_types import PARTNER_PROFILES_TYPE
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


PROBE_API = scrud_django.ninja.api(urls_namespace="test-ninja-api", built_ins=False)
PROBE_API.add_router("", probes)


@api_view(["GET"])
@permission_classes([AllowAny])
@drf_errors
def drf_error_probe(request, name):
    raise ERRORS[name]()


urlpatterns = [
    path("drf/errors/<name>/", drf_error_probe),
    path("ninja/", PROBE_API.urls),
]

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
        (lambda: _list_etag_header("If-Match"), 201, ["enter", "save", "exit"]),
        (lambda: {"If-None-Match": '"other"'}, 201, ["enter", "save", "exit"]),
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
    enters the type's conditional-create lock (core.writes) once, and saves
    while it holds it; one that needs none takes no lock."""
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
