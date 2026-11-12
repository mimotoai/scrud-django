"""A parity harness: the core facade (`scrud_django.core.resources`,
`lists`, `writes`) against the DRF routes it mirrors, for each type of
`TYPES` (a plain one, one with hooks, one under a workflow policy), each user
of `users_for` and each place a resource is stored. The DRF views do not call
the facade, so only a comparison like this one sees them drift apart.

The helpers (`outcome_of`, `request_of`, `users_for`, `store`, `drf_status`...)
are module level so that later cases can reuse them. The DRF PUT and DELETE
send ``If-Match`` with the resource's etag (scrudful answers 428 without it):
the harness compares authorization, not preconditions."""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import transaction
from django.http import Http404
from django.test import RequestFactory
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.permissions import drf_error

from scrud_django import collection_type_uri_for
from scrud_django.core.lists import readable_queryset
from scrud_django.core.resources import allowed_methods, load_for_read, object_readable
from scrud_django.core.types import requirement_for
from scrud_django.core.writes import update, validate_write
from scrud_django.exceptions import ScrudException
from scrud_django.registration import ResourceRegistration, get_resource_type_for

from .factories import UserFactory
from .resource_types import HOOKED_NOTE_TYPE_URI, hooked_resource_types
from .steps import *  # noqa: F403, F401
from .test_core_resources import JSON_LD, SCRUD_WORKFLOW, users

TODO = "tests://ToDo"
JSON = "application/json"
STORED_IN = ["a", "b", ["a", "b"]]
BODY_CONTEXTS = ["", "a", "b", ["a", "b"]]
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


@pytest.fixture(autouse=True)
def registered_types(db):
    """The test types registered (rows and hooks), the hooked ones again
    afterwards."""
    reverse("hooked-notes-list")
    hooked_resource_types()
    yield
    hooked_resource_types()


def user_with(*grants, superuser=False):
    """A user holding each ``(role definition, context)`` of `grants`."""
    user = UserFactory(is_staff=superuser, is_superuser=superuser)
    for definition, context in grants:
        role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
        role.save()
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=context,
            etag=uuid4().hex,
            modified_at=now(),
        ).save()
    return user


def users_for(uri):
    """`tests.test_core_resources.users` for the type `uri` (that matrix
    grants ``JSON_LD`` only), with a deleter in "a" and a writer in "a" and
    "b" besides: the same names, so a case can name a user for any type."""
    if uri == JSON_LD:
        matrix = users()
    else:
        listed = collection_type_uri_for(uri)
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
                (
                    {
                        "http.GET": [uri, listed],
                        "http.POST": [uri, listed],
                    },
                    "a",
                )
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
    full = {f"http.{m}": [uri, collection_type_uri_for(uri)] for m in _METHODS}
    matrix["deleter-a"] = user_with(({"http.GET": [uri], "http.DELETE": [uri]}, "a"))
    matrix["writer-ab"] = user_with((full, "a"), (full, "b"))
    return matrix


_METHODS = ("GET", "POST", "PUT", "DELETE")


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


def store(parity: ParityType, context):
    return ResourceRegistration.register(parity.content(context), parity.slug)


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
    """The DRF detail route's status for `method`, in a savepoint rolled
    back. A 401 of an anonymous requester is the 403 the facade names."""
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
    status = response.status_code
    return 403 if status == 401 else status


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


# Known divergences, each a strict xfail so that the task that closes one
# must remove its entry. The DRF route asks the method's request check
# (``has_permission``) before it looks the object up; `load_for_read` asks the
# object check alone. Keys are (user, stored_in) and are the same for every
# type.
READ_GAP = {
    ("conditioned", "a"),
    ("conditioned", "b"),
    ("conditioned", ("a", "b")),
    ("anonymous", "a"),
    ("anonymous", "b"),
    ("anonymous", ("a", "b")),
}
DELETE_GAP = {
    ("reader-a", "b"),
    ("conditioned", "a"),
    ("writer-a", "b"),
    ("anonymous", "a"),
    ("anonymous", "b"),
    ("anonymous", ("a", "b")),
    ("lister-a", "b"),
    ("workflow-a", "b"),
}
# (type, user, body_context) of `test_write_parity`: the body's request check
# refuses (403) before the facade's own answer (400 or 404).
WRITE_GAP = {
    (slug, user, key_of(ctx))
    for slug in TYPES
    for user in ("conditioned", "anonymous")
    for ctx in BODY_CONTEXTS
}
WRITE_GAP |= {
    ("hooked-notes", user, ("a", "b"))
    for user in ("superuser", "writer-a", "workflow-a", "writer-ab")
}
WRITE_GAP |= {
    ("todos", user, key_of(ctx))
    for user in ("writer-a", "workflow-a")
    for ctx in ("", "b", ["a", "b"])
}
WRITE_GAP.add(("todos", "writer-ab", ""))


def expect_gap(request, gap, key, reason):
    """Mark the running test a strict xfail when `key` is a known gap."""
    if key in gap:
        request.applymarker(pytest.mark.xfail(strict=True, reason=reason))


# The matrix is built inside each test (users are rows), so the parameters
# are names.
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
]
TYPE_SLUGS = list(TYPES)


@pytest.mark.django_db
@pytest.mark.parametrize("stored_in", STORED_IN, ids=str)
@pytest.mark.parametrize("user_name", USER_NAMES)
@pytest.mark.parametrize("type_slug", TYPE_SLUGS)
def test_item_read_parity(type_slug, user_name, stored_in, request):
    expect_gap(request, READ_GAP, (user_name, key_of(stored_in)), ITEM_REQUEST_GAP)
    parity = TYPES[type_slug]
    user = users_for(parity.uri)[user_name]
    resource = store(parity, stored_in)
    response = drf_status(parity, user, "GET", resource)
    facade = facade_load(parity, user, resource, "GET")
    assert facade == drf_authz_status(response), (facade, response.status_code)


@pytest.mark.django_db
@pytest.mark.parametrize("stored_in", STORED_IN, ids=str)
@pytest.mark.parametrize("user_name", USER_NAMES)
@pytest.mark.parametrize("type_slug", TYPE_SLUGS)
def test_item_delete_parity(type_slug, user_name, stored_in, request):
    expect_gap(request, DELETE_GAP, (user_name, key_of(stored_in)), ITEM_REQUEST_GAP)
    parity = TYPES[type_slug]
    user = users_for(parity.uri)[user_name]
    resource = store(parity, stored_in)
    facade = facade_load(parity, user, resource, "DELETE")
    status = drf_authz_status(drf_status(parity, user, "DELETE", resource))
    assert facade == (200 if status == 204 else status), (facade, status)


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
    403/401, from `LIST_STATUS`."""
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
    for context in STORED_IN:
        store(parity, context)
    response = list_response(parity, user)
    assert response.status_code == 200, response.content
    assert {str(r.pk) for r in facade_list(parity, user)} == listed_pks(response)


@pytest.mark.django_db
@pytest.mark.xfail(strict=True, reason=LIST_GRANT_GAP)
@pytest.mark.parametrize("type_slug, user_name", list_cases(listed=False))
def test_list_refusal_parity(type_slug, user_name):
    """The DRF list is refused (no list grant); the facade's `readable_queryset`
    does not ask it yet, so it does not raise."""
    parity = TYPES[type_slug]
    user = users_for(parity.uri)[user_name]
    for context in STORED_IN:
        store(parity, context)
    assert list_response(parity, user).status_code in (401, 403)
    assert outcome_of(lambda: list(facade_list(parity, user))) == 403


@pytest.mark.django_db
@pytest.mark.parametrize("body_context", BODY_CONTEXTS, ids=str)
@pytest.mark.parametrize("stored_in", ["a"])
@pytest.mark.parametrize("user_name", USER_NAMES)
@pytest.mark.parametrize("type_slug", TYPE_SLUGS)
def test_write_parity(type_slug, user_name, stored_in, body_context, request):
    expect_gap(
        request,
        WRITE_GAP,
        (type_slug, user_name, key_of(body_context)),
        BODY_REQUEST_GAP,
    )
    parity = TYPES[type_slug]
    user = users_for(parity.uri)[user_name]
    resource = store(parity, stored_in)
    resource_type = resource_type_of(parity)
    requirement = requirement_for(resource_type, parity.slug)
    body = parity.body(body_context)

    def facade_put():
        request = request_of("PUT", user)
        stored = load_for_read(
            request,
            resource_type,
            resource.pk,
            requirement=requirement,
            method="PUT",
        )
        validate_write(
            request,
            resource_type,
            body,
            stored=stored,
            requirement=requirement,
            readable=lambda: object_readable(
                request, resource_type, stored, requirement
            ),
        )
        update(
            request,
            resource_type,
            stored,
            body,
            requirement=requirement,
            using="default",
        )

    with transaction.atomic():
        facade = outcome_class(outcome_of(facade_put))
        transaction.set_rollback(True)
    drf = outcome_class(
        drf_authz_status(drf_status(parity, user, "PUT", resource, body=body))
    )
    assert facade == drf, (facade, drf)
