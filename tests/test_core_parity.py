"""A parity harness: the core facade (`scrud_django.core.resources`,
`lists`, `writes`) against the DRF routes it mirrors, for each type of
`TYPES` (a plain one, one with hooks, the workflow note of
`tests.test_core_workflow` under its policy, and a ToDo under its default
policy), each user of `users_for` and each place a resource is stored. The
DRF views do not call the facade, so only a comparison like this one sees
them drift apart.

The helpers (`outcome_of`, `request_of`, `users_for`, `store`, `drf_status`...)
are module level so that later cases can reuse them. The DRF PUT and DELETE
send ``If-Match`` with the resource's etag (scrudful answers 428 without it):
the harness compares authorization, not preconditions.

Known divergences are pinned: `GAPS` and `FAIL_OPEN_GAPS` map a case to the
``(facade outcome, DRF status)`` pair it shows today. A pinned case is a
strict xfail (`check_parity`); if either side changes (including the gap
closing) it fails with `GapDrift` instead, so the entry must be corrected or
removed. `FAIL_OPEN_GAPS` holds those where the facade allows what DRF
refuses."""

from collections.abc import Callable
from dataclasses import dataclass

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import transaction
from django.http import Http404
from django.test import RequestFactory
from django.urls import reverse
from django.utils.http import quote_etag
from rest_framework.test import APIClient
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access
from scoped_rbac.permissions import drf_error

from scrud_django import collection_type_uri_for
from scrud_django.core.lists import readable_queryset
from scrud_django.core.requirements import (
    asks_grants,
    asks_grants_itself,
    is_hook,
    requirement_leaves,
)
from scrud_django.core.resources import allowed_methods, load_for_read, object_readable
from scrud_django.core.types import requirement_for
from scrud_django.core.writes import update, validate_write
from scrud_django.exceptions import ScrudException
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    workflow_policy_resource_type,
)

from .resource_types import (
    HOOKED_NOTE_TYPE_URI,
    WORKFLOW_NOTE_TYPE_URI,
    hooked_resource_types,
)
from .steps import *  # noqa: F403, F401
from .test_core_resources import JSON_LD, SCRUD_WORKFLOW, user_with, users
from .test_core_workflow import DRAFT, NOTE_POLICY, PUBLISHED

TODO = "tests://ToDo"
JSON = "application/json"
SECRET = "secret-a"  # stored in "a", marked ``secret`` (hidden by hooked-notes)
STORED_IN = ["a", "b", ["a", "b"], SECRET]
BODY_CONTEXTS = ["", "a", "b", ["a", "b"]]
_METHODS = ("GET", "POST", "PUT", "DELETE")

LIST_GRANT_GAP = "core-list-grant-and-request-check: Task 3"
ITEM_REQUEST_GAP = (
    "core-list-grant-and-request-check: Task 3 (load_for_read asks only the "
    "object check; DRF's request check refuses first)"
)
BODY_REQUEST_GAP = (
    "the facade runs no request check of the body's contexts: DRF refuses "
    "(403) a write the body's contexts are not granted before validating it; "
    "validate_write answers its own error (400) or the object's 404"
)
FAIL_OPEN_REASON = (
    "facade allows what DRF refuses: core-list-grant-and-request-check: "
    "Task 3 check_request"
)


@dataclass(frozen=True)
class ParityType:
    slug: str
    uri: str
    content: Callable  # (context) -> stored content
    body: Callable  # (context) -> a valid PUT body


TYPES = {
    "json-ld": ParityType(
        "json-ld",
        JSON_LD,
        lambda context: {"rbac_context": context},
        lambda context: {"text": "x", "rbac_context": context},
    ),
    "hooked-notes": ParityType(
        "hooked-notes",
        HOOKED_NOTE_TYPE_URI,
        lambda context: {"text": "n", "rbac_context": context},
        lambda context: {"text": "x", "rbac_context": context},
    ),
    # `tests.test_core_workflow`'s note: a draft is published by "publish"
    # under the policy of context "a" (none in "b").
    "workflow-notes": ParityType(
        "workflow-notes",
        WORKFLOW_NOTE_TYPE_URI,
        lambda context: dict(DRAFT, rbac_context=context),
        lambda context: dict(PUBLISHED, rbac_context=context),
    ),
    "todos": ParityType(
        "todos",
        TODO,
        lambda context: {
            "title": "t",
            "status": "not started",
            "priority": "low",
            "assignee": "http://somewhere.example/someone",
            "rbac_context": context,
        },
        lambda context: {
            "title": "x",
            "status": "in progress",
            "priority": "low",
            "assignee": "http://somewhere.example/someone",
            "rbac_context": context,
        },
    ),
}
TYPE_SLUGS = list(TYPES)
USER_NAMES = [
    "superuser",
    "reader-a",
    "conditioned",
    "writer-a",
    "anonymous",
    "lister-a",
    "workflow-a",
    "deleter-a",
    "writer-ab",
    "publisher-a",
    "retracter-a",
]


@pytest.fixture(autouse=True)
def registered_types(db):
    """The test types registered (rows and hooks), the workflow note's policy
    stored, and the hooked types again afterwards."""
    reverse("hooked-notes-list")
    hooked_resource_types()
    workflow_policy_resource_type()
    ResourceRegistration.register(dict(NOTE_POLICY), "workflow-policies")
    yield
    hooked_resource_types()


def users_for(uri):
    """`tests.test_core_resources.users` for the type `uri` (that matrix
    grants ``JSON_LD`` only), with a deleter in "a", a writer in "a" and "b",
    and a publisher and a retracter in "a" (PUT only by the workflow's
    "publish", "retract") besides: the same names, so a case can name a user
    for any type."""
    listed = collection_type_uri_for(uri)
    if uri == JSON_LD:
        matrix = users()
    else:
        conditioned = {"http.GET": {uri: {"condition": 'rbac_context == "b"'}}}
        workflow = {"condition": SCRUD_WORKFLOW["condition"]}
        matrix = {
            "superuser": user_with(superuser=True),
            "reader-a": user_with(({"http.GET": [uri]}, "a")),
            "conditioned": user_with((conditioned, "a"), (conditioned, "b")),
            "writer-a": user_with(
                ({"http.GET": [uri], "http.POST": [uri], "http.PUT": [uri]}, "a")
            ),
            "anonymous": AnonymousUser(),
            "lister-a": user_with(
                ({"http.GET": [uri, listed], "http.POST": [uri, listed]}, "a")
            ),
            "workflow-a": user_with(
                (
                    {
                        "http.GET": [uri],
                        "http.PUT": {uri: workflow},
                        "http.DELETE": {uri: workflow},
                    },
                    "a",
                )
            ),
        }
    full = {f"http.{m}": [uri, listed] for m in _METHODS}
    matrix["deleter-a"] = user_with(({"http.GET": [uri], "http.DELETE": [uri]}, "a"))
    matrix["writer-ab"] = user_with((full, "a"), (full, "b"))
    for name, transition in (("publisher-a", "publish"), ("retracter-a", "retract")):
        scrud_workflow = {
            "condition": {
                "operator": "scrud_workflow",
                "authorized_transitions": [transition],
            }
        }
        matrix[name] = user_with(
            ({"http.GET": [uri], "http.PUT": {uri: scrud_workflow}}, "a")
        )
    return matrix


def request_of(method, user, path="/"):
    request = getattr(RequestFactory(), method.lower())(path)
    request.user = user
    return request


def client_of(user):
    client = APIClient()
    if user.is_authenticated:
        client.force_authenticate(user)
    return client


def resource_type_of(parity: ParityType):
    return get_resource_type_for(parity.slug)


def store(parity: ParityType, where):
    """A resource of `parity` stored in `where` (a context, or `SECRET`)."""
    if where == SECRET:
        content = dict(parity.content("a"), secret=True)
    else:
        content = parity.content(where)
    return ResourceRegistration.register(content, parity.slug)


def outcome_of(call) -> int:
    """The status DRF would give the result of `call`: a value is 200;
    ``Http404`` 404; ``access.AccessDenied`` 403; a `ScrudException` its
    ``status_code``; any other ``access.AccessError`` its DRF status
    (`scoped_rbac.permissions.drf_error`)."""
    try:
        call()
    except Http404:
        return 404
    except access.AccessDenied:
        return 403
    except ScrudException as exc:
        return exc.status_code
    except access.AccessError as exc:
        return drf_error(exc).status_code
    return 200


def outcome_class(status):
    return 200 if 200 <= status < 300 else status


def etag_header(resource):
    return {"HTTP_IF_MATCH": quote_etag(resource.etag)}


def drf_status(parity: ParityType, user, method, resource, *, body=None):
    """The DRF detail route's response for `method`, in a savepoint rolled
    back."""
    url = reverse(f"{parity.slug}-detail", args=[resource.pk])
    client = client_of(user)
    with transaction.atomic():
        if method == "GET":
            response = client.get(url, HTTP_ACCEPT=JSON)
        elif method == "DELETE":
            response = client.delete(url, **etag_header(resource))
        elif method == "PUT":
            response = client.put(url, body, format="json", **etag_header(resource))
        else:
            response = client.options(url)
        transaction.set_rollback(True)
    return response


def drf_authz_status(response):
    """The status class of `response`, a 401 (the anonymous requester's) being
    the 403 the facade names."""
    status = response.status_code
    return 403 if status == 401 else outcome_class(status)


def facade_load(parity, user, resource, method):
    return outcome_of(
        lambda: load_for_read(
            request_of(method, user),
            resource_type_of(parity),
            resource.pk,
            requirement=requirement_for(resource_type_of(parity), parity.slug),
            method=method,
        )
    )


def key_of(value):
    """A hashable form of a stored or body context."""
    return tuple(value) if isinstance(value, list) else value


class GapDrift(Exception):
    """A pinned divergence is no longer what it was (either side changed, or
    it closed): correct or remove its entry."""


def cells(test, slugs, users, wheres, pair):
    """The pinned `pair` of each case of `test` over `slugs`, `users` and
    `wheres`."""
    return {
        (test, slug, user, key_of(where)): pair
        for slug in slugs
        for user in users
        for where in wheres
    }


ALL = TYPE_SLUGS
GAPS = {}
FAIL_OPEN_GAPS = {}
# The DRF route asks the method's request check (``has_permission``, with the
# body's contexts for a write) before it looks the object up; the facade does
# not. Each entry is the ``(facade outcome, DRF status)`` pair it shows.
for slugs, users_, wheres in [
    (ALL, ["anonymous"], STORED_IN),
    (ALL, ["conditioned"], ["a", SECRET]),
]:
    GAPS |= cells("read", slugs, users_, wheres, (404, 403))
FAIL_OPEN_GAPS |= cells("read", ALL, ["conditioned"], ["b", ["a", "b"]], (200, 403))
GAPS |= cells("delete", ALL, ["anonymous"], STORED_IN, (404, 403))
GAPS |= cells("delete", ALL, ["conditioned"], ["a", SECRET], (404, 403))
for user in (
    "reader-a",
    "writer-a",
    "lister-a",
    "workflow-a",
    "publisher-a",
    "retracter-a",
):
    GAPS |= cells(
        "delete", [s for s in ALL if s != "hooked-notes"], [user], ["b"], (404, 403)
    )
    GAPS |= cells("delete", ["hooked-notes"], [user], ["b", SECRET], (404, 403))
# A user with no list grant: the facade lists (200), DRF refuses.
FAIL_OPEN_GAPS |= cells(
    "list",
    ALL,
    [name for name in USER_NAMES if name not in {"superuser", "lister-a", "writer-ab"}],
    [None],
    (200, 403),
)
# A write: DRF refuses (403) a body whose contexts are not granted before it
# validates it; the facade answers the object's 404 or its own 400.
GAPS |= cells("write", ALL, ["anonymous", "conditioned"], BODY_CONTEXTS, (404, 403))
GAPS |= cells(
    "write",
    ["hooked-notes"],
    ["superuser", "writer-a", "workflow-a", "writer-ab", "publisher-a", "retracter-a"],
    [["a", "b"]],
    (400, 403),
)
GAPS |= cells(
    "write",
    ["workflow-notes"],
    ["writer-a", "publisher-a", "workflow-a", "retracter-a"],
    [["a", "b"]],
    (400, 403),
)
GAPS |= cells(
    "write",
    ["todos"],
    ["writer-a", "publisher-a", "workflow-a", "retracter-a"],
    ["", "b", ["a", "b"]],
    (400, 403),
)
GAPS |= cells("write", ["todos"], ["writer-ab"], [""], (400, 403))
GAPS |= cells(
    "write", ["workflow-notes"], ["retracter-a", "workflow-a"], ["", "b"], (400, 403)
)


def check_parity(request, case, facade, drf):
    """Assert `facade` equals `drf` for `case`, or, for a pinned divergence,
    that it is the pinned pair, then fail as a strict xfail."""
    pinned = GAPS.get(case) or FAIL_OPEN_GAPS.get(case)
    if pinned is None:
        assert facade == drf, (facade, drf)
        return
    reason = {"list": LIST_GRANT_GAP, "write": BODY_REQUEST_GAP}.get(
        case[0], ITEM_REQUEST_GAP
    )
    if case in FAIL_OPEN_GAPS:
        reason = f"{FAIL_OPEN_REASON}; {reason}"
    request.applymarker(
        pytest.mark.xfail(strict=True, raises=AssertionError, reason=reason)
    )
    if (facade, drf) != pinned:
        raise GapDrift(f"{case}: pinned {pinned}, now {(facade, drf)}")
    assert facade == drf, (facade, drf)


@pytest.mark.django_db
@pytest.mark.parametrize("stored_in", STORED_IN, ids=str)
@pytest.mark.parametrize("user_name", USER_NAMES)
@pytest.mark.parametrize("type_slug", TYPE_SLUGS)
def test_item_read_parity(type_slug, user_name, stored_in, request):
    parity = TYPES[type_slug]
    user = users_for(parity.uri)[user_name]
    resource = store(parity, stored_in)
    drf = drf_authz_status(drf_status(parity, user, "GET", resource))
    facade = facade_load(parity, user, resource, "GET")
    check_parity(
        request, ("read", type_slug, user_name, key_of(stored_in)), facade, drf
    )


@pytest.mark.django_db
@pytest.mark.parametrize("stored_in", STORED_IN, ids=str)
@pytest.mark.parametrize("user_name", USER_NAMES)
@pytest.mark.parametrize("type_slug", TYPE_SLUGS)
def test_item_delete_parity(type_slug, user_name, stored_in, request):
    parity = TYPES[type_slug]
    user = users_for(parity.uri)[user_name]
    resource = store(parity, stored_in)
    facade = facade_load(parity, user, resource, "DELETE")
    drf = drf_authz_status(drf_status(parity, user, "DELETE", resource))
    check_parity(
        request,
        ("delete", type_slug, user_name, key_of(stored_in)),
        facade,
        200 if drf == 204 else drf,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("stored_in", STORED_IN, ids=str)
@pytest.mark.parametrize("user_name", USER_NAMES)
@pytest.mark.parametrize("type_slug", TYPE_SLUGS)
def test_allow_parity(type_slug, user_name, stored_in):
    parity = TYPES[type_slug]
    user = users_for(parity.uri)[user_name]
    resource = store(parity, stored_in)
    resource_type = resource_type_of(parity)
    requirement = requirement_for(resource_type, parity.slug)
    response = drf_status(parity, user, "OPTIONS", resource)
    request = request_of("OPTIONS", user)
    if response.status_code == 404:
        with pytest.raises(Http404):
            allowed_methods(request, resource_type, resource, requirement)
        return
    assert response.status_code == 204
    allow = [method.strip() for method in response["Allow"].split(",")]
    assert sorted(
        allowed_methods(request, resource_type, resource, requirement)
    ) == sorted(allow)


def list_response(parity, user):
    return client_of(user).get(reverse(f"{parity.slug}-list"), HTTP_ACCEPT=JSON)


def listed_pks(response):
    return {
        entry["href"].rstrip("/").rsplit("/", 1)[1]
        for entry in response.json()["content"]
    }


def facade_list(parity, user):
    resource_type = resource_type_of(parity)
    return readable_queryset(
        request_of("GET", user),
        resource_type,
        requirement_for(resource_type, parity.slug),
    )


# The users whose DRF list is a 200 (the others hold no grant on the list
# type, a 403, or are anonymous, a 401); each test asserts it of the DRF
# route, so a change in the grants is not missed.
LISTERS = {slug: {"superuser", "lister-a", "writer-ab"} for slug in TYPE_SLUGS}


def list_cases(*, listed: bool):
    """The ``(type, user)`` pairs whose DRF list is a 200 (`listed`) or a
    403/401, from `LISTERS`."""
    return [
        (slug, name)
        for slug in TYPE_SLUGS
        for name in USER_NAMES
        if (name in LISTERS[slug]) is listed
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("type_slug, user_name", list_cases(listed=True))
def test_list_parity(type_slug, user_name):
    parity = TYPES[type_slug]
    user = users_for(parity.uri)[user_name]
    for where in STORED_IN:
        store(parity, where)
    response = list_response(parity, user)
    assert response.status_code == 200, response.content
    assert {str(r.pk) for r in facade_list(parity, user)} == listed_pks(response)


@pytest.mark.django_db
@pytest.mark.parametrize("type_slug, user_name", list_cases(listed=False))
def test_list_refusal_parity(type_slug, user_name, request):
    """The DRF list is refused (no list grant); the facade's `readable_queryset`
    does not ask it yet, so it does not raise."""
    parity = TYPES[type_slug]
    user = users_for(parity.uri)[user_name]
    for where in STORED_IN:
        store(parity, where)
    status = list_response(parity, user).status_code
    assert status in (401, 403)
    facade = outcome_of(lambda: list(facade_list(parity, user)))
    check_parity(request, ("list", type_slug, user_name, None), facade, 403)


@pytest.mark.django_db
@pytest.mark.parametrize("body_context", BODY_CONTEXTS, ids=str)
@pytest.mark.parametrize("user_name", USER_NAMES)
@pytest.mark.parametrize("type_slug", TYPE_SLUGS)
def test_write_parity(type_slug, user_name, body_context, request):
    """A PUT of `body_context`'s body on a resource in "a"."""
    parity = TYPES[type_slug]
    user = users_for(parity.uri)[user_name]
    resource = store(parity, "a")
    resource_type = resource_type_of(parity)
    requirement = requirement_for(resource_type, parity.slug)
    body = parity.body(body_context)

    def facade_put():
        put = request_of("PUT", user)
        stored = load_for_read(
            put, resource_type, resource.pk, requirement=requirement, method="PUT"
        )
        validate_write(
            put,
            resource_type,
            body,
            stored=stored,
            requirement=requirement,
            readable=lambda: object_readable(put, resource_type, stored, requirement),
        )
        update(
            put, resource_type, stored, body, requirement=requirement, using="default"
        )

    with transaction.atomic():
        facade = outcome_class(outcome_of(facade_put))
        transaction.set_rollback(True)
    response = drf_status(parity, user, "PUT", resource, body=body)
    check_parity(
        request,
        ("write", type_slug, user_name, key_of(body_context)),
        facade,
        drf_authz_status(response),
    )


def more_permissive(pair):
    """Whether the facade `pair` ``(facade, drf)`` allows what DRF refuses."""
    facade, drf = pair
    return facade == 200 and drf != 200


def test_only_fail_open_gaps_are_more_permissive():
    """A pinned divergence where the facade allows what DRF refuses is in
    `FAIL_OPEN_GAPS`, and only those."""
    assert not set(GAPS) & set(FAIL_OPEN_GAPS)
    loose = [case for case, pair in GAPS.items() if more_permissive(pair)]
    assert not loose, loose
    tight = [case for case, pair in FAIL_OPEN_GAPS.items() if not more_permissive(pair)]
    assert not tight, tight


@pytest.mark.django_db
def test_workflow_note_publish_is_allowed_by_both():
    """The workflow note's success path: the publisher's PUT of the published
    note in "a" (an authorized transition) is a 200 on DRF, and the facade
    allows it; the retracter's is refused (400) by both."""
    parity = TYPES["workflow-notes"]
    matrix = users_for(parity.uri)
    resource = store(parity, "a")
    body = parity.body("a")
    assert (
        drf_status(
            parity, matrix["publisher-a"], "PUT", resource, body=body
        ).status_code
        == 200
    )
    assert (
        drf_status(
            parity, matrix["retracter-a"], "PUT", resource, body=body
        ).status_code
        == 400
    )


@pytest.mark.django_db
@pytest.mark.parametrize("type_slug", TYPE_SLUGS)
def test_requirement_for_leaves_are_trusted_to_ask_the_grants(type_slug):
    """Every leaf of `requirement_for` is a `GrantsLeaf` (asking the grants
    itself) or a type's hooks: the core asks no grants beside them, so the
    cases above answer as they did before it trusted leaves by class
    (`register_grant_asking_leaf`)."""
    parity = TYPES[type_slug]
    requirement = requirement_for(resource_type_of(parity), parity.slug)
    assert asks_grants(requirement)
    for leaf in requirement_leaves(requirement):
        assert asks_grants_itself(leaf) or is_hook(leaf), leaf
