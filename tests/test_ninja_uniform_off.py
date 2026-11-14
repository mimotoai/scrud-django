"""Parity of a ninja API built while ``SCOPED_RBAC["UNIFORM_ERRORS"]`` is off
(`tests.ninja_off_urls`), as a project configured with them off builds it:
it answers each case as DRF does, uniform errors off for the request (the
project's setting) or turned on afterwards, as the API built with them on
(`tests.test_front_end_parity`) does."""

import json
import threading

import ninja
import pytest
from django.conf import settings
from django.http import Http404
from django.test import RequestFactory, override_settings
from scoped_rbac.models import Role

from scrud_django.ninja import errors

from .front_ends import SETUP_TIME, Case, parity, serve_both
from .test_front_end_parity import (
    COMPUTE_MESSAGE_CASES,
    COMPUTE_REFUSAL_CASES,
    COMPUTED,
    COMPUTED_CASES,
    REFUSING,
    ROLE_CASES,
    UNIFORM_CASES,
    _admin,
    _semantic_links_of_the_drf_mount,
)

pytestmark = [pytest.mark.urls("tests.ninja_off_urls"), pytest.mark.django_db]


@pytest.fixture(autouse=True)
def _hsrs_built_here_are_forgotten():
    """As `tests.test_front_end_parity`'s: scrudful forgets the HTTP Schema
    Resources built under this URLconf when each test starts and ends."""
    with override_settings(ROOT_URLCONF="tests.ninja_off_urls"):
        yield


def _serve_off():
    """Build the API with uniform errors off (importing `tests.ninja_off_urls`
    the first time), then register the types in the test's transaction
    (`serve_both`), before and outside each run's."""
    from . import ninja_off_urls  # noqa: F401

    serve_both()


#: The answers that differed, each front end's native body: the 401 of the
#: anonymous (an item's, a computed resource's), a computed route's 405, a
#: ``compute``'s bare ``Http404``.
DIFFERED = [
    Case("item-anonymous", "GET", "{mount}partner-profiles/999999/"),
    Case("computed-anonymous", "GET", COMPUTED),
    Case("computed-patch", "PATCH", COMPUTED, _admin, body={}),
    Case("computed-compute-404-bare", "GET", REFUSING + "missing-bare/", _admin),
]

NATIVE_DETAILS = {
    "item-anonymous": (401, "Authentication credentials were not provided."),
    "computed-anonymous": (401, "Authentication credentials were not provided."),
    "computed-patch": (405, 'Method "PATCH" not allowed.'),
    "computed-compute-404-bare": (404, "Not found."),
}

#: The uniform-errors corpus, the computed routes' cases and the answers
#: that differed, each once.
CASES = list(
    {
        case.id: case
        for case in [
            *DIFFERED,
            *UNIFORM_CASES,
            *(case for case, _ in COMPUTED_CASES),
            *(case for case, *_ in COMPUTE_REFUSAL_CASES),
            *(
                Case(f"computed-{key}", "GET", REFUSING + key + "/", _admin)
                for key, *_ in COMPUTE_MESSAGE_CASES
            ),
        ]
    }.values()
)


#: The key of the role a role-route case's setup stores (a key of its own:
#: the harness replaces it in the URL, as each run stores it alike).
ROLE_KEY = 970901


def _stored_role(client):
    """A role (`ROLE_KEY`) whose ETag is ``"fixed"``, the same in each run;
    no one logged in."""
    Role(pk=ROLE_KEY, definition={"http.GET": ["tests://R"]}).save()
    Role.objects.filter(pk=ROLE_KEY).update(etag="fixed", modified_at=SETUP_TIME)
    return {"role": ROLE_KEY}


def _role(client):
    """`_stored_role`, with a superuser logged in on `client`."""
    _admin(client)
    return _stored_role(client)


ROLE = "{mount}roles/{role}/"
ROLE_BODY = {"definition": {"http.GET": ["tests://S"]}}
FIXED = {"If-Match": '"fixed"'}

#: Errors of scoped-rbac's role routes and of user-rbac-policy, whose
#: routers scoped-rbac builds reading ``UNIFORM_ERRORS`` (scrudful's 400,
#: 412 and 428 bodies, the problem entity's Links). Not a role body failing
#: validation: follow-up ``ninja-role-validation-native``.
ROLE_ERROR_CASES = [
    Case("role-put-stale", "PUT", ROLE, _role, ROLE_BODY, {"If-Match": '"stale"'}),
    Case("role-put-without-if-match", "PUT", ROLE, _role, ROLE_BODY),
    Case(
        "role-put-malformed-if-match", "PUT", ROLE, _role, ROLE_BODY, {"If-Match": "x"}
    ),
    Case("role-put-not-json", "PUT", ROLE, _role, "{x", FIXED),
    Case("role-patch-stale", "PATCH", ROLE, _role, ROLE_BODY, {"If-Match": '"stale"'}),
    Case("role-delete-stale", "DELETE", ROLE, _role, None, {"If-Match": '"stale"'}),
    Case("role-get-missing", "GET", "{mount}roles/970999/", _role),
    Case(
        "role-post-if-match-stale",
        "POST",
        "{mount}roles/",
        _role,
        ROLE_BODY,
        {"If-Match": '"stale"'},
    ),
    Case("role-put-anonymous", "PUT", ROLE, _stored_role, ROLE_BODY, FIXED),
    Case("user-rbac-policy-post", "POST", "{mount}user-rbac-policy/", _admin, {}),
]


ROLE_ERROR_STATUSES = {
    "role-put-stale": 412,
    "role-put-without-if-match": 428,
    "role-put-malformed-if-match": 400,
    "role-put-not-json": 400,
    "role-patch-stale": 412,
    "role-delete-stale": 412,
    "role-get-missing": 404,
    "role-post-if-match-stale": 412,
    "role-put-anonymous": 401,
    "user-rbac-policy-post": 405,
}


def _answers(case, uniform):
    _serve_off()
    setting = dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=uniform)
    with override_settings(SCOPED_RBAC=setting):
        return parity(case)


@pytest.mark.parametrize("case", DIFFERED, ids=lambda case: case.id)
def test_drfs_native_error_bodies_with_uniform_errors_off(case):
    drf, ninja = _answers(case, uniform=False)

    status, detail = NATIVE_DETAILS[case.id]
    assert drf.status == status, drf
    assert drf.headers["Content-Type"] == "application/json", drf
    assert json.loads(drf.body) == {"detail": detail}, drf
    assert drf == ninja


@pytest.mark.parametrize("uniform", [False, True], ids=["native", "uniform"])
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_an_api_built_with_uniform_errors_off_answers_as_drf(case, uniform):
    drf, ninja = _answers(case, uniform)

    assert drf == ninja


@pytest.mark.parametrize("uniform", [False, True], ids=["native", "uniform"])
@pytest.mark.parametrize(
    "case", [case for case, _ in ROLE_CASES], ids=lambda case: case.id
)
def test_role_routes_of_an_api_built_with_uniform_errors_off_answer_as_drf(
    case, uniform
):
    drf, ninja = _answers(case, uniform)

    assert drf == _semantic_links_of_the_drf_mount(ninja)


def test_the_api_gets_the_problem_details_handlers_built_with_uniform_errors_off():
    api = ninja.NinjaAPI(urls_namespace="scrud-ninja-install")
    off = dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=False)
    with override_settings(SCOPED_RBAC=off):
        errors.install(api)

        # The setting is the configured one again.
        assert not errors.uniform_errors()
        response = api.on_exception(RequestFactory().get("/"), Http404())

    # scoped-rbac's problem, which each URL pattern answers natively while
    # uniform errors are off for the request (errors.answer_natively_when_off),
    # not ninja's own {"detail": "Not Found"}.
    assert response.status_code == 404
    assert response["Content-Type"] == "application/problem+json"
    assert json.loads(response.content)["detail"] == "Not found."


@pytest.mark.parametrize("uniform", [False, True], ids=["native", "uniform"])
@pytest.mark.parametrize("case", ROLE_ERROR_CASES, ids=lambda case: case.id)
def test_role_route_errors_of_an_api_built_with_uniform_errors_off_answer_as_drf(
    case, uniform
):
    drf, ninja = _answers(case, uniform)

    assert drf.status == ROLE_ERROR_STATUSES[case.id], drf
    assert drf == _semantic_links_of_the_drf_mount(ninja)


def test_builds_on_two_threads_leave_uniform_errors_as_configured():
    # Thread a swaps the settings and waits; thread b asks to swap them
    # meanwhile (it waits for a's block to end), then leaves its block after
    # a's: unserialized, b would restore the settings a swapped in, on for
    # good.
    a_inside, b_about_to_swap, release_a, release_b = (
        threading.Event() for _ in range(4)
    )

    def build(entered, release, about_to_swap=None):
        if about_to_swap is not None:
            about_to_swap.set()
        with errors.building_with_uniform_errors_on():
            if entered is not None:
                entered.set()
            release.wait(5)

    off = dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=False)
    with override_settings(SCOPED_RBAC=off):
        a = threading.Thread(target=build, args=(a_inside, release_a))
        b = threading.Thread(target=build, args=(None, release_b, b_about_to_swap))
        a.start()
        assert a_inside.wait(5)
        b.start()
        assert b_about_to_swap.wait(5)
        release_a.set()
        a.join(5)
        release_b.set()
        b.join(5)

        assert not errors.uniform_errors()
