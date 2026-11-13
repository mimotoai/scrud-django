"""Parity: the DRF and the ninja front end, both mounted by
`tests.parity_urls` (``/drf/``, ``/ninja/``) over one database, answer each
case alike, normalized only as `tests.front_ends` says."""

import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import override_settings
from django.utils.http import http_date, quote_etag
from scoped_rbac.models import Role, RoleAssignment

from scrud_django import collection_type_uri_for, registration
from scrud_django.models import Resource, ResourceType

from .front_ends import SETUP_TIME, Case, Exchange, normalized, parity
from .resource_types import PARTNER_PROFILES_TYPE, WORKFLOW_NOTE_TYPE_URI
from .test_core_workflow import DRAFT, NOTE_POLICY, PUBLISHED

pytestmark = [pytest.mark.urls("tests.parity_urls"), pytest.mark.django_db]

HSR_REL = "https://w3id.org/semantic-http/ns#http-schema-resource-document"


def test_the_harness_normalizes_only_the_listed_differences():
    body = (
        '{"href":"http://testserver/ninja/todos/12/","id":12,'
        '"next":"http://testserver/ninja/todos/123/?page=12","other":"/x12/"}'
    )
    response = HttpResponse(body, content_type="application/json")
    response["Content-Length"] = str(len(body.encode()))
    response["Date"] = "Thu, 01 Oct 2026 12:00:00 GMT"
    response["Vary"] = "Accept"
    response["WWW-Authenticate"] = 'Basic realm="api"'
    response.set_cookie("sessionid", "x")
    response["Location"] = "http://testserver/ninja/todos/12/"
    response["Link"] = (
        '<http://testserver/ninja/todos/_hsr/slug>; rel="' + HSR_REL + '"; '
        'type="application/json", <http://testserver/ninja/todos/12/s>; '
        'rel="describedby"'
    )
    response["ETag"] = '"12"'

    exchange = normalized(response, "/ninja/", {"todo": 12, "next": 123})

    expected_body = (
        '{"href":"http://testserver{mount}todos/{todo}/","id":12,'
        '"next":"http://testserver{mount}todos/{next}/?page=12","other":"/x12/"}'
    )
    assert exchange == Exchange(
        status=200,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(expected_body.encode())),
            "Location": "http://testserver{mount}todos/{todo}/",
            "Link": (
                '<{hsr}>; rel="' + HSR_REL + '"; type="application/json", '
                '<http://testserver{mount}todos/{todo}/s>; rel="describedby"'
            ),
            "ETag": '"12"',
        },
        body=expected_body,
    )


def test_the_harness_refuses_keys_it_cannot_tell_apart():
    with pytest.raises(ValueError):
        normalized(HttpResponse(), "/drf/", {"a": 1, "b": 1})


def test_each_front_end_runs_on_the_same_fresh_data():
    registration.json_resource_type_spec(**PARTNER_PROFILES_TYPE).register()
    seen = []

    def setup(client):
        count = Resource.objects.count()
        resource = registration.ResourceRegistration.register(
            {"name": "x", "rbac_context": "a"}, "partner-profiles"
        )
        seen.append((count, resource.etag, resource.modified_at))
        return {"profile": resource.pk}

    parity(Case("fresh", "GET", "{mount}partner-profiles/{profile}/", setup))

    count, _, modified_at = seen[0]
    assert seen[0] == seen[1]
    assert modified_at == SETUP_TIME
    # Nothing a run wrote is left.
    assert Resource.objects.count() == count


def test_hooked_types_are_served_alike_in_both_runs():
    # hooked-notes is one of the types the session does not commit
    # (conftest's django_db_setup): only the URLconf's import, or the
    # harness, registers it.
    seen = []

    def setup(client):
        seen.append(
            list(
                ResourceType.objects.filter(slug="hooked-notes").values_list(
                    "pk", "etag", "modified_at"
                )
            )
        )
        client.force_login(
            User.objects.create(username="admin", is_staff=True, is_superuser=True)
        )
        note = registration.ResourceRegistration.register(
            {"text": "n", "rbac_context": "a"}, "hooked-notes"
        )
        return {"note": note.pk}

    drf, ninja = parity(
        Case("hooked-notes", "GET", "{mount}hooked-notes/{note}/", setup)
    )

    assert seen[0], "hooked-notes is not registered"
    assert seen[0] == seen[1]
    assert drf.status == 200, drf
    assert drf == ninja


def test_hooked_types_are_served_alike_on_a_processs_first_parity_call():
    # The test above, alone in a fresh process: its parity call is the first,
    # before the URLconf was imported.
    test = "tests/test_front_end_parity.py::test_hooked_types_are_served_alike_in_both_runs"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.xfail(strict=True, reason="Task 8")
def test_services_match():
    drf, ninja = parity(Case("services", "GET", "{mount}services/"))

    assert drf == ninja


def test_unknown_route_is_404_on_both():
    drf, ninja = parity(Case("no-such-type", "GET", "{mount}no-such-type/"))

    assert drf.status == ninja.status == 404


# The item routes (GET, HEAD, PUT, DELETE; PATCH is not routed), on a plain
# type (partner-profiles) and on a document type (json-schema, served as
# application/schema+json).

PROFILES = "tests://PartnerProfiles"
JSON_SCHEMA = "http://json-schema.org/draft-04/schema"


def _user(client, *grants, superuser=False):
    """A user holding each ``(role definition, context)`` of `grants`, logged
    in on `client`."""
    user = User.objects.create(
        username=f"user-{User.objects.count()}",
        is_staff=superuser,
        is_superuser=superuser,
    )
    for definition, context in grants:
        role = Role(definition=definition, etag="r", modified_at=SETUP_TIME)
        role.save()
        RoleAssignment(
            user=user, role=role, rbac_context=context, etag="a", modified_at=SETUP_TIME
        ).save()
    client.force_login(user)
    return user


def _etag(name):
    """The headers of a request carrying `If-Match` with the current ETag of
    the resource `name` of a case's setup."""

    def headers(keys):
        return {"If-Match": quote_etag(Resource.objects.get(pk=keys[name]).etag)}

    return headers


def _nested(depth):
    body = {"name": "x"}
    for _ in range(depth):
        body = {"nested": body}
    return dict(body, name="x")


class ItemType:
    """A type the item corpus runs on: its slug and IRI, and the content of
    a resource stored in a context."""

    def __init__(self, slug, iri, content, body):
        self.slug = slug
        self.iri = iri
        self.content = content
        self.body = body

    def store(self, context="a", **content):
        return registration.ResourceRegistration.register(
            {**self.content(context), **content}, self.slug
        )

    def path(self, key="{item}"):
        return "{mount}" + f"{self.slug}/{key}/"

    def setup(self, *grants, context="a", superuser=False, login=True):
        """A setup storing a resource in `context` (``item``) and logging in a
        user holding `grants` (each a role definition, in context "a")."""

        def setup(client):
            item = self.store(context)
            if login:
                _user(client, *((g, "a") for g in grants), superuser=superuser)
            return {"item": item.pk}

        return setup


PROFILE = ItemType(
    "partner-profiles",
    PROFILES,
    lambda context: {"name": "x", "rbac_context": context},
    lambda context: {"name": "y", "rbac_context": context},
)
SCHEMA = ItemType(
    "json-schema",
    JSON_SCHEMA,
    lambda context: {"type": "object", "rbac_context": context},
    lambda context: {"type": "string", "rbac_context": context},
)


def _read_cases(kind):
    """The read cases of `kind` (an `ItemType`), with the status DRF
    answers."""
    reader = {"http.GET": [kind.iri]}
    writer = {"http.GET": [kind.iri], "http.PUT": [kind.iri]}
    s = kind.slug
    return [
        (Case(f"{s}-get-admin", "GET", kind.path(), kind.setup(superuser=True)), 200),
        (Case(f"{s}-get-reader", "GET", kind.path(), kind.setup(reader)), 200),
        (Case(f"{s}-get-no-roles", "GET", kind.path(), kind.setup()), 403),
        (
            Case(f"{s}-get-anonymous", "GET", kind.path(), kind.setup(login=False)),
            401,
        ),
        (
            Case(
                f"{s}-get-json",
                "GET",
                kind.path(),
                kind.setup(reader),
                headers={"Accept": "application/json"},
            ),
            200,
        ),
        (
            Case(
                f"{s}-get-own-type",
                "GET",
                kind.path(),
                kind.setup(reader),
                headers={"Accept": "application/schema+json, application/json;q=0.5"},
            ),
            200,
        ),
        (
            Case(f"{s}-get-missing", "GET", kind.path("999999999"), kind.setup(reader)),
            404,
        ),
        (
            Case(f"{s}-get-not-a-key", "GET", kind.path("x"), kind.setup(reader)),
            404,
        ),
        (
            Case(
                f"{s}-get-hidden", "GET", kind.path(), kind.setup(reader, context="b")
            ),
            404,
        ),
        (Case(f"{s}-head", "HEAD", kind.path(), kind.setup(reader)), 200),
        (
            Case(
                f"{s}-if-none-match",
                "GET",
                kind.path(),
                kind.setup(reader),
                headers=lambda keys: {
                    "If-None-Match": quote_etag(
                        Resource.objects.get(pk=keys["item"]).etag
                    )
                },
            ),
            304,
        ),
        (
            Case(
                f"{s}-if-modified-since",
                "GET",
                kind.path(),
                kind.setup(reader),
                headers={"If-Modified-Since": http_date(SETUP_TIME.timestamp())},
            ),
            304,
        ),
        (
            Case(
                f"{s}-if-modified-since-past",
                "GET",
                kind.path(),
                kind.setup(reader),
                headers={"If-Modified-Since": http_date(SETUP_TIME.timestamp() - 60)},
            ),
            200,
        ),
        (
            Case(
                f"{s}-put",
                "PUT",
                kind.path(),
                kind.setup(writer),
                body=kind.body("a"),
                headers=_etag("item"),
            ),
            200,
        ),
        (
            Case(
                f"{s}-put-without-if-match",
                "PUT",
                kind.path(),
                kind.setup(writer),
                body=kind.body("a"),
            ),
            428,
        ),
    ]


def _profile_write_cases():
    writer = {"http.GET": [PROFILES], "http.PUT": [PROFILES]}
    deleter = {"http.GET": [PROFILES], "http.DELETE": [PROFILES]}
    body = PROFILE.body("a")
    put = PROFILE.path()

    def in_use(client):
        _user(client, superuser=True)
        schema = registration.get_resource_type_for("partner-profiles").schema
        return {"item": schema.pk}

    return [
        (
            Case(
                "put-stale-if-match",
                "PUT",
                put,
                PROFILE.setup(writer),
                body=body,
                headers={"If-Match": '"stale"'},
            ),
            412,
        ),
        (
            Case(
                "put-malformed-if-match",
                "PUT",
                put,
                PROFILE.setup(writer),
                body=body,
                headers={"If-Match": "stale"},
            ),
            400,
        ),
        (
            Case(
                "put-failing-the-schema",
                "PUT",
                put,
                PROFILE.setup(writer),
                body={"display_name": 1, "rbac_context": "a"},
                headers=_etag("item"),
            ),
            400,
        ),
        (
            Case(
                "put-not-an-object",
                "PUT",
                put,
                # A list body writes the default context "", where a writer in
                # "a" may not write (403 before the 400).
                PROFILE.setup(superuser=True),
                body=[1, 2],
                headers=_etag("item"),
            ),
            400,
        ),
        (
            Case(
                "put-not-json",
                "PUT",
                put,
                PROFILE.setup(writer),
                body="{x",
                headers=_etag("item"),
            ),
            400,
        ),
        (
            Case(
                "put-empty",
                "PUT",
                put,
                PROFILE.setup(writer),
                body="",
                headers=_etag("item"),
            ),
            400,
        ),
        (
            Case(
                "put-too-deep",
                "PUT",
                put,
                PROFILE.setup(writer),
                body=_nested(120),
                headers=_etag("item"),
            ),
            400,
        ),
        (
            Case(
                "put-unwritable-context",
                "PUT",
                put,
                PROFILE.setup(writer),
                body=PROFILE.body("b"),
                headers=_etag("item"),
            ),
            403,
        ),
        (
            Case(
                "put-not-granted",
                "PUT",
                put,
                PROFILE.setup({"http.GET": [PROFILES]}),
                body=body,
                headers=_etag("item"),
            ),
            403,
        ),
        (
            Case(
                "put-hidden-stale-if-match",
                "PUT",
                put,
                # A body the writer may write: hidden, not refused.
                PROFILE.setup(writer, context="b"),
                body=body,
                headers={"If-Match": '"stale"'},
            ),
            404,
        ),
        (
            Case(
                "delete",
                "DELETE",
                put,
                PROFILE.setup(deleter),
                headers=_etag("item"),
            ),
            204,
        ),
        (
            Case("delete-without-if-match", "DELETE", put, PROFILE.setup(deleter)),
            428,
        ),
        (
            Case(
                "delete-in-use",
                "DELETE",
                SCHEMA.path(),
                in_use,
                headers=_etag("item"),
            ),
            409,
        ),
        (
            Case(
                "delete-hidden",
                "DELETE",
                put,
                PROFILE.setup(deleter, context="b"),
                headers=_etag("item"),
            ),
            404,
        ),
        (
            Case(
                "delete-hidden-stale-if-match",
                "DELETE",
                put,
                PROFILE.setup(deleter, context="b"),
                headers={"If-Match": '"stale"'},
            ),
            404,
        ),
        (
            Case(
                "patch",
                "PATCH",
                put,
                PROFILE.setup(writer),
                body=body,
                headers=_etag("item"),
            ),
            405,
        ),
    ]


def _workflow_cases():
    def may_transition(*transitions):
        return {
            "http.GET": [WORKFLOW_NOTE_TYPE_URI],
            "http.PUT": {
                WORKFLOW_NOTE_TYPE_URI: {
                    "condition": {
                        "operator": "scrud_workflow",
                        "authorized_transitions": list(transitions),
                    }
                }
            },
        }

    def setup(*transitions, content=DRAFT):
        def setup(client):
            registration.ResourceRegistration.register(
                dict(NOTE_POLICY), "workflow-policies"
            )
            note = registration.ResourceRegistration.register(
                dict(content), "workflow-notes"
            )
            _user(client, (may_transition(*transitions), "a"))
            return {"item": note.pk}

        return setup

    path = "{mount}workflow-notes/{item}/"
    return [
        (
            Case(
                "workflow-put-allowed",
                "PUT",
                path,
                setup("publish"),
                body=dict(PUBLISHED),
                headers=_etag("item"),
            ),
            200,
        ),
        (
            Case(
                "workflow-put-refused",
                "PUT",
                path,
                setup("retract"),
                body=dict(PUBLISHED),
                headers=_etag("item"),
            ),
            400,
        ),
        (
            Case(
                "workflow-put-no-transition",
                "PUT",
                path,
                setup("publish"),
                body={"state": "gone", "rbac_context": "a"},
                headers=_etag("item"),
            ),
            400,
        ),
    ]


ITEM_CASES = [
    *_read_cases(PROFILE),
    *_read_cases(SCHEMA),
    *_profile_write_cases(),
    *_workflow_cases(),
]


@pytest.mark.parametrize(
    "case, status",
    [pytest.param(case, status, id=case.id) for case, status in ITEM_CASES],
)
def test_item_routes_answer_alike(case, status):
    drf, ninja = parity(case)

    assert drf.status == status, drf
    assert drf == ninja


# The anonymous user's grants (``POLICY_FOR_UNAUTHENTICATED``): reading
# partner profiles, as a type an anonymous user may read.
ANONYMOUS_READS = {
    "": {
        "http.OPTIONS": True,
        "http.GET": [PROFILES, collection_type_uri_for(PROFILES)],
    }
}


def test_anonymous_granted_get_answers_alike():
    granted = {**settings.SCOPED_RBAC, "POLICY_FOR_UNAUTHENTICATED": ANONYMOUS_READS}
    case = Case(
        "anonymous-granted-get",
        "GET",
        PROFILE.path(),
        PROFILE.setup(context="", login=False),
    )
    with override_settings(SCOPED_RBAC=granted):
        drf, ninja = parity(case)

    assert drf.status == 200, drf
    assert drf == ninja


def test_anonymous_granted_nothing_answers_alike():
    # The tests' own policy for the anonymous grants partner profiles' OPTIONS
    # and POST only.
    for case in (
        Case(
            "anonymous-grants-nothing",
            "GET",
            PROFILE.path(),
            PROFILE.setup(context="", login=False),
        ),
        Case(
            "anonymous-grants-nothing-missing",
            "GET",
            PROFILE.path("999999999"),
            PROFILE.setup(context="", login=False),
        ),
    ):
        drf, ninja = parity(case)

        assert drf.status == 401, drf
        assert drf == ninja
