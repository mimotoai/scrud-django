"""Parity of a ninja API built while ``SCOPED_RBAC["UNIFORM_ERRORS"]`` is off
(`tests.ninja_off_urls`), as a project configured with them off builds it:
it answers each case as DRF does, uniform errors off for the request (the
project's setting) or turned on afterwards, as the API built with them on
(`tests.test_front_end_parity`) does."""

import json

import ninja
import pytest
from django.conf import settings
from django.http import Http404
from django.test import RequestFactory, override_settings

from scrud_django.ninja import errors

from .front_ends import Case, parity, serve_both
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
