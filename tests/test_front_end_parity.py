"""Parity: the DRF and the ninja front end, both mounted by
`tests.parity_urls` (``/drf/``, ``/ninja/``) over one database, answer each
case alike, normalized only as `tests.front_ends` says."""

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.http import HttpResponse
from django.test import Client, override_settings
from django.urls import Resolver404, resolve
from django.utils.http import http_date, quote_etag
from ninja.errors import AuthenticationError
from scoped_rbac.models import Role, RoleAssignment

from scrud_django import ScrudServices, collection_type_uri_for, registration
from scrud_django.computed import computed_etag
from scrud_django.core.lists import list_etag
from scrud_django.models import Resource, ResourceType

from . import front_ends
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
from .resource_types import (
    PARTNER_PROFILES_TYPE,
    WORKFLOW_NOTE_TYPE_URI,
    encode_summary,
)
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


def test_the_harness_refuses_runs_choosing_different_keys():
    chosen = iter([970001, 970002])

    def setup(client):
        front_ends.note_chosen_key(next(chosen))
        return {}

    with pytest.raises(AssertionError, match="chose different keys"):
        parity(Case("drifting", "GET", "{mount}services/", setup))


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


def test_unknown_route_is_404_on_both():
    drf, ninja = parity(Case("no-such-type", "GET", "{mount}no-such-type/"))

    assert drf.status == ninja.status == 404


# The item routes (GET, HEAD, PUT, DELETE; PATCH is not routed), on a plain
# type (partner-profiles) and on a document type (json-schema, served as
# application/schema+json).

PROFILES = "tests://PartnerProfiles"
WORKFLOW_POLICY = "http://api.openteams.com/json-ld/WorkflowPolicy"
JSON_SCHEMA = "http://json-schema.org/draft-04/schema"
JSON_LD = "http://www.w3.org/ns/json-ld#context"


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
    """A type the item corpus runs on: its slug and IRI, the content of a
    resource stored in a context, and the media type its documents are
    served as (asked for by the ``-get-own-type`` case)."""

    def __init__(self, slug, iri, content, body, own_type="application/schema+json"):
        self.slug = slug
        self.iri = iri
        self.content = content
        self.body = body
        self.own_type = own_type

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
LD = ItemType(
    "json-ld",
    JSON_LD,
    lambda context: {
        "@context": {"x": "http://example.org/x"},
        "rbac_context": context,
    },
    lambda context: {
        "@context": {"y": "http://example.org/y"},
        "rbac_context": context,
    },
    own_type="application/ld+json",
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
                headers={"Accept": f"{kind.own_type}, application/json;q=0.5"},
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

    def setup(*transitions, content=DRAFT, read_policies=False):
        def setup(client):
            policy = registration.ResourceRegistration.register(
                dict(NOTE_POLICY), "workflow-policies"
            )
            note = registration.ResourceRegistration.register(
                dict(content), "workflow-notes"
            )
            grants = [(may_transition(*transitions), "a")]
            if read_policies:
                grants.append(({"http.GET": [WORKFLOW_POLICY]}, "a"))
            _user(client, *grants)
            return {"item": note.pk, "policy": policy.pk}

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
            # A requester who may read the policy is told its URL: the
            # serving API's (a stored policy's URL reversed globally only
            # named DRF's route on ninja).
            Case(
                "workflow-put-refused-policy-readable",
                "PUT",
                path,
                setup("retract", read_policies=True),
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


def _shared_policy_cases():
    """A ``WorkflowPolicy`` stored (server-side) in two contexts governs
    both: updating or deleting it needs the write in each."""
    policy = {
        "resource_type": WORKFLOW_NOTE_TYPE_URI,
        "initial_states": {"any": "true"},
        "states": {},
        "transitions": {},
    }

    def setup(*contexts):
        def setup(client):
            shared = registration.ResourceRegistration.register(
                dict(policy, rbac_context=["a", "b"]), "workflow-policies"
            )
            methods = ("http.GET", "http.PUT", "http.DELETE")
            _user(
                client,
                *(({m: [WORKFLOW_POLICY] for m in methods}, c) for c in contexts),
            )
            return {"policy": shared.pk}

        return setup

    path = "{mount}workflow-policies/{policy}/"
    return [
        (
            # The update was not checked in every context on ninja.
            Case(
                "shared-policy-put-by-one-context",
                "PUT",
                path,
                setup("a"),
                body=dict(policy, rbac_context="a"),
                headers=_etag("policy"),
            ),
            403,
        ),
        (
            Case(
                "shared-policy-put-by-every-context",
                "PUT",
                path,
                setup("a", "b"),
                body=dict(policy, rbac_context="a"),
                headers=_etag("policy"),
            ),
            200,
        ),
        (
            Case(
                "shared-policy-delete-by-one-context",
                "DELETE",
                path,
                setup("a"),
                headers=_etag("policy"),
            ),
            403,
        ),
    ]


ITEM_CASES = [
    *_read_cases(PROFILE),
    *_read_cases(SCHEMA),
    *_read_cases(LD),
    *_profile_write_cases(),
    *_workflow_cases(),
    *_shared_policy_cases(),
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
    of `keys` no resource has (noted for `parity`,
    `front_ends.note_chosen_key`)."""
    taken = set(Resource.objects.values_list("pk", flat=True))
    key = next(key for key in keys if key not in taken)
    front_ends.note_chosen_key(key)
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


# A computed type (``summaries``, computed by its registered ``compute``): its
# item route only, open to an authenticated requester (DRF's
# ``IsAuthenticated``), answered with the content's ETag and the type's
# schema and context Links; every other method a 405 (after authentication).

COMPUTED = "{mount}summaries/k1/"
ENCODED = "{mount}encoded-summaries/k1/"


def _computed_etag(keys):
    """The headers of a request carrying `If-None-Match` with the ETag of
    ``summaries/k1/`` as the user the setup logged in (the last) gets it."""
    user = User.objects.latest("pk")
    return {"If-None-Match": computed_etag({"key": "k1", "user": user.get_username()})}


def _admin(client):
    _user(client, superuser=True)
    return {}


def _no_grants(client):
    _user(client)
    return {}


COMPUTED_CASES = [
    (Case("computed-get-admin", "GET", COMPUTED, _admin), 200),
    (Case("computed-get-no-grants", "GET", COMPUTED, _no_grants), 200),
    (Case("computed-get-anonymous", "GET", COMPUTED), 401),
    (
        Case(
            "computed-get-json",
            "GET",
            COMPUTED,
            _admin,
            headers={"Accept": "application/json"},
        ),
        200,
    ),
    (Case("computed-head", "HEAD", COMPUTED, _admin), 200),
    (
        Case("computed-if-none-match", "GET", COMPUTED, _admin, headers=_computed_etag),
        304,
    ),
    (
        Case(
            "computed-head-if-none-match",
            "HEAD",
            COMPUTED,
            _admin,
            headers=_computed_etag,
        ),
        304,
    ),
    (
        Case(
            "computed-if-none-match-other",
            "GET",
            COMPUTED,
            _admin,
            headers={"If-None-Match": '"other"'},
        ),
        200,
    ),
    (
        Case(
            "computed-if-none-match-star",
            "GET",
            COMPUTED,
            _admin,
            headers={"If-None-Match": "*"},
        ),
        304,
    ),
    (Case("computed-options", "OPTIONS", COMPUTED, _admin), 200),
    (Case("computed-options-anonymous", "OPTIONS", COMPUTED), 401),
    (Case("preflight-computed", "OPTIONS", COMPUTED, _admin, headers=PREFLIGHT), 200),
    (Case("preflight-computed-anonymous", "OPTIONS", COMPUTED, headers=PREFLIGHT), 401),
    *(
        (Case(f"computed-{method.lower()}", method, COMPUTED, _admin, body={}), 405)
        for method in ("POST", "PUT", "PATCH", "DELETE")
    ),
    (Case("computed-put-anonymous", "PUT", COMPUTED, body={}), 401),
    (Case("computed-delete-anonymous", "DELETE", COMPUTED), 401),
    # Content that is not all JSON (a datetime with microseconds, a decimal,
    # a duration, a set), written as DRF writes it.
    (Case("computed-encoded", "GET", ENCODED, _admin), 200),
    (Case("computed-encoded-head", "HEAD", ENCODED, _admin), 200),
    (
        Case(
            "computed-encoded-if-none-match",
            "GET",
            ENCODED,
            _admin,
            headers={"If-None-Match": computed_etag(encode_summary(None, "k1"))},
        ),
        304,
    ),
]


@pytest.mark.parametrize(
    "case, status",
    [pytest.param(case, status, id=case.id) for case, status in COMPUTED_CASES],
)
def test_computed_routes_answer_alike(case, status):
    drf, ninja = parity(case)

    assert drf.status == status, drf
    assert drf == ninja


# A computed type's ``compute`` may raise ``PermissionDenied`` or ``Http404``
# (``refusing-summaries``, `tests.resource_types.refuse_summary`): DRF
# answers its message as the ``detail``, its own default without one.

REFUSING = "{mount}refusing-summaries/"
PERMISSION_DENIED = "You do not have permission to perform this action."

COMPUTE_REFUSAL_CASES = [
    (Case("computed-compute-403", "GET", REFUSING + "denied/", _admin), 403, "nope"),
    (
        Case("computed-compute-403-bare", "GET", REFUSING + "denied-bare/", _admin),
        403,
        PERMISSION_DENIED,
    ),
    (Case("computed-compute-404", "GET", REFUSING + "missing/", _admin), 404, "nope"),
    (
        Case("computed-compute-404-bare", "GET", REFUSING + "missing-bare/", _admin),
        404,
        "Not found.",
    ),
    (
        Case("computed-compute-404-head", "HEAD", REFUSING + "missing/", _admin),
        404,
        None,
    ),
]


@pytest.mark.parametrize("uniform", [True, False], ids=["uniform", "native"])
@pytest.mark.parametrize(
    "case, status, detail",
    [
        pytest.param(case, status, detail, id=case.id)
        for case, status, detail in COMPUTE_REFUSAL_CASES
    ],
)
def test_computed_refusals_answer_alike(case, status, detail, uniform):
    serve_both()
    setting = dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=uniform)
    with override_settings(SCOPED_RBAC=setting):
        drf, ninja = parity(case)

    assert drf.status == status, drf
    if detail is not None:
        assert json.loads(drf.body)["detail"] == detail, drf
    assert drf == ninja


def test_computed_links_name_the_documents_of_the_serving_mount():
    drf, ninja = parity(Case("computed-links", "GET", COMPUTED, _admin))

    assert drf == ninja
    rtype = registration.get_resource_type_for("summaries")
    assert split_links(ninja.headers["Link"]) == [
        f"<http://testserver{{mount}}json-schema/{rtype.schema.pk}/>; "
        'rel="describedby"; type="application/schema+json"',
        f"<http://testserver{{mount}}json-ld/{rtype.context.pk}/>; "
        'rel="http://www.w3.org/ns/json-ld#context"; type="application/ld+json"',
    ]


# The service catalog (``services/``): GET (HEAD is DRF's api_view's 405),
# OPTIONS, open to everyone. The catalog lists each type's list URL, on the
# serving mount.

SERVICES = "{mount}services/"

SERVICES_CASES = [
    (Case("services-options", "OPTIONS", SERVICES), 204),
    (Case("services-options-admin", "OPTIONS", SERVICES, _admin), 204),
    (Case("preflight-services", "OPTIONS", SERVICES, headers=PREFLIGHT), 204),
    (
        Case(
            "services-malformed-if-none-match",
            "GET",
            SERVICES,
            headers={"If-None-Match": "other"},
        ),
        400,
    ),
    *(
        (Case(f"services-{method.lower()}", method, SERVICES, body={}), 405)
        for method in ("POST", "PUT", "PATCH", "DELETE")
    ),
]


@pytest.mark.parametrize(
    "case, status",
    [pytest.param(case, status, id=case.id) for case, status in SERVICES_CASES],
)
def test_services_routes_answer_alike(case, status):
    drf, ninja = parity(case)

    assert drf.status == status, drf
    assert drf == ninja


def _ninja_serves(url):
    """Whether `url` (a normalized list URL of the catalog) is served by the
    ninja API."""
    path = url.removeprefix("http://testserver").replace("{mount}", MOUNTS["ninja"])
    try:
        return resolve(path).namespace == "scrud-ninja"
    except Resolver404:
        return False


def _catalog(exchange, front_end):
    """The catalog `exchange` (a normalized ``services/`` GET) served, its
    URLs on `front_end`'s mount again, in order."""
    return [
        (type_uri, url.replace("{mount}", MOUNTS[front_end]))
        for type_uri, url in json.loads(exchange.body).items()
    ]


def test_services_differ_only_where_the_serving_mount_does():
    """The two catalogs cannot answer alike byte for byte, so this asserts
    exactly how they differ. A catalog lists absolute URLs, which name the
    serving mount, and its ETag is a hash of the catalog served
    (`ScrudServices.etag_of`): the two ETags differ as the URLs do, and no
    normalization of the harness can equate two hashes. And the UI's types
    are served on DRF only (R3), so ninja's catalog names DRF's URL for
    them, where they are served. So: each lists every type, in the same
    order; a type both front ends serve on the serving mount (alike once
    normalized); one ninja does not serve at the URL serving it (DRF's);
    each ETag the hash of its own catalog; every other header alike."""
    drf, ninja = parity(Case("services", "GET", SERVICES))

    assert drf.status == ninja.status == 200, drf
    drf_catalog, ninja_catalog = _catalog(drf, "drf"), _catalog(ninja, "ninja")
    assert [uri for uri, _ in drf_catalog] == [uri for uri, _ in ninja_catalog]
    normalized_drf = json.loads(drf.body)
    unserved = 0
    for (type_uri, drf_url), (_, ninja_url) in zip(
        drf_catalog, ninja_catalog, strict=True
    ):
        if _ninja_serves(normalized_drf[type_uri]):
            assert ninja_url == drf_url.replace(MOUNTS["drf"], MOUNTS["ninja"])
        else:
            unserved += 1
            assert ninja_url == drf_url, type_uri
    assert 0 < unserved < len(drf_catalog)
    for exchange, catalog in ((drf, drf_catalog), (ninja, ninja_catalog)):
        assert exchange.headers["ETag"] == quote_etag(
            ScrudServices.etag_of(dict(catalog))
        )
    differing = {"ETag", "Content-Length"}
    assert {k: v for k, v in drf.headers.items() if k not in differing} == {
        k: v for k, v in ninja.headers.items() if k not in differing
    }


def test_services_if_none_match_answers_alike(client):
    """A GET naming the catalog's ETag (each front end's own) is a 304, alike
    on both but for that ETag (`test_services_differ_only_where_the_serving_mount_does`);
    one naming any (``*``) too."""
    serve_both()
    answers = {}
    for mount in MOUNTS.values():
        url = SERVICES.replace("{mount}", mount)
        etag = client.get(url)["ETag"]
        for name, header in (("own", etag), ("any", "*")):
            response = client.get(url, headers={"If-None-Match": header})
            exchange = normalized(response, mount, {})
            assert exchange.status == 304, exchange
            assert exchange.headers.pop("ETag") == etag
            answers.setdefault(name, []).append(exchange)

    for drf, ninja in answers.values():
        assert drf == ninja


def _head_of(client, mount, path):
    """A HEAD of `path` on `mount`, normalized, its Content-Length dropped (it
    counts a body the HEAD leaves out), and that Content-Length."""
    response = client.head(path.replace("{mount}", mount))
    length = response.get("Content-Length")
    if length is not None:
        del response["Content-Length"]
    return normalized(response, mount, {}), length


def test_services_head_is_refused_alike(client):
    # DRF's api_view serves only the methods it lists: not HEAD.
    serve_both()
    drf, ninja = (_head_of(client, mount, SERVICES) for mount in MOUNTS.values())

    assert drf[0].status == 405, drf
    assert drf == ninja


# The role routes (scoped-rbac's own: ``roles/``, ``role-assignments/``,
# ``user-rbac-policy/``), served by both front ends at the mount's root.

ROLE_CASES = [
    (Case("roles-get-admin", "GET", "{mount}roles/", _admin), 200),
    (Case("roles-get-anonymous", "GET", "{mount}roles/"), 401),
    (
        Case("role-assignments-get-admin", "GET", "{mount}role-assignments/", _admin),
        200,
    ),
    (
        Case("user-rbac-policy-get-admin", "GET", "{mount}user-rbac-policy/", _admin),
        200,
    ),
]

#: Where scoped-rbac serves its schemas and contexts in `tests.parity_urls`:
#: ``scoped_rbac.semantic_urls``, included once (``scrud_django.urls``, under
#: the DRF mount), which both front ends link to.
SEMANTIC = f"http://testserver{MOUNTS['drf']}semantic/"


def _semantic_links_of_the_drf_mount(exchange):
    """`exchange` (a ninja one) with its Links to scoped-rbac's semantic
    documents (`SEMANTIC`) normalized as the DRF exchange's are: both front
    ends link to the one place serving them, which the harness normalizes
    on the DRF mount only."""
    if "Link" not in exchange.headers:
        return exchange
    link = exchange.headers["Link"].replace(
        SEMANTIC, "http://testserver{mount}semantic/"
    )
    return Exchange(exchange.status, {**exchange.headers, "Link": link}, exchange.body)


@pytest.mark.parametrize(
    "case, status",
    [pytest.param(case, status, id=case.id) for case, status in ROLE_CASES],
)
def test_role_routes_answer_alike(case, status):
    drf, ninja = parity(case)

    assert drf.status == status, drf
    assert drf == _semantic_links_of_the_drf_mount(ninja)


def test_computed_and_services_vary_on_accept_alike(client):
    serve_both()
    admin = _user(client, superuser=True)
    etag = computed_etag({"key": "k1", "user": admin.get_username()})
    json_body = {"data": "{}", "content_type": "application/json"}
    asked = {
        "computed-get": _varies(client, "GET", COMPUTED),
        "computed-head": _varies(client, "HEAD", COMPUTED),
        "computed-get-json": _varies(
            client, "GET", COMPUTED, headers={"Accept": "application/json"}
        ),
        "computed-not-modified": _varies(
            client, "GET", COMPUTED, headers={"If-None-Match": etag}
        ),
        "computed-options": _varies(client, "OPTIONS", COMPUTED),
        "computed-put": _varies(client, "PUT", COMPUTED, **json_body),
        "services-get": _varies(client, "GET", SERVICES),
        "services-not-modified": _varies(
            client, "GET", SERVICES, headers={"If-None-Match": "*"}
        ),
        "services-malformed": _varies(
            client, "GET", SERVICES, headers={"If-None-Match": "x"}
        ),
        "services-options": _varies(client, "OPTIONS", SERVICES),
        "services-post": _varies(client, "POST", SERVICES, **json_body),
        "services-head": _varies(client, "HEAD", SERVICES),
    }
    client.logout()
    asked["computed-anonymous"] = _varies(client, "GET", COMPUTED)
    asked["computed-put-anonymous"] = _varies(client, "PUT", COMPUTED, **json_body)

    for name, varies in asked.items():
        assert varies["drf"] == varies["ninja"], (name, varies)


def test_preflights_vary_alike(client):
    # A scrudful route's CORS preflight is scrudful's bare 204, answered
    # before DRF's view (no Vary); the others are their DRF view's answer.
    serve_both()
    _user(client, superuser=True)
    item = PROFILE.store()
    varies = {
        path: _varies(client, "OPTIONS", path, headers=PREFLIGHT)
        for path in (
            PROFILE.path(item.pk),
            PROFILE.path("999999999"),
            LIST,
            SERVICES,
            COMPUTED,
            COLLECTION_SCHEMA,
            "{mount}collections-json-ld/partner-profiles/",
        )
    }

    for path, vary in varies.items():
        assert vary["drf"] == vary["ninja"], (path, vary)
    for path in (PROFILE.path(item.pk), LIST, SERVICES):
        assert varies[path]["drf"] is None, (path, varies[path])


def _refusing_hook(request):
    """A ``SCOPED_RBAC["NINJA_USER"]`` hook refusing the request's credentials."""
    raise AuthenticationError()


@pytest.mark.parametrize(
    "path",
    [
        "{mount}partner-profiles/",
        "{mount}collections-json-schema/partner-profiles/",
        "{mount}collections-json-ld/partner-profiles/",
        COMPUTED,
        SERVICES,
    ],
)
def test_options_refuses_a_requester_failing_authentication(client, path):
    """Every ninja OPTIONS resolves its requester
    (``scoped_rbac.ninja.resolve_user``), as DRF authenticates every OPTIONS:
    a ``NINJA_USER`` hook refusing the credentials refuses it (401), as DRF
    refuses credentials its authentication fails."""
    serve_both()
    _user(client, superuser=True)
    url = path.replace("{mount}", MOUNTS["ninja"])
    # Answered without the hook: it is the hook that refuses.
    assert client.options(url).status_code in (200, 204)
    refusing = {**settings.SCOPED_RBAC, "NINJA_USER": _refusing_hook}
    with override_settings(SCOPED_RBAC=refusing):
        ninja = front_ends.exchange(client, "ninja", "OPTIONS", path)

    assert ninja.status == 401, ninja
    assert json.loads(ninja.body)["code"] == "not_authenticated", ninja
    drf = front_ends.exchange(
        Client(), "drf", "OPTIONS", path, headers=REFUSED_CREDENTIALS
    )
    assert drf == ninja


#: Credentials each front end refuses: a ``NINJA_USER`` hook raising ninja's
#: ``AuthenticationError`` (ninja; DRF reads no such hook) and Basic
#: credentials of no user (DRF; ninja authenticates no Basic credentials).
REFUSED_CREDENTIALS = {"Authorization": "Basic bm86bm8="}

FAILING_CREDENTIALS_CASES = [
    Case(
        f"failing-credentials-{method.lower()}-{name}",
        method,
        "{mount}" + path,
        headers=REFUSED_CREDENTIALS,
    )
    for method, name, path in (
        # Methods the route does not serve: refused before the 405.
        ("HEAD", "services", "services/"),
        ("PATCH", "services", "services/"),
        ("PATCH", "collection-schema", "collections-json-schema/partner-profiles/"),
        ("PATCH", "collection-context", "collections-json-ld/partner-profiles/"),
        ("PATCH", "roles", "roles/"),
        ("PATCH", "role-assignments", "role-assignments/"),
        ("PUT", "user-rbac-policy", "user-rbac-policy/"),
        ("PATCH", "type-list", "partner-profiles/"),
        ("PUT", "computed", "summaries/k1/"),
        # Methods it serves.
        ("GET", "services", "services/"),
        ("GET", "type-list", "partner-profiles/"),
        ("OPTIONS", "type-list", "partner-profiles/"),
        ("GET", "roles", "roles/"),
        ("GET", "user-rbac-policy", "user-rbac-policy/"),
        ("GET", "collection-schema", "collections-json-schema/partner-profiles/"),
        ("OPTIONS", "services", "services/"),
        ("OPTIONS", "roles", "roles/"),
        ("OPTIONS", "role-assignments", "role-assignments/"),
        ("OPTIONS", "user-rbac-policy", "user-rbac-policy/"),
        ("OPTIONS", "collection-schema", "collections-json-schema/partner-profiles/"),
        ("OPTIONS", "computed", "summaries/k1/"),
    )
]


@pytest.mark.parametrize("case", FAILING_CREDENTIALS_CASES, ids=lambda case: case.id)
def test_failing_credentials_are_refused_alike(case):
    serve_both()
    refusing = {**settings.SCOPED_RBAC, "NINJA_USER": _refusing_hook}
    with override_settings(SCOPED_RBAC=refusing):
        drf, ninja = parity(case)
        varies = _varies(Client(), case.method, case.path, headers=case.headers)

    assert drf.status == 401, drf
    assert drf == ninja
    # The two are refused by different mechanisms: DRF's authentication reads
    # the session before the Basic credentials fail (``Vary: Cookie``);
    # ninja's hook refuses before the session is read.
    assert varies["drf"] == ", ".join(filter(None, [varies["ninja"], "Cookie"])), varies


def test_role_routes_vary_alike(client):
    serve_both()
    paths = ("{mount}roles/", "{mount}role-assignments/", "{mount}user-rbac-policy/")
    asked = {}
    for login in ("anonymous", "admin"):
        if login == "admin":
            _user(client, superuser=True)
        for path in paths:
            for method in ("GET", "OPTIONS"):
                asked[(login, method, path)] = _varies(client, method, path)
            asked[(login, "preflight", path)] = _varies(
                client, "OPTIONS", path, headers=PREFLIGHT
            )

    for name, varies in asked.items():
        assert varies["drf"] == varies["ninja"], (name, varies)


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
    *(
        case
        for case in _options_cases()
        if case.id in {"put-collections-json-schema", "delete-collections-json-ld"}
    ),
    *(
        case
        for case, _ in [*COMPUTED_CASES, *SERVICES_CASES]
        if case.id
        in {
            "computed-get-anonymous",
            "computed-options-anonymous",
            "computed-put",
            "computed-put-anonymous",
            "services-malformed-if-none-match",
            "services-post",
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


# DRF negotiates the response's renderer first (``initial()``), every method
# alike: an Accept none of the route's renderers serves is its 406, and a
# format override none of them has its 404, before anything else; a
# scrudful route's CORS preflight is answered before (204). The renderer it
# selects decides a document's media type (``q`` is not weighed).

XML = {"Accept": "application/xml"}
COLLECTION_SCHEMA = "{mount}collections-json-schema/partner-profiles/"


def _xml_with_etag(keys):
    return {**XML, **_etag("item")(keys)}


NOT_ACCEPTABLE_CASES = [
    Case("xml-item-get", "GET", PROFILE.path(), PROFILE.setup(WRITER), headers=XML),
    Case(
        "xml-item-get-anonymous",
        "GET",
        PROFILE.path(),
        PROFILE.setup(login=False),
        headers=XML,
    ),
    Case(
        "xml-item-missing",
        "GET",
        PROFILE.path("999999999"),
        PROFILE.setup(superuser=True),
        headers=XML,
    ),
    Case(
        "xml-item-put",
        "PUT",
        PROFILE.path(),
        PROFILE.setup(WRITER),
        body=PROFILE.body("a"),
        headers=_xml_with_etag,
    ),
    Case(
        "xml-item-delete",
        "DELETE",
        PROFILE.path(),
        PROFILE.setup(superuser=True),
        headers=_xml_with_etag,
    ),
    Case(
        "xml-item-patch",
        "PATCH",
        PROFILE.path(),
        PROFILE.setup(superuser=True),
        body={},
        headers=XML,
    ),
    Case(
        "xml-item-options",
        "OPTIONS",
        PROFILE.path(),
        PROFILE.setup(superuser=True),
        headers=XML,
    ),
    Case(
        "xml-item-preflight",
        "OPTIONS",
        PROFILE.path(),
        PROFILE.setup(superuser=True),
        headers={**XML, **PREFLIGHT},
    ),
    Case("xml-list-get", "GET", LIST, _listing(superuser=True), headers=XML),
    Case(
        "xml-list-post",
        "POST",
        LIST,
        _listing(superuser=True),
        body={"name": "n", "rbac_context": "a"},
        headers=XML,
    ),
    Case("xml-list-options", "OPTIONS", LIST, _listing(superuser=True), headers=XML),
    Case(
        "xml-document-get",
        "GET",
        SCHEMA.path(),
        SCHEMA.setup(superuser=True),
        headers=XML,
    ),
    Case(
        "xml-document-put",
        "PUT",
        SCHEMA.path(),
        SCHEMA.setup(superuser=True),
        body=SCHEMA.body("a"),
        headers=_xml_with_etag,
    ),
    Case("xml-collection-get", "GET", COLLECTION_SCHEMA, headers=XML),
    Case("xml-collection-head", "HEAD", COLLECTION_SCHEMA, headers=XML),
    Case(
        "xml-collection-unknown",
        "GET",
        "{mount}collections-json-ld/no-such-type/",
        headers=XML,
    ),
    Case("xml-collection-options", "OPTIONS", COLLECTION_SCHEMA, headers=XML),
    Case(
        "xml-collection-preflight",
        "OPTIONS",
        COLLECTION_SCHEMA,
        headers={**XML, **PREFLIGHT},
    ),
    Case("xml-collection-post", "POST", COLLECTION_SCHEMA, body={}, headers=XML),
    Case("xml-computed-get", "GET", COMPUTED, _admin, headers=XML),
    Case("xml-computed-get-anonymous", "GET", COMPUTED, headers=XML),
    Case("xml-computed-put", "PUT", COMPUTED, _admin, body={}, headers=XML),
    Case("xml-computed-put-anonymous", "PUT", COMPUTED, body={}, headers=XML),
    Case("xml-computed-options", "OPTIONS", COMPUTED, _admin, headers=XML),
    Case(
        "xml-computed-preflight",
        "OPTIONS",
        COMPUTED,
        _admin,
        headers={**XML, **PREFLIGHT},
    ),
    Case("xml-services-get", "GET", SERVICES, headers=XML),
    Case("xml-services-post", "POST", SERVICES, body={}, headers=XML),
    Case("xml-services-options", "OPTIONS", SERVICES, headers=XML),
    Case("xml-services-preflight", "OPTIONS", SERVICES, headers={**XML, **PREFLIGHT}),
    # A format override none of the route's renderers has: 404.
    Case(
        "format-unknown-item",
        "GET",
        PROFILE.path() + "?format=xml",
        PROFILE.setup(superuser=True),
    ),
    Case(
        "format-unknown-document",
        "GET",
        SCHEMA.path() + "?format=ld-json",
        SCHEMA.setup(superuser=True),
    ),
    Case("format-unknown-collection", "GET", COLLECTION_SCHEMA + "?format=html"),
    Case("format-unknown-computed", "GET", COMPUTED + "?format=xml", _admin),
    Case("format-unknown-services", "GET", SERVICES + "?format=html"),
    # Formats the route's renderers have.
    Case(
        "format-json-document",
        "GET",
        SCHEMA.path() + "?format=json",
        SCHEMA.setup(superuser=True),
    ),
    Case(
        "format-own-document",
        "GET",
        SCHEMA.path() + "?format=schema-json",
        SCHEMA.setup(superuser=True),
    ),
    Case("format-json-collection", "GET", COLLECTION_SCHEMA + "?format=json"),
    Case(
        "format-xml-json-collection",
        "GET",
        COLLECTION_SCHEMA + "?format=json",
        headers=XML,
    ),
    # Accepts DRF reads in its own way.
    *(
        Case(f"accept-{name}-{kind}", "GET", path, setup, headers={"Accept": accept})
        for name, accept in (
            ("empty", ""),
            ("image-any", "image/*"),
            ("any-q-0", "*/*;q=0"),
            ("json-q-0", "application/json;q=0"),
            (
                "document-lower-q",
                "application/schema+json;q=0.1, application/json;q=0.5",
            ),
            ("document-with-a-parameter", "application/schema+json; x=1"),
            ("any-json", "*/json"),
            ("xml-then-any", "application/xml, */*;q=0.1"),
        )
        for kind, path, setup in (
            ("document", SCHEMA.path(), SCHEMA.setup(superuser=True)),
            ("collection", COLLECTION_SCHEMA, lambda client: {}),
            ("item", PROFILE.path(), PROFILE.setup(superuser=True)),
        )
    ),
]

# The cases DRF answers 406.
NOT_ACCEPTED = (
    {
        case.id
        for case in NOT_ACCEPTABLE_CASES
        if case.id.startswith("xml-")
        and case.id not in {"xml-item-preflight", "xml-services-preflight"}
    }
    | {
        f"accept-{name}-{kind}"
        for name in ("empty", "image-any")
        for kind in ("document", "collection", "item")
    }
    | {
        # The format's renderer does not serve what the Accept asks.
        "format-xml-json-collection",
        # A plain type's item has no schema renderer.
        "accept-document-with-a-parameter-item",
    }
)


@pytest.mark.parametrize("uniform", [True, False], ids=["uniform", "native"])
@pytest.mark.parametrize("case", NOT_ACCEPTABLE_CASES, ids=lambda case: case.id)
def test_negotiation_answers_alike(case, uniform):
    serve_both()
    setting = dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=uniform)
    with override_settings(SCOPED_RBAC=setting):
        drf, ninja = parity(case)

    assert drf == ninja
    if case.id in NOT_ACCEPTED:
        assert drf.status == 406, drf
    elif case.id.startswith("format-unknown-"):
        assert drf.status == 404, drf
    else:
        assert drf.status < 400, drf


# The parameters of the accepted media range reach DRF's JSON renderers:
# ``indent`` pretty-prints (clamped to 0-8, 0 or a non-integer compact).

INDENTED = {"Accept": "application/json; indent=4"}


def _indent_cases():
    cases = [
        (Case("indent-item", "GET", PROFILE.path(), PROFILE.setup(WRITER)), INDENTED),
        (Case("indent-list", "GET", LIST, ADMIN_LIST), INDENTED),
        (Case("indent-collection-schema", "GET", COLLECTION_SCHEMA), INDENTED),
        (Case("indent-computed", "GET", COMPUTED, _admin), INDENTED),
        (
            Case("indent-computed-refused", "GET", REFUSING + "denied/", _admin),
            INDENTED,
        ),
        (
            Case("indent-item-missing", "GET", PROFILE.path("999"), PROFILE.setup()),
            INDENTED,
        ),
        (
            Case(
                "indent-item-anonymous",
                "GET",
                PROFILE.path(),
                PROFILE.setup(login=False),
            ),
            INDENTED,
        ),
        (
            Case(
                "indent-item-put",
                "PUT",
                PROFILE.path(),
                PROFILE.setup(WRITER),
                body=PROFILE.body("a"),
                headers=lambda keys: {**INDENTED, **_etag("item")(keys)},
            ),
            None,
        ),
        (
            Case(
                "indent-schema-document",
                "GET",
                SCHEMA.path(),
                SCHEMA.setup({"http.GET": [JSON_SCHEMA]}),
            ),
            {"Accept": "application/schema+json; indent=2"},
        ),
    ]
    for name, accept in (
        ("any", "*/*; indent=2"),
        ("application-any", "application/*; indent=3"),
        ("zero", "application/json; indent=0"),
        ("negative", "application/json; indent=-2"),
        ("over", "application/json; indent=20"),
        ("not-an-integer", "application/json; indent=x"),
        ("quoted", 'application/json; indent="5"'),
        ("q", "application/json; q=0.5; indent=1"),
    ):
        cases.append(
            (
                Case(f"indent-{name}", "GET", PROFILE.path(), PROFILE.setup(WRITER)),
                {"Accept": accept},
            )
        )
    cases.append(
        (
            Case(
                "indent-schema-document-missing",
                "GET",
                SCHEMA.path("999"),
                SCHEMA.setup({"http.GET": [JSON_SCHEMA]}),
            ),
            {"Accept": "application/schema+json; indent=2"},
        )
    )
    # Every error case of uniform errors, indented.
    cases.extend(
        (dataclasses.replace(case, id=f"indent-{case.id}"), INDENTED)
        for case in UNIFORM_CASES
    )
    return [
        dataclasses.replace(case, headers=_with(case.headers, headers))
        for case, headers in cases
    ]


def _with(own, headers):
    """A case's headers `own` (a mapping, or a callable of its setup's keys)
    with `headers` (``None``: none) over them."""
    if headers is None:
        return own
    if own is None:
        return headers

    def merged(keys):
        return {**(own(keys) if callable(own) else own), **headers}

    return merged


@pytest.mark.parametrize("uniform", [True, False], ids=["uniform", "native"])
@pytest.mark.parametrize("case", _indent_cases(), ids=lambda case: case.id)
def test_accept_parameters_answer_alike(case, uniform):
    serve_both()
    setting = dict(settings.SCOPED_RBAC, UNIFORM_ERRORS=uniform)
    with override_settings(SCOPED_RBAC=setting):
        drf, ninja = parity(case)

    assert drf == ninja
    if case.id in {"indent-item", "indent-list", "indent-collection-schema"}:
        assert drf.body.startswith("{\n    "), drf


def test_services_indent_alike():
    # The catalogs differ where the serving mount does
    # (`test_services_differ_only_where_the_serving_mount_does`): each is
    # indented as DRF indents it, its other headers alike.
    drf, ninja = parity(Case("indent-services", "GET", SERVICES, headers=INDENTED))

    for exchange in (drf, ninja):
        assert exchange.status == 200, exchange
        assert exchange.body == json.dumps(
            json.loads(exchange.body), indent=4, separators=(",", ": ")
        ), exchange
    differing = {"ETag", "Content-Length"}
    assert {k: v for k, v in drf.headers.items() if k not in differing} == {
        k: v for k, v in ninja.headers.items() if k not in differing
    }


def test_negotiation_refusals_vary_on_accept_alike(client):
    serve_both()
    _user(client, superuser=True)
    item = PROFILE.store()
    for method, path in (
        ("GET", PROFILE.path(item.pk)),
        ("PATCH", PROFILE.path(item.pk)),
        ("GET", LIST),
        ("GET", COLLECTION_SCHEMA),
        ("GET", COMPUTED),
        ("GET", SERVICES),
        ("OPTIONS", SERVICES),
    ):
        varies = _varies(client, method, path, headers=XML)
        assert varies["drf"] == varies["ninja"], (method, path, varies)


@pytest.mark.parametrize(
    "method, path",
    [
        (method, "{mount}" + path)
        for method in ("POST", "PUT")
        for path in (
            "collections-json-schema/partner-profiles/",
            "collections-json-ld/partner-profiles/",
            "services/",
            "summaries/k1/",
        )
    ],
)
def test_csrf_refusals_answer_alike(method, path):
    # A session user's unsafe request without a CSRF token, on the routes
    # whose answers carry their DRF view's Allow: refused (403) before the
    # 405, with that Allow (follow-up ninja-allow-of-entry-refusals).
    serve_both()
    client = Client(enforce_csrf_checks=True)
    _user(client, superuser=True)

    drf, ninja = (
        front_ends.exchange(client, front_end, method, path, body={})
        for front_end in MOUNTS
    )

    assert drf.status == 403, drf
    assert "CSRF Failed" in drf.body, drf
    assert drf.headers.get("Allow"), drf
    assert drf == ninja
