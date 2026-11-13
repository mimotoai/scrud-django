"""Parity: the DRF and the ninja front end, both mounted by
`tests.parity_urls` (``/drf/``, ``/ninja/``) over one database, answer each
case alike, normalized only as `tests.front_ends` says."""

import json
import os
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
from scrud_django.core.lists import list_etag
from scrud_django.models import Resource, ResourceType

from .front_ends import (
    HSR,
    MOUNTS,
    SETUP_TIME,
    Case,
    Exchange,
    normalized,
    parity,
    serve_both,
    split_links,
)
from .resource_types import PARTNER_PROFILES_TYPE, WORKFLOW_NOTE_TYPE_URI
from .test_core_workflow import DRAFT, NOTE_POLICY, PUBLISHED

pytestmark = [pytest.mark.urls("tests.parity_urls"), pytest.mark.django_db]


@pytest.fixture(autouse=True)
def _hsrs_built_here_are_forgotten():
    """pytest-django's ``urls`` mark sets ``ROOT_URLCONF`` without sending
    ``setting_changed``, on which scrudful forgets the DRF views' HTTP Schema
    Resources it built: those built here (linking under ``/drf/``) would be
    served to later tests of another URLconf. ``override_settings`` sends it
    as each test starts and ends."""
    with override_settings(ROOT_URLCONF="tests.parity_urls"):
        yield


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

    # Its list, filtered on content (the type's content_filter): the list's
    # ETag hashes the keys, so the note is stored with a key of its own.
    def list_setup(client):
        setup(client)
        note = store("hooked-notes", LIST_KEYS, {"text": "m", "rbac_context": "a"})
        return {"note": note.pk}

    drf, ninja = parity(
        Case("hooked-notes-list", "GET", "{mount}hooked-notes/?text=m", list_setup)
    )

    assert drf.status == 200, drf
    assert '"count":1' in drf.body, drf
    assert drf == ninja


def test_hooked_types_are_served_alike_on_a_processs_first_parity_call():
    # The test above, alone in a fresh process: its parity call is the first,
    # before the URLconf was imported.
    test = "tests/test_front_end_parity.py::test_hooked_types_are_served_alike_in_both_runs"
    # On a database server, a test database of its own: this session's
    # (test_scrud) is open, and a second session creating it would clash.
    env = dict(os.environ)
    for engine in ("PG", "MYSQL"):
        name = os.environ.get(f"SCRUD_TEST_{engine}_NAME", "scrud")
        env[f"SCRUD_TEST_{engine}_NAME"] = f"{name}_first_parity_call"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--create-db",
            test,
        ],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        env=env,
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


# The list route (GET, HEAD, POST) and the collection documents its Links
# name. A list's ETag hashes its resources' keys: the setups store them with
# keys of their own (`store`), the same in both runs.

#: The keys of the resources a list case stores, in order.
LIST_KEYS = range(970001, 970100)

LIST = "{mount}partner-profiles/"
PROFILES_LIST = collection_type_uri_for(PROFILES)
LIST_READER = {"http.GET": [PROFILES, PROFILES_LIST]}
CREATOR = {"http.GET": [PROFILES, PROFILES_LIST], "http.POST": [PROFILES]}


def store(slug, keys, content):
    """A resource of `slug` whose content is `content`, stored with the first
    of `keys` no resource has."""
    taken = set(Resource.objects.values_list("pk", flat=True))
    key = next(key for key in keys if key not in taken)
    resource_type = registration.get_resource_type_for(slug)
    return registration.ResourceRegistration.register_instance(
        Resource(pk=key, content=content, resource_type=resource_type)
    )


def _listing(*grants, contexts=("a",), superuser=False, login=True):
    """A setup storing a partner profile in each of `contexts` (``p0``,
    ``p1`` ...) and logging in a user holding `grants` (each in context
    "a")."""

    def setup(client):
        keys = {}
        for index, context in enumerate(contexts):
            profile = store(
                "partner-profiles",
                LIST_KEYS,
                {"name": f"p{index}", "rbac_context": context},
            )
            keys[f"p{index}"] = profile.pk
        if login:
            _user(client, *((g, "a") for g in grants), superuser=superuser)
        return keys

    return setup


def _list_etag(query=()):
    """The headers of a request whose ``{header}`` is the ETag of the partner
    profiles list as a superuser gets it (every profile, one page), with the
    query parameters `query`."""

    def etag(keys):
        resource_type = registration.get_resource_type_for("partner-profiles")
        items = list(Resource.objects.filter(resource_type=resource_type))
        return quote_etag(
            list_etag(resource_type, items=items, count=len(items), query=list(query))
        )

    return etag


def _with_list_etag(header, query=()):
    etag = _list_etag(query)
    return lambda keys: {header: etag(keys)}


def _notes(client):
    _user(client, superuser=True)
    keys = {}
    for index, text in enumerate(("n", "m", "n")):
        note = store("hooked-notes", LIST_KEYS, {"text": text, "rbac_context": "a"})
        keys[f"n{index}"] = note.pk
    return keys


def _workflow_creator(client):
    registration.ResourceRegistration.register(dict(NOTE_POLICY), "workflow-policies")
    _user(
        client,
        (
            {
                "http.GET": [WORKFLOW_NOTE_TYPE_URI],
                "http.POST": [WORKFLOW_NOTE_TYPE_URI],
            },
            "a",
        ),
    )
    return {}


ADMIN_LIST = _listing(contexts=("a", "b"), superuser=True)
THREE = _listing(contexts=("a", "a", "a"), superuser=True)
BODY = {"name": "y", "rbac_context": "a"}

LIST_CASES = [
    (Case("list-admin", "GET", LIST, ADMIN_LIST), 200),
    (
        Case("list-reader", "GET", LIST, _listing(LIST_READER, contexts=("a", "b"))),
        200,
    ),
    (
        Case(
            "list-item-grant-only",
            "GET",
            LIST,
            _listing({"http.GET": [PROFILES]}),
        ),
        403,
    ),
    (Case("list-anonymous", "GET", LIST, _listing(login=False)), 401),
    (
        Case(
            "list-json",
            "GET",
            LIST,
            ADMIN_LIST,
            headers={"Accept": "application/json"},
        ),
        200,
    ),
    (Case("list-page", "GET", LIST + "?page=2&page_size=1", THREE), 200),
    (Case("list-page-first", "GET", LIST + "?page=1&page_size=2", THREE), 200),
    (Case("list-page-last", "GET", LIST + "?page=last&page_size=2", THREE), 200),
    (Case("list-page-blank", "GET", LIST + "?page=&page_size=2", THREE), 200),
    (Case("list-page-0", "GET", LIST + "?page=0", THREE), 404),
    (Case("list-page-x", "GET", LIST + "?page=x", THREE), 404),
    (Case("list-page-99", "GET", LIST + "?page=99", THREE), 404),
    (Case("list-page-size-0", "GET", LIST + "?page_size=0", THREE), 200),
    (Case("list-page-size-x", "GET", LIST + "?page_size=x", THREE), 200),
    (Case("list-page-size-5000", "GET", LIST + "?page_size=5000", THREE), 200),
    (Case("list-empty", "GET", LIST, _listing(contexts=(), superuser=True)), 200),
    (
        Case(
            "list-empty-page-2",
            "GET",
            LIST + "?page=2",
            _listing(contexts=(), superuser=True),
        ),
        404,
    ),
    (
        Case(
            "list-if-none-match",
            "GET",
            LIST,
            ADMIN_LIST,
            headers=_with_list_etag("If-None-Match"),
        ),
        304,
    ),
    (
        Case(
            "list-if-none-match-other",
            "GET",
            LIST,
            ADMIN_LIST,
            headers={"If-None-Match": '"other"'},
        ),
        200,
    ),
    (
        Case(
            "list-if-modified-since",
            "GET",
            LIST,
            ADMIN_LIST,
            headers={"If-Modified-Since": http_date(SETUP_TIME.timestamp())},
        ),
        304,
    ),
    (
        Case(
            "list-if-match-stale",
            "GET",
            LIST,
            ADMIN_LIST,
            headers={"If-Match": '"stale"'},
        ),
        412,
    ),
    (
        Case(
            "list-if-none-match-past-the-last-page",
            "GET",
            LIST + "?page=99",
            ADMIN_LIST,
            headers={"If-None-Match": "*"},
        ),
        404,
    ),
    (Case("list-head", "HEAD", LIST, ADMIN_LIST), 200),
    (
        Case(
            "list-head-if-none-match",
            "HEAD",
            LIST,
            ADMIN_LIST,
            headers=_with_list_etag("If-None-Match"),
        ),
        304,
    ),
    # A content filter (hooked-notes is registered with content_filter=True).
    (Case("list-filtered", "GET", "{mount}hooked-notes/?text=n", _notes), 200),
    (
        Case(
            "list-filtered-paged",
            "GET",
            "{mount}hooked-notes/?text=n&page=2&page_size=1&format=json&ui-mode=view",
            _notes,
        ),
        200,
    ),
    (Case("list-filter-integer", "GET", "{mount}hooked-notes/?1=x", _notes), 400),
    (Case("list-filter-nul", "GET", "{mount}hooked-notes/?text=%00", _notes), 400),
    # A computed type has no list.
    (Case("list-computed", "GET", "{mount}summaries/", ADMIN_LIST), 404),
    # Create.
    (Case("post", "POST", LIST, _listing(CREATOR), body=BODY), 201),
    (
        Case(
            "post-failing-the-schema",
            "POST",
            LIST,
            _listing(CREATOR),
            body={"display_name": 1, "rbac_context": "a"},
        ),
        400,
    ),
    (
        Case(
            "post-unwritable-context",
            "POST",
            LIST,
            _listing(CREATOR),
            body={"name": "y", "rbac_context": "b"},
        ),
        403,
    ),
    (Case("post-not-granted", "POST", LIST, _listing(LIST_READER), body=BODY), 403),
    (
        Case(
            "post-anonymous",
            "POST",
            LIST,
            _listing(login=False),
            body={"name": "y", "rbac_context": ""},
        ),
        201,
    ),
    (
        Case(
            "post-anonymous-other-context",
            "POST",
            LIST,
            _listing(login=False),
            body=BODY,
        ),
        401,
    ),
    (Case("post-not-json", "POST", LIST, _listing(CREATOR), body="{x"), 400),
    (Case("post-empty", "POST", LIST, _listing(CREATOR), body=""), 400),
    (
        Case(
            "post-not-an-object",
            "POST",
            LIST,
            _listing(superuser=True),
            body=[1, 2],
        ),
        400,
    ),
    (
        Case(
            "post-if-none-match-star",
            "POST",
            LIST,
            ADMIN_LIST,
            body=BODY,
            headers={"If-None-Match": "*"},
        ),
        412,
    ),
    (
        Case(
            "post-if-none-match-other",
            "POST",
            LIST,
            ADMIN_LIST,
            body=BODY,
            headers={"If-None-Match": '"other"'},
        ),
        201,
    ),
    (
        Case(
            "post-if-none-match-current",
            "POST",
            LIST,
            ADMIN_LIST,
            body=BODY,
            headers=_with_list_etag("If-None-Match"),
        ),
        412,
    ),
    (
        Case(
            "post-if-match",
            "POST",
            LIST,
            ADMIN_LIST,
            body=BODY,
            headers=_with_list_etag("If-Match"),
        ),
        201,
    ),
    (
        Case(
            "post-if-match-paged",
            "POST",
            LIST + "?page_size=5000",
            ADMIN_LIST,
            body=BODY,
            headers=_with_list_etag("If-Match", [("page_size", ["5000"])]),
        ),
        201,
    ),
    (
        Case(
            "post-if-match-stale",
            "POST",
            LIST,
            ADMIN_LIST,
            body=BODY,
            headers={"If-Match": '"stale"'},
        ),
        412,
    ),
    (
        Case(
            "post-if-match-malformed",
            "POST",
            LIST,
            ADMIN_LIST,
            body=BODY,
            headers={"If-Match": "stale"},
        ),
        400,
    ),
    (
        Case(
            "post-if-match-star",
            "POST",
            LIST,
            ADMIN_LIST,
            body=BODY,
            headers={"If-Match": "*"},
        ),
        201,
    ),
    (
        Case(
            "post-if-unmodified-since",
            "POST",
            LIST,
            ADMIN_LIST,
            body=BODY,
            headers={"If-Unmodified-Since": http_date(SETUP_TIME.timestamp() - 60)},
        ),
        412,
    ),
    (
        Case(
            "post-if-match-past-the-last-page",
            "POST",
            LIST + "?page=99",
            ADMIN_LIST,
            body=BODY,
            headers={"If-Match": '"stale"'},
        ),
        404,
    ),
    (
        Case(
            "workflow-post-allowed",
            "POST",
            "{mount}workflow-notes/",
            _workflow_creator,
            body=dict(DRAFT),
        ),
        201,
    ),
    (
        Case(
            "workflow-post-refused",
            "POST",
            "{mount}workflow-notes/",
            _workflow_creator,
            body=dict(PUBLISHED),
        ),
        400,
    ),
    # The collection documents a list's Links name.
    *(
        (
            Case(
                f"{name}-{accept_id}",
                "GET",
                "{mount}" + f"{name}/partner-profiles/",
                headers={"Accept": accept},
            ),
            200,
        )
        for name in ("collections-json-schema", "collections-json-ld")
        for accept_id, accept in (("any", "*/*"), ("json", "application/json"))
    ),
    *(
        (Case(f"{name}-unknown", "GET", "{mount}" + f"{name}/no-such-type/"), 404)
        for name in ("collections-json-schema", "collections-json-ld")
    ),
]


@pytest.mark.parametrize(
    "case, status",
    [pytest.param(case, status, id=case.id) for case, status in LIST_CASES],
)
def test_list_routes_answer_alike(case, status):
    drf, ninja = parity(case)

    assert drf.status == status, drf
    assert drf == ninja


def test_list_links_name_the_collection_documents_of_the_serving_mount():
    drf, ninja = parity(Case("list-links", "GET", LIST, ADMIN_LIST))

    assert drf == ninja
    links = split_links(ninja.headers["Link"])
    assert links[:2] == [
        "<http://testserver{mount}collections-json-schema/partner-profiles/>; "
        'rel="describedby"; type="application/schema+json"',
        "<http://testserver{mount}collections-json-ld/partner-profiles/>; "
        'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"',
    ]
    assert links[2].startswith(f"<{HSR}>")


# What the harness normalizes away, compared directly: the HTTP Schema
# Resource a response links to, and Vary.


def _hsr_of(response):
    (link,) = [
        link for link in split_links(response["Link"]) if f'rel="{HSR_REL}"' in link
    ]
    return link[1 : link.index(">")]


def test_each_front_ends_hsr_links_to_its_own_routes(client):
    serve_both()
    _user(client, superuser=True)
    profile = PROFILE.store()
    rtype = registration.get_resource_type_for("partner-profiles")
    for front_end, other in (("ninja", "drf"), ("drf", "ninja")):
        mount = MOUNTS[front_end]
        response = client.get(f"{mount}partner-profiles/{profile.pk}/")
        assert response.status_code == 200, response.content
        document = client.get(_hsr_of(response))
        assert document.status_code == 200, document.content
        text = document.content.decode()
        assert f"{mount}json-schema/{rtype.schema.pk}/" in text, text
        assert f"{mount}json-ld/{rtype.context.pk}/" in text, text
        assert MOUNTS[other] not in text, text


def _varies(client, method, path, **kwargs):
    """The Vary of `method` `path` on each front end, by front end."""
    return {
        front_end: client.generic(method, path.replace("{mount}", mount), **kwargs).get(
            "Vary"
        )
        for front_end, mount in MOUNTS.items()
    }


@pytest.mark.parametrize("kind", [SCHEMA, PROFILE], ids=lambda kind: kind.slug)
def test_item_routes_vary_on_accept_alike(client, kind):
    serve_both()
    _user(client, superuser=True)
    item = kind.store()
    path = kind.path(item.pk)
    etag = quote_etag(item.etag)
    body = json.dumps(kind.body("a"))
    json_body = {"data": body, "content_type": "application/json"}
    asked = {
        "get": _varies(client, "GET", path),
        "get-json": _varies(
            client, "GET", path, headers={"Accept": "application/json"}
        ),
        "head": _varies(client, "HEAD", path),
        "missing": _varies(client, "GET", kind.path("999999999")),
        "not-modified": _varies(client, "GET", path, headers={"If-None-Match": etag}),
        "stale": _varies(
            client, "PUT", path, headers={"If-Match": '"stale"'}, **json_body
        ),
        "unconditional": _varies(client, "PUT", path, **json_body),
        "delete-stale": _varies(
            client, "DELETE", path, headers={"If-Match": '"stale"'}
        ),
        "patch": _varies(client, "PATCH", path, **json_body),
        "options": _varies(client, "OPTIONS", path),
    }
    # A PUT that writes, once on each front end.
    written = {}
    for front_end, mount in MOUNTS.items():
        current = quote_etag(Resource.objects.get(pk=item.pk).etag)
        response = client.put(
            path.replace("{mount}", mount),
            headers={"If-Match": current},
            **json_body,
        )
        assert response.status_code == 200, response.content
        written[front_end] = response.get("Vary")
    asked["put"] = written

    for name, varies in asked.items():
        assert varies["drf"] == varies["ninja"], (name, varies)
        # DRF negotiates among several renderers: every answer varies on
        # Accept (a document type's representation does depend on it).
        assert "Accept" in varies["drf"], (name, varies)


def test_list_routes_vary_on_accept_alike(client):
    serve_both()
    _user(client, superuser=True)
    PROFILE.store()
    json_body = {"data": json.dumps(BODY), "content_type": "application/json"}
    asked = {
        "get": _varies(client, "GET", LIST),
        "get-json": _varies(
            client, "GET", LIST, headers={"Accept": "application/json"}
        ),
        "head": _varies(client, "HEAD", LIST),
        "page-0": _varies(client, "GET", LIST + "?page=0"),
        "not-modified": _varies(
            client,
            "GET",
            LIST,
            headers={"If-Modified-Since": http_date(SETUP_TIME.timestamp() + 3600)},
        ),
        "filter-integer": _varies(client, "GET", "{mount}hooked-notes/?1=x"),
        "post": _varies(client, "POST", LIST, **json_body),
        "post-stale": _varies(
            client, "POST", LIST, headers={"If-Match": '"stale"'}, **json_body
        ),
        "post-failing-the-schema": _varies(
            client,
            "POST",
            LIST,
            data=json.dumps({"display_name": 1}),
            content_type="application/json",
        ),
        "put": _varies(client, "PUT", LIST, **json_body),
        **{
            f"{name}-{accept}": _varies(
                client,
                "GET",
                "{mount}" + f"{name}/{slug}/",
                headers={"Accept": accept},
            )
            for name in ("collections-json-schema", "collections-json-ld")
            for slug in ("partner-profiles", "no-such-type")
            for accept in ("*/*", "application/json")
        },
    }

    for name, varies in asked.items():
        assert varies["drf"] == varies["ninja"], (name, varies)
        assert "Accept" in varies["drf"], (name, varies)


def test_each_front_ends_list_hsr_links_to_its_own_routes(client):
    serve_both()
    _user(client, superuser=True)
    rtype = registration.get_resource_type_for("partner-profiles")
    for front_end, other in (("ninja", "drf"), ("drf", "ninja")):
        mount = MOUNTS[front_end]
        response = client.get(f"{mount}partner-profiles/")
        assert response.status_code == 200, response.content
        document = client.get(_hsr_of(response))
        assert document.status_code == 200, document.content
        text = document.content.decode()
        for target in (
            f"{mount}collections-json-schema/partner-profiles/",
            f"{mount}collections-json-ld/partner-profiles/",
            # The create's answer is the new resource's.
            f"{mount}json-schema/{rtype.schema.pk}/",
            f"{mount}json-ld/{rtype.context.pk}/",
        ):
            assert target in text, (target, text)
        assert MOUNTS[other] not in text, text


# OPTIONS: the type routes answer scrud's own OPTIONS (the core's
# `allowed_methods_for`), a CORS preflight a bare 204; the collection
# documents answer DRF's metadata.

#: The requesters of the OPTIONS cases: a role definition to hold in context
#: "a" (``None``: none), a superuser, or no login.
OPTIONS_REQUESTERS = {
    "admin": {"superuser": True},
    "reader": {"grants": [LIST_READER]},
    "nothing": {},
    "anonymous": {"login": False},
}
PREFLIGHT = {"Origin": "http://example.com", "Access-Control-Request-Method": "PUT"}


def _options_cases():
    cases = []
    for name, requester in OPTIONS_REQUESTERS.items():
        grants = requester.get("grants", [])
        superuser = requester.get("superuser", False)
        login = requester.get("login", True)
        cases.append(
            Case(
                f"options-list-{name}",
                "OPTIONS",
                LIST,
                _listing(*grants, superuser=superuser, login=login),
            )
        )
        for target, context, key in (
            ("readable", "a", "{item}"),
            ("hidden", "b", "{item}"),
            ("missing", "a", "999999999"),
            ("not-a-key", "a", "x"),
        ):
            cases.append(
                Case(
                    f"options-{target}-{name}",
                    "OPTIONS",
                    PROFILE.path(key),
                    PROFILE.setup(
                        *grants, context=context, superuser=superuser, login=login
                    ),
                )
            )
    for path in (LIST, PROFILE.path()):
        cases.append(
            Case(
                f"preflight-{'list' if path == LIST else 'item'}",
                "OPTIONS",
                path,
                PROFILE.setup(superuser=True),
                headers=PREFLIGHT,
            )
        )
    for name in ("collections-json-schema", "collections-json-ld"):
        for slug in ("partner-profiles", "no-such-type"):
            cases.append(
                Case(f"options-{name}-{slug}", "OPTIONS", "{mount}" + f"{name}/{slug}/")
            )
        document = "{mount}" + f"{name}/partner-profiles/"
        cases.extend(
            [
                Case(
                    f"options-{name}-json",
                    "OPTIONS",
                    document,
                    headers={"Accept": "application/json"},
                ),
                Case(f"preflight-{name}", "OPTIONS", document, headers=PREFLIGHT),
                *(
                    Case(f"{method.lower()}-{name}", method, document, body={})
                    for method in ("POST", "PUT", "PATCH", "DELETE")
                ),
            ]
        )
    return cases


@pytest.mark.parametrize("case", _options_cases(), ids=lambda case: case.id)
def test_options_answer_alike(case):
    drf, ninja = parity(case)

    assert drf == ninja


def test_options_answers_are_drfs():
    """What the OPTIONS cases answer on DRF (`test_options_answer_alike`
    compares ninja's with them): pinned, so that the corpus keeps covering
    each kind of answer."""
    answers = {}
    for case in _options_cases():
        drf, _ = parity(case)
        answers[case.id] = (drf.status, drf.headers.get("Allow"))
    every = "GET, PUT, DELETE, OPTIONS, HEAD"
    document = (200, "GET, HEAD, OPTIONS")
    refused = (405, "GET, HEAD, OPTIONS")
    assert answers == {
        "options-list-admin": (204, "GET, POST, OPTIONS, HEAD"),
        "options-readable-admin": (204, every),
        "options-hidden-admin": (204, every),
        "options-missing-admin": (404, None),
        "options-not-a-key-admin": (404, None),
        "options-list-reader": (204, "GET, OPTIONS, HEAD"),
        "options-readable-reader": (204, "GET, OPTIONS, HEAD"),
        "options-hidden-reader": (404, None),
        "options-missing-reader": (404, None),
        "options-not-a-key-reader": (404, None),
        **{
            f"options-{target}-nothing": (204, "OPTIONS")
            for target in ("list", "readable", "hidden", "missing", "not-a-key")
        },
        # The tests' policy for the anonymous grants partner profiles' POST.
        "options-list-anonymous": (204, "POST, OPTIONS"),
        **{
            f"options-{target}-anonymous": (204, "OPTIONS")
            for target in ("readable", "hidden", "missing", "not-a-key")
        },
        "preflight-list": (204, None),
        "preflight-item": (204, None),
        **{
            f"{case}-{name}{suffix}": answer
            for name in ("collections-json-schema", "collections-json-ld")
            for case, suffix, answer in (
                ("options", "-partner-profiles", document),
                ("options", "-no-such-type", document),
                ("options", "-json", document),
                ("preflight", "", document),
                ("post", "", refused),
                ("put", "", refused),
                ("patch", "", refused),
                ("delete", "", refused),
            )
        },
    }, answers


def test_options_missing_and_existing_answer_alike():
    """A requester granted nothing learns nothing of a key from OPTIONS: an
    existing item, a hidden one and a missing key answer alike, on both
    front ends."""

    def setup(context):
        def setup(client):
            item = PROFILE.store(context)
            _user(client)
            return {"item": item.pk}

        return setup

    existing = parity(Case("options-existing", "OPTIONS", PROFILE.path(), setup("a")))
    hidden = parity(Case("options-hidden", "OPTIONS", PROFILE.path(), setup("b")))
    missing = parity(
        Case("options-missing", "OPTIONS", PROFILE.path("999999999"), setup("a"))
    )

    for drf, ninja in (existing, hidden, missing):
        assert drf == ninja
    assert existing[0].status == 204, existing[0]
    assert existing[0].headers["Allow"] == "OPTIONS"
    assert existing == hidden == missing


def test_options_resolves_the_user_as_other_methods_do(client):
    """The ninja OPTIONS resolves its requester as every other method
    (``scoped_rbac.ninja.resolve_user``): through ``SCOPED_RBAC["NINJA_USER"]``
    when it is set, the session's user otherwise."""
    serve_both()
    admin = User.objects.create(username="admin", is_staff=True, is_superuser=True)
    item = PROFILE.store()
    url = f"{MOUNTS['ninja']}partner-profiles/{item.pk}/"
    every = "GET, PUT, DELETE, OPTIONS, HEAD"

    response = client.options(url)
    assert (response.status_code, response["Allow"]) == (204, "OPTIONS")
    mapped = {**settings.SCOPED_RBAC, "NINJA_USER": lambda request: admin}
    with override_settings(SCOPED_RBAC=mapped):
        response = client.options(url)
        list_response = client.options(f"{MOUNTS['ninja']}partner-profiles/")
    assert (response.status_code, response["Allow"]) == (204, every)
    assert list_response["Allow"] == "GET, POST, OPTIONS, HEAD"
    # The session's user, as on DRF.
    client.force_login(admin)
    for mount in MOUNTS.values():
        response = client.options(f"{mount}partner-profiles/{item.pk}/")
        assert (response.status_code, response["Allow"]) == (204, every)


# Uniform errors off (SCOPED_RBAC["UNIFORM_ERRORS"]) for a request: each front
# end reads it per request, so an API built with it on answers natively.

WRITER = {"http.GET": [PROFILES], "http.PUT": [PROFILES]}
UNIFORM_CASES = [
    Case(
        "stale-if-match",
        "PUT",
        PROFILE.path(),
        PROFILE.setup(WRITER),
        body=PROFILE.body("a"),
        headers={"If-Match": '"stale"'},
    ),
    Case(
        "without-if-match",
        "PUT",
        PROFILE.path(),
        PROFILE.setup(WRITER),
        body={"name": "y"},
    ),
    Case(
        "malformed-if-match",
        "PUT",
        PROFILE.path(),
        PROFILE.setup(WRITER),
        body=PROFILE.body("a"),
        headers={"If-Match": "stale"},
    ),
    Case(
        "document-stale-if-match",
        "PUT",
        SCHEMA.path(),
        SCHEMA.setup(superuser=True),
        body=SCHEMA.body("a"),
        headers={"If-Match": '"stale"'},
    ),
    Case(
        "not-an-object",
        "PUT",
        PROFILE.path(),
        PROFILE.setup(superuser=True),
        body=[1],
        headers=_etag("item"),
    ),
    Case(
        "failing-the-schema",
        "PUT",
        PROFILE.path(),
        PROFILE.setup(WRITER),
        body={"rbac_context": "a"},
        headers=_etag("item"),
    ),
    Case(
        "not-json",
        "PUT",
        PROFILE.path(),
        PROFILE.setup(WRITER),
        body="{x",
        headers=_etag("item"),
    ),
    Case(
        "not-a-json-media-type",
        "PUT",
        PROFILE.path(),
        PROFILE.setup(WRITER),
        body=b"x",
        headers=lambda keys: {"Content-Type": "text/plain", **_etag("item")(keys)},
    ),
    Case("patch", "PATCH", PROFILE.path(), PROFILE.setup(WRITER), body={}),
    Case("forbidden", "GET", PROFILE.path(), PROFILE.setup()),
    Case("missing", "GET", PROFILE.path("999999999"), PROFILE.setup(superuser=True)),
    Case("anonymous", "GET", PROFILE.path(), PROFILE.setup(login=False)),
    Case(
        "not-modified",
        "GET",
        PROFILE.path(),
        PROFILE.setup(superuser=True),
        headers=lambda keys: {
            "If-None-Match": quote_etag(Resource.objects.get(pk=keys["item"]).etag)
        },
    ),
    *(
        case
        for case, _ in LIST_CASES
        if case.id
        in {
            "list-page-0",
            "list-anonymous",
            "list-item-grant-only",
            "list-if-match-stale",
            "list-filter-integer",
            "list-filter-nul",
            "post-failing-the-schema",
            "post-not-an-object",
            "post-not-json",
            "post-if-none-match-star",
            "post-if-match-malformed",
            "collections-json-schema-unknown",
            "collections-json-ld-unknown",
        }
    ),
]

# scrud's own errors (a schema's) are problem details either way.
OWN_ERRORS = {"failing-the-schema", "post-failing-the-schema"}


@pytest.mark.parametrize("uniform", [True, False], ids=["uniform", "native"])
@pytest.mark.parametrize("case", UNIFORM_CASES, ids=lambda case: case.id)
def test_errors_follow_uniform_errors_per_request_alike(case, uniform):
    # The ninja API is built (with uniform errors on, the tests' setting)
    # before the setting changes.
    serve_both()
    setting = dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=uniform)
    with override_settings(SCOPED_RBAC=setting):
        drf, ninja = parity(case)

    assert drf == ninja
    if drf.status >= 400 and case.id not in OWN_ERRORS:
        expected = "application/problem+json" if uniform else "application/json"
        assert drf.headers["Content-Type"] == expected, drf
