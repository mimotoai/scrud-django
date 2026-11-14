"""An item without DRF (`scrud_django.core.resources`): its lookup and
object check (`load_for_read`: a refusal of an object the requester cannot
read is a 404, of one they can a 403), whether it is readable, its
validators and Links, and the ``Allow`` of an OPTIONS (`allowed_methods`),
asked with a plain Django request and a neutral requirement, as the DRF
routes answer them."""

from uuid import uuid4

import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import Http404
from django.test import RequestFactory
from django.urls import reverse
from django.utils.timezone import now
from rest_framework import status
from rest_framework.request import Request
from rest_framework.test import APIClient
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access
from scoped_rbac.models import Role, RoleAssignment

from scrud_django import collection_type_uri_for
from scrud_django.core.authz import content_of
from scrud_django.core.requirements import GrantsLeaf
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration, get_resource_type_for

from .factories import UserFactory
from .steps import *  # noqa: F403, F401

JSON_LD = "http://www.w3.org/ns/json-ld#context"
JSON_LD_LIST = collection_type_uri_for(JSON_LD)
SCRUD_WORKFLOW = {
    "condition": {"operator": "scrud_workflow", "authorized_transitions": ["any"]}
}
CONTEXTS = ["", "a", "b", ["a", "b"]]


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


def users():
    """Task 2's user matrix (`tests.test_core_requirements.users`): a
    superuser, a reader in "a", a reader whose grants are conditioned, a
    writer in "a" and the anonymous user; and a lister and writer of the
    list in "a", and a user who may PUT and DELETE in "a" only through the
    workflow check (``scrud_workflow``)."""
    conditioned = {"http.GET": {JSON_LD: {"condition": 'rbac_context == "b"'}}}
    return {
        "superuser": user_with(superuser=True),
        "reader-a": user_with(({"http.GET": [JSON_LD]}, "a")),
        "conditioned": user_with((conditioned, "a"), (conditioned, "b")),
        "writer-a": user_with(
            (
                {"http.GET": [JSON_LD], "http.POST": [JSON_LD], "http.PUT": [JSON_LD]},
                "a",
            )
        ),
        "anonymous": AnonymousUser(),
        "lister-a": user_with(
            (
                {
                    "http.GET": [JSON_LD, JSON_LD_LIST],
                    "http.POST": [JSON_LD, JSON_LD_LIST],
                },
                "a",
            )
        ),
        "workflow-a": user_with(
            (
                {
                    "http.GET": [JSON_LD],
                    "http.PUT": {JSON_LD: SCRUD_WORKFLOW},
                    "http.DELETE": {JSON_LD: SCRUD_WORKFLOW},
                },
                "a",
            )
        ),
    }


def store(context):
    reverse("json-ld-list")  # importing tests.urls registers the types
    return ResourceRegistration.register({"rbac_context": context}, "json-ld")


def json_ld():
    reverse("json-ld-list")
    return get_resource_type_for("json-ld")


def request_of(method, user, path="/"):
    request = getattr(RequestFactory(), method.lower())(path)
    request.user = user
    return request


def client_of(user):
    client = APIClient()
    if user.is_authenticated:
        client.force_authenticate(user)
    return client


def allow_of(response):
    return [method.strip() for method in response["Allow"].split(",")]


@pytest.mark.django_db
@test_context
def test_load_404_vs_403():
    from scrud_django.core.resources import load_for_read

    resource_type = json_ld()
    requirement = [GrantsLeaf(JSON_LD)]
    note = store("a")
    matrix = users()

    def load(name, method):
        return load_for_read(
            request_of(method, matrix[name]),
            resource_type,
            note.pk,
            requirement=requirement,
            method=method,
        )

    # May GET, may not PUT: a 403 (the object is readable).
    assert load("reader-a", "GET") == note
    with pytest.raises(access.AccessDenied):
        load("reader-a", "PUT")
    with pytest.raises(access.AccessDenied):
        load("reader-a", "DELETE")
    # The object check is `method`'s, whatever the request's.
    with pytest.raises(access.AccessDenied):
        load_for_read(
            request_of("GET", matrix["reader-a"]),
            resource_type,
            note.pk,
            requirement=requirement,
            method="PUT",
        )
    # May not GET it: a 404 for a GET. A PUT or DELETE, which the reader may
    # not make anywhere, is refused by the request check before the lookup:
    # a 403, as on the DRF route (whose request check runs first).
    other = store("b")
    with pytest.raises(Http404):
        load_for_read(
            request_of("GET", matrix["reader-a"]),
            resource_type,
            other.pk,
            requirement=requirement,
            method="GET",
        )
    for method in ("PUT", "DELETE"):
        with pytest.raises(access.AccessDenied):
            load_for_read(
                request_of(method, matrix["reader-a"]),
                resource_type,
                other.pk,
                requirement=requirement,
                method=method,
            )
    # Anonymous: refused by the request check (DRF: 401).
    for method in ("GET", "PUT"):
        with pytest.raises(access.AccessDenied):
            load("anonymous", method)
    # A reader whose grant is conditioned on the object is refused by the
    # request check of an item (scoped-rbac's authorize_request).
    with pytest.raises(access.AccessDenied):
        load("conditioned", "GET")
    assert load("writer-a", "PUT") == note
    assert load("superuser", "DELETE") == note


@pytest.mark.django_db
@test_context
def test_load_covers_the_update_put_only():
    """``scrud_workflow`` allows the object check of a PUT (the update action
    the workflow check covers, `ResourceViewMixin.checks_workflow`), and
    refuses it for a DELETE: a 403, the object being readable."""
    from scrud_django.core.resources import load_for_read

    resource_type = json_ld()
    note = store("a")
    user = users()["workflow-a"]
    kwargs = {"requirement": [GrantsLeaf(JSON_LD)]}
    request = request_of("PUT", user)
    assert (
        load_for_read(request, resource_type, note.pk, method="PUT", **kwargs) == note
    )
    with pytest.raises(access.AccessDenied):
        load_for_read(
            request_of("DELETE", user),
            resource_type,
            note.pk,
            method="DELETE",
            **kwargs,
        )
    # So does the DRF route: its DELETE is a 403; its OPTIONS, whose object
    # checks are the methods' own, lists PUT and not DELETE.
    client = client_of(user)
    url = reverse("json-ld-detail", args=[note.pk])
    assert client.delete(url).status_code == status.HTTP_403_FORBIDDEN
    assert allow_of(client.options(url)) == ["GET", "PUT", "OPTIONS", "HEAD"]


@pytest.mark.django_db
@test_context
def test_load_misses_what_the_detail_route_misses():
    from scrud_django.core.resources import load_for_read

    resource_type = json_ld()
    superuser = users()["superuser"]
    kwargs = {"requirement": [GrantsLeaf(JSON_LD)], "method": "GET"}
    request = request_of("GET", superuser)
    reserved = store("a")
    Resource.objects.filter(pk=reserved.pk).update(content={"rbac_context": "​__x"})
    another_type = ResourceRegistration.register({"x": 1}, "partner-profiles")
    for pk in (reserved.pk, another_type.pk, 10**9, "not-a-key"):
        with pytest.raises(Http404):
            load_for_read(request, resource_type, pk, **kwargs)
        response = client_of(superuser).get(
            reverse("json-ld-detail", args=[pk]), HTTP_ACCEPT="application/json"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.django_db
@test_context
def test_load_caches_the_type_and_reads_where_it_locks():
    from django.db import DEFAULT_DB_ALIAS, transaction

    from scrud_django.core.resources import load_for_read

    resource_type = json_ld()
    note = store("a")
    request = request_of("PUT", users()["superuser"])
    with transaction.atomic(using=DEFAULT_DB_ALIAS):
        loaded = load_for_read(
            request,
            resource_type,
            note.pk,
            requirement=[GrantsLeaf(JSON_LD)],
            method="PUT",
            lock_using=DEFAULT_DB_ALIAS,
        )
    assert loaded._state.db == DEFAULT_DB_ALIAS
    assert Resource.resource_type.field.get_cached_value(loaded) is resource_type


@pytest.mark.django_db
@test_context
def test_object_readable_matches_the_drf_views():
    """Whatever the request's method (a GET copy is asked), as the DRF
    view's ``object_readable`` answers it, which tells its 404 from its
    403."""
    from scrud_django.core.resources import object_readable
    from scrud_django.views import ResourceViewSet

    resource_type = json_ld()
    requirement = [GrantsLeaf(JSON_LD)]
    matrix = users()
    answers = set()
    for context in CONTEXTS:
        note = store(context)
        for name, user in matrix.items():
            for method, action in (
                ("GET", "retrieve"),
                ("PUT", "update"),
                ("DELETE", "destroy"),
            ):
                drf = Request(request_of(method, user))
                drf.user = user
                view = ResourceViewSet(resource_type_name="json-ld")
                view.request, view.action = drf, action
                expected = view.object_readable(drf, content_of(note))
                readable = object_readable(
                    request_of(method, user), resource_type, note, requirement
                )
                assert readable is expected, (name, context, method)
                answers.add(readable)
    assert answers == {True, False}


@pytest.mark.django_db
@test_context
def test_item_validators_and_links():
    from scrud_django.core.resources import item_links, item_validators

    reverse("partner-profiles-list")
    resource_type = get_resource_type_for("partner-profiles")
    profile = ResourceRegistration.register({"name": "x"}, "partner-profiles")
    assert item_validators(profile) == (profile.etag, profile.modified_at)
    url = reverse("partner-profiles-detail", args=[profile.pk])
    response = client_of(users()["superuser"]).get(url, HTTP_ACCEPT="application/json")
    assert response.status_code == status.HTTP_200_OK
    links = item_links(RequestFactory().get(url), resource_type)
    # The schema's and the context's.
    assert len(links) == 2
    sent = [link.strip() for link in response["Link"].split(", ")]
    for link in links:
        assert link in sent, (link, sent)


@pytest.mark.django_db
@test_context
def test_item_link_targets_are_the_links_targets():
    from scrud_django.core.resources import item_link_targets, item_links

    reverse("partner-profiles-list")
    resource_type = get_resource_type_for("partner-profiles")
    request = RequestFactory().get("/")
    schema, context = item_link_targets(request, resource_type)

    assert schema.startswith("http://testserver/")
    assert context.startswith("http://testserver/")
    first, second = item_links(request, resource_type)
    assert first.startswith(f"<{schema}>;")
    assert second.startswith(f"<{context}>;")
    # Without a request or a namespace, the paths of the global routes.
    assert item_link_targets(None, resource_type) == (
        schema.removeprefix("http://testserver"),
        context.removeprefix("http://testserver"),
    )


@pytest.mark.django_db
@pytest.mark.urls("tests.parity_urls")
@test_context
def test_item_link_targets_without_a_request_are_the_namespaces():
    # scrudful builds an HTTP Schema Resource without a request: the links of
    # an API's document point at that API's routes (its URL namespace).
    from scrud_django.core.resources import item_link_targets

    from .front_ends import serve_both

    serve_both()
    resource_type = get_resource_type_for("partner-profiles")
    schema, context = item_link_targets(None, resource_type, namespace="scrud-ninja")
    assert schema == f"/ninja/json-schema/{resource_type.schema.pk}/"
    assert context == f"/ninja/json-ld/{resource_type.context.pk}/"
    assert item_link_targets(None, resource_type) == (
        f"/drf/json-schema/{resource_type.schema.pk}/",
        f"/drf/json-ld/{resource_type.context.pk}/",
    )


@pytest.mark.django_db
@test_context
def test_allowed_methods_match_drf_options():
    from scrud_django.core.resources import allowed_methods

    resource_type = json_ld()
    item = [GrantsLeaf(JSON_LD)]
    notes = [store(context) for context in CONTEXTS]
    seen = set()
    for name, user in users().items():
        client = client_of(user)
        url = reverse("json-ld-list")
        response = client.options(url)
        assert response.status_code == status.HTTP_204_NO_CONTENT
        # The list's GET is checked on the list's type, its POST on the item's.
        core = allowed_methods(
            request_of("OPTIONS", user, url), resource_type, None, item
        )
        assert core == allow_of(response), (name, "list")
        seen.add(("list", tuple(core)))
        for note in notes:
            url = reverse("json-ld-detail", args=[note.pk])
            response = client.options(url)
            request = request_of("OPTIONS", user, url)
            if response.status_code == status.HTTP_404_NOT_FOUND:
                # The first method's object check hides the object.
                with pytest.raises(Http404):
                    allowed_methods(request, resource_type, note, item)
                seen.add(("item", 404))
                continue
            assert response.status_code == status.HTTP_204_NO_CONTENT
            core = allowed_methods(request, resource_type, note, item)
            assert core == allow_of(response), (name, note.content)
            seen.add(("item", tuple(core)))
    # The matrix reaches every kind of answer.
    assert seen >= {
        ("list", ("OPTIONS",)),
        ("list", ("GET", "POST", "OPTIONS", "HEAD")),
        ("item", ("OPTIONS",)),
        ("item", ("GET", "OPTIONS", "HEAD")),
        ("item", ("GET", "PUT", "OPTIONS", "HEAD")),
        ("item", ("GET", "PUT", "DELETE", "OPTIONS", "HEAD")),
        ("item", 404),
    }, seen


@pytest.mark.django_db
@test_context
def test_allowed_methods_of_a_reserved_object_is_a_404():
    """Once a method passes its request check, the object is loaded: one
    stored in a reserved context is missing (a 404), as on the DRF route.
    Refused every method, the requester gets ``Allow: OPTIONS``."""
    from scrud_django.core.resources import allowed_methods

    resource_type = json_ld()
    note = store("a")
    Resource.objects.filter(pk=note.pk).update(content={"rbac_context": "​__x"})
    note.refresh_from_db()
    url = reverse("json-ld-detail", args=[note.pk])
    matrix = users()
    response = client_of(matrix["superuser"]).options(url)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    with pytest.raises(Http404):
        allowed_methods(
            request_of("OPTIONS", matrix["superuser"], url),
            resource_type,
            note,
            [GrantsLeaf(JSON_LD)],
        )
    response = client_of(matrix["anonymous"]).options(url)
    assert allow_of(response) == ["OPTIONS"]
    assert allowed_methods(
        request_of("OPTIONS", matrix["anonymous"], url),
        resource_type,
        note,
        [GrantsLeaf(JSON_LD)],
    ) == ["OPTIONS"]


@pytest.mark.django_db
@test_context
def test_resources_reexports_the_list_and_write_functions():
    from scrud_django.core import lists, resources, writes

    for name in (
        "readable_queryset",
        "content_filtered",
        "list_etag",
        "list_last_modified",
        "evaluate_list_preconditions",
    ):
        assert getattr(resources, name) is getattr(lists, name)
    for name in ("create", "update", "destroy", "validate_write"):
        assert getattr(resources, name) is getattr(writes, name)


@pytest.mark.django_db
@test_context
def test_drf_views_keep_their_refusals_and_their_not_path():
    """The DRF view's ``check_grants_in_each_context`` refuses with DRF's own
    ``PermissionDenied``, a neutral requirement or not; a view whose ``~``
    wraps an ``IsAuthorized`` (`NotNeutral`) keeps its view-based path for
    it and for ``object_readable``."""
    from rest_framework.exceptions import PermissionDenied
    from scoped_rbac.permissions import IsAuthorized

    from scrud_django.views import ResourceViewSet

    from .urls import ContextDenyingIsAuthorized

    reverse("json-ld-list")
    content = content_of(store("a"))
    reader_a = user_with(({"http.GET": [JSON_LD]}, "a"))
    reader_b = user_with(({"http.GET": [JSON_LD]}, "b"))

    def view_of(permission, user):
        drf = Request(request_of("GET", user))
        drf.user = user
        view = ResourceViewSet(resource_type_name="json-ld")
        view.permission_classes = [permission]
        view.request, view.action = drf, "retrieve"
        return view, drf

    for permission in (
        ContextDenyingIsAuthorized,
        IsAuthorized & ~ContextDenyingIsAuthorized,
    ):
        view, drf = view_of(permission, reader_a)
        view.check_grants_in_each_context(drf, content)
        view, drf = view_of(permission, reader_b)
        with pytest.raises(PermissionDenied):
            view.check_grants_in_each_context(drf, content)
    for user, readable in ((reader_a, True), (reader_b, False)):
        view, drf = view_of(~IsAuthorized, user)
        assert view.object_readable(drf, content) is readable
        assert "get_permissions" not in view.__dict__
        assert view.request is drf and view.action == "retrieve"


@pytest.mark.django_db
@test_context
def test_hooks_checks_must_allow_in_every_stored_context():
    """A type's ``checks`` must allow a PUT or DELETE in each context the
    resource is stored in (not any, as an ``IsAuthorized``'s object check):
    a hooked note in ["a", "b"] whose check refuses "b" is refused (403, it
    is readable) for a writer granted in both."""
    from scrud_django.core.resources import check_object, load_for_read
    from scrud_django.core.types import requirement_for

    from .fixtures import assign_role
    from .resource_types import HOOKED_NOTE_TYPE_URI, hooked_resource_types

    reverse("hooked-notes-list")
    hooked_resource_types()
    hooked = get_resource_type_for("hooked-notes")
    requirement = requirement_for(hooked, "hooked-notes")
    both = ResourceRegistration.register(
        {"text": "in both", "rbac_context": ["a", "b"]}, "hooked-notes"
    )
    grants = [HOOKED_NOTE_TYPE_URI, collection_type_uri_for(HOOKED_NOTE_TYPE_URI)]
    writer = assign_role(
        {m: grants for m in ("http.GET", "http.PUT", "http.DELETE")}, "a", "b"
    )
    for method in ("PUT", "DELETE"):
        with pytest.raises(access.AccessDenied):
            check_object(
                request_of(method, writer), hooked, both, requirement, covered=False
            )
        with pytest.raises(access.AccessDenied):
            load_for_read(
                request_of(method, writer),
                hooked,
                both.pk,
                requirement=requirement,
                method=method,
            )
    assert (
        load_for_read(
            request_of("GET", writer),
            hooked,
            both.pk,
            requirement=requirement,
            method="GET",
        )
        == both
    )


class RefusingRequests:
    """A leaf (of an untrusted class: the grants are asked beside it) whose
    request check refuses every request and whose object check allows every
    object."""

    type_iri = JSON_LD
    reads_each_context = False
    overrides_object_checks = False

    def contexts_for(self, obj):
        return [obj.get("rbac_context", "")]

    def has_permission(self, request, body):
        return False

    def has_object_permission(self, request, obj):
        return True

    def has_context_permission(self, request, obj, context):
        return True


class RefusingGrantsLeaf(GrantsLeaf):
    """A trusted leaf (a `GrantsLeaf` subclass overriding neither
    ``has_context_permission`` nor ``reads_each_context``) whose request
    check refuses every request."""

    def has_permission(self, request, body):
        return False


@pytest.mark.django_db
@test_context
def test_load_for_read_asks_the_request_check():
    """A requirement ``[GrantsLeaf(JSON_LD), RefusingRequests()]``: its
    request check refuses (DRF's ``has_permission`` refusal, 403), so
    `load_for_read` raises ``access.AccessDenied`` whatever the object
    check allows, before the lookup: a missing key is refused alike."""
    from scrud_django.core.resources import load_for_read

    resource_type = json_ld()
    note = store("a")
    superuser = users()["superuser"]
    for method in ("GET", "PUT", "DELETE"):
        request = request_of(method, superuser)
        assert (
            load_for_read(
                request,
                resource_type,
                note.pk,
                requirement=[GrantsLeaf(JSON_LD)],
                method=method,
            )
            == note
        )
        for pk in (note.pk, note.pk + 1000, "not-a-key"):
            with pytest.raises(access.AccessDenied):
                load_for_read(
                    request,
                    resource_type,
                    pk,
                    requirement=[GrantsLeaf(JSON_LD), RefusingRequests()],
                    method=method,
                )


@pytest.mark.django_db
@test_context
def test_load_for_read_refuses_at_request_level_before_the_lookup():
    """A requester the method's request check refuses (anonymous, a reader
    whose grant is conditioned on the object, a reader asking for a
    DELETE) gets a 403 for an existing and a missing key alike, as on the
    DRF route (``initial()`` asks the permissions before ``get_object()``):
    nothing tells whether the key exists."""
    from scrud_django.core.resources import load_for_read

    resource_type = json_ld()
    matrix = users()
    stored = [store("a"), store("b")]
    missing = stored[-1].pk + 1000
    for name, method in (
        ("anonymous", "GET"),
        ("anonymous", "DELETE"),
        ("conditioned", "GET"),
        ("reader-a", "DELETE"),
        ("lister-a", "PUT"),
    ):
        user = matrix[name]
        for pk in (*(r.pk for r in stored), missing):
            with pytest.raises(access.AccessDenied):
                load_for_read(
                    request_of(method, user),
                    resource_type,
                    pk,
                    requirement=[GrantsLeaf(JSON_LD)],
                    method=method,
                )
            url = reverse("json-ld-detail", args=[pk])
            client = client_of(user)
            if method == "GET":
                response = client.get(url, HTTP_ACCEPT="application/json")
            elif method == "PUT":
                response = client.put(url, {"rbac_context": "a"}, format="json")
            else:
                response = client.delete(url)
            expected = 401 if name == "anonymous" else 403
            assert response.status_code == expected, (name, method, pk)


@pytest.mark.django_db
@test_context
def test_load_for_read_checks_the_body_it_is_given():
    """With a `body`, a PUT's request check reads the contexts it writes
    (DRF's, with ``request.data``), before the lookup; without one it is
    the body-less check ``Allow`` asks (`allowed_methods`), whether the
    requester may PUT in some context."""
    from scrud_django.core.resources import load_for_read

    resource_type = json_ld()
    matrix = users()
    note = store("a")

    def load(name, pk, **kwargs):
        return load_for_read(
            request_of("PUT", matrix[name]),
            resource_type,
            pk,
            requirement=[GrantsLeaf(JSON_LD)],
            method="PUT",
            **kwargs,
        )

    assert load("writer-a", note.pk) == note
    assert load("writer-a", note.pk, body={"rbac_context": "a"}) == note
    for body in ({"rbac_context": "b"}, {"rbac_context": ["a", "b"]}, {}):
        for pk in (note.pk, note.pk + 1000):
            with pytest.raises(access.AccessDenied):
                load("writer-a", pk, body=body)
    # A grant conditioned on scrud_workflow passes the request check of the
    # update's PUT, which the workflow check covers, as on DRF.
    assert load("workflow-a", note.pk, body={"rbac_context": "a"}) == note
    assert load("workflow-a", note.pk) == note


@pytest.mark.django_db
@test_context
def test_check_request():
    """The request check of a route: the list's GET or HEAD asks the list
    grant and each entry's GET check; any other request (an item's, or a
    POST to the list with its body) each entry's check of it. A
    requirement asking no grants is ``ImproperlyConfigured``."""
    from django.core.exceptions import ImproperlyConfigured

    from scrud_django.core.resources import check_request

    resource_type = json_ld()
    matrix = users()
    requirement = [GrantsLeaf(JSON_LD)]

    def allows(name, method, *, list_route, body=None, requirement=requirement):
        try:
            check_request(
                request_of(method, matrix[name]),
                resource_type,
                requirement,
                list_route=list_route,
                body=body,
            )
        except access.AccessDenied:
            return False
        return True

    list_only = user_with(({"http.GET": [JSON_LD_LIST]}, "a"))
    for method in ("GET", "HEAD"):
        assert allows("lister-a", method, list_route=True)
        assert allows("superuser", method, list_route=True)
        # The item's grant alone does not list; the list's grant alone does
        # (each grants leaf is asked as its list type, as DRF names it on
        # the list action), the list then filtered by the item grants.
        assert not allows("reader-a", method, list_route=True)
        check_request(
            request_of(method, list_only), resource_type, requirement, list_route=True
        )
        # A trusted leaf with a request check of its own is asked as it is
        # beside the list grant.
        with pytest.raises(access.AccessDenied):
            check_request(
                request_of(method, matrix["superuser"]),
                resource_type,
                [RefusingGrantsLeaf(JSON_LD)],
                list_route=True,
            )
        assert not allows("anonymous", method, list_route=True)
        assert not allows("conditioned", method, list_route=True)
        assert allows("reader-a", method, list_route=False)
        assert not allows("conditioned", method, list_route=False)
        assert not allows(
            "superuser",
            method,
            list_route=True,
            requirement=[GrantsLeaf(JSON_LD), RefusingRequests()],
        )
    # A create (POST to the list) asks the requirement with the body, not
    # the list's GET grant: the writer may create in "a" only.
    for name in ("writer-a", "lister-a"):
        assert allows(name, "POST", list_route=True, body={"rbac_context": "a"})
        assert not allows(name, "POST", list_route=True, body={"rbac_context": "b"})
    # A body that writes no context is checked in the default context "".
    assert not allows("writer-a", "POST", list_route=True, body={})
    assert not allows("reader-a", "POST", list_route=True, body={"rbac_context": "a"})
    # An item's PUT, with its body.
    assert allows("writer-a", "PUT", list_route=False, body={"rbac_context": "a"})
    assert not allows("writer-a", "PUT", list_route=False, body={"rbac_context": "b"})
    assert allows("workflow-a", "PUT", list_route=False, body={"rbac_context": "a"})
    # A DELETE, which the workflow check does not cover: scrud_workflow
    # refuses its request check, as on DRF.
    assert allows("superuser", "DELETE", list_route=False)
    assert not allows("workflow-a", "DELETE", list_route=False)
    assert not allows("writer-a", "DELETE", list_route=False)
    # An AnyOf: any option allowing suffices; an option allows when each of
    # its entries does.
    other = [GrantsLeaf("tests://Other")]
    assert allows(
        "reader-a",
        "GET",
        list_route=False,
        requirement=[access.AnyOf([other, requirement])],
    )
    assert not allows(
        "reader-a",
        "GET",
        list_route=False,
        requirement=[access.AnyOf([other, [*requirement, RefusingRequests()]])],
    )
    with pytest.raises(ImproperlyConfigured, match="asks no grants"):
        check_request(
            request_of("GET", matrix["superuser"]),
            resource_type,
            [],
            list_route=False,
        )


@pytest.mark.django_db
@test_context
def test_validate_write_asks_the_request_check_first():
    """`validate_write` refuses (``access.AccessDenied``, DRF's 403) a body
    whose contexts the requester may not write before it validates it, as
    the DRF route asks ``has_permission`` with the body before its
    serializer's validators. A create asks it of the list's POST."""
    from scrud_django.core.writes import validate_write

    resource_type = json_ld()
    matrix = users()
    note = store("a")
    put = request_of("PUT", matrix["writer-a"])
    for stored, method in ((note, "PUT"), (None, "POST")):
        request = request_of(method, matrix["writer-a"])
        with pytest.raises(access.AccessDenied):
            validate_write(
                request,
                resource_type,
                {"rbac_context": "b"},
                stored=stored,
                requirement=[GrantsLeaf(JSON_LD)],
                readable=lambda: True,
            )
    validate_write(
        put,
        resource_type,
        {"rbac_context": "a"},
        stored=note,
        requirement=[GrantsLeaf(JSON_LD)],
        readable=lambda: True,
    )
    with pytest.raises(access.AccessDenied):
        validate_write(
            put,
            resource_type,
            {"rbac_context": "a"},
            stored=note,
            requirement=[GrantsLeaf(JSON_LD), RefusingRequests()],
            readable=lambda: True,
        )


@pytest.mark.django_db
@test_context
def test_readable_content_takes_no_grants_flag_and_the_private_one_asks_object_checks():
    from scrud_django.core import resources
    from scrud_django.core.types import requirement_for

    from .resource_types import hooked_resource_types

    reverse("hooked-notes-list")
    hooked_resource_types()
    # The hooks alone: with no grants leaf, nothing is asked but the object
    # checks (the public function refuses such a requirement).
    requirement = requirement_for(
        get_resource_type_for("hooked-notes"), "hooked-notes"
    )[1:]
    nobody = user_with()  # holds no grants at all
    read = request_of("GET", nobody)
    with pytest.raises(TypeError):
        resources.readable_content(read, {}, requirement, grants=False)
    # The object checks alone allow what is not secret and refuse what is.
    assert resources._readable_content(read, {"text": "x"}, requirement, grants=False)
    assert not resources._readable_content(
        read, {"text": "x", "secret": True}, requirement, grants=False
    )


@pytest.mark.django_db
@test_context
def test_allowed_methods_of_a_computed_item():
    """A computed type (registered with ``compute=``) serves GET only: its
    ``Allow`` lists no PUT or DELETE even for a superuser, as the DRF route's
    does."""
    from scrud_django.core.resources import allowed_methods, served_methods
    from tests.resource_types import hooked_resource_types

    url = reverse("summaries-detail", args=["k"])
    hooked_resource_types()  # the computed type's row and options
    resource_type = get_resource_type_for("summaries")
    assert served_methods(resource_type, item=True) == ("GET",)
    assert served_methods(resource_type, item=False) == ("GET",)
    assert served_methods(json_ld(), item=True) == ("GET", "PUT", "DELETE")
    assert served_methods(json_ld(), item=False) == ("GET", "POST")
    user = users()["superuser"]
    response = client_of(user).options(url)
    # A computed route answers OPTIONS with DRF's metadata (200), not
    # scrudful's 204; its ``Allow`` is the oracle all the same.
    assert response.status_code == status.HTTP_200_OK
    item = ResourceRegistration.register({"rbac_context": "a"}, "summaries")
    core = allowed_methods(
        request_of("OPTIONS", user, url),
        resource_type,
        item,
        [GrantsLeaf(resource_type.type_uri)],
    )
    assert core == ["GET", "OPTIONS", "HEAD"]
    # DRF's own ``Allow`` (not scrudful's) lists them in its order.
    assert sorted(core) == sorted(allow_of(response))
    # An explicit `methods` overrides the type's.
    assert allowed_methods(
        request_of("OPTIONS", user, url),
        resource_type,
        item,
        [GrantsLeaf(resource_type.type_uri)],
        methods=("GET", "PUT"),
    ) == ["GET", "PUT", "OPTIONS", "HEAD"]
    hooked_resource_types()  # the options as registered, for later tests


@pytest.mark.django_db
@test_context
def test_allowed_methods_do_not_depend_on_the_methods_order_or_case():
    from scrud_django.core.resources import allowed_methods

    resource_type = json_ld()
    note = store("a")
    user = users()["superuser"]
    request = request_of("OPTIONS", user)
    answers = [
        allowed_methods(
            request, resource_type, note, [GrantsLeaf(JSON_LD)], methods=methods
        )
        for methods in (
            ["GET", "PUT"],
            ["put", "get"],
            ["Put", "GET", "get"],
        )
    ]
    assert answers[0] == ["GET", "PUT", "OPTIONS", "HEAD"]
    assert answers[1] == answers[2] == answers[0]


@pytest.mark.django_db
@test_context
def test_allowed_methods_for_matches_the_drf_route():
    """`allowed_methods_for`, given the key rather than the item, answers
    what the DRF route's OPTIONS answers: for the list, a readable item, a
    hidden one and a missing key, as an admin, a reader in "a", a user
    granted nothing and the anonymous user. Refused every method, a
    requester gets ``Allow: OPTIONS`` whether the key exists or not: nothing
    is looked up."""
    from scrud_django.core.resources import allowed_methods_for

    resource_type = json_ld()
    requirement = [GrantsLeaf(JSON_LD)]
    readable, hidden = store("a"), store("b")
    matrix = users()
    requesters = {
        "admin": matrix["superuser"],
        "reader-a": matrix["reader-a"],
        "nothing": user_with(),
        "anonymous": matrix["anonymous"],
    }
    targets = {
        "list": None,
        "readable": str(readable.pk),
        "hidden": str(hidden.pk),
        "missing": "999999999",
        "not-a-key": "x",
    }
    seen = {}
    for name, user in requesters.items():
        client = client_of(user)
        for target, pk in targets.items():
            if pk is None:
                url = reverse("json-ld-list")
            else:
                url = reverse("json-ld-detail", args=[pk])
            response = client.options(url)
            request = request_of("OPTIONS", user, url)
            if response.status_code == status.HTTP_404_NOT_FOUND:
                with pytest.raises(Http404):
                    allowed_methods_for(request, resource_type, pk, requirement)
                seen[name, target] = 404
                continue
            assert response.status_code == status.HTTP_204_NO_CONTENT, (name, target)
            core = allowed_methods_for(request, resource_type, pk, requirement)
            assert core == allow_of(response), (name, target)
            seen[name, target] = tuple(core)
    # DRF's answers, pinned.
    every = ("GET", "PUT", "DELETE", "OPTIONS", "HEAD")
    assert seen == {
        ("admin", "list"): ("GET", "POST", "OPTIONS", "HEAD"),
        ("admin", "readable"): every,
        ("admin", "hidden"): every,
        ("admin", "missing"): 404,
        ("admin", "not-a-key"): 404,
        ("reader-a", "list"): ("OPTIONS",),
        ("reader-a", "readable"): ("GET", "OPTIONS", "HEAD"),
        ("reader-a", "hidden"): 404,
        ("reader-a", "missing"): 404,
        ("reader-a", "not-a-key"): 404,
        **{
            (name, target): ("OPTIONS",)
            for name in ("nothing", "anonymous")
            for target in targets
        },
    }, seen


@pytest.mark.django_db
@test_context
def test_allowed_methods_for_looks_nothing_up_for_a_requester_refused_everything(
    monkeypatch,
):
    """Refused every method's request check, the requester gets ``Allow:
    OPTIONS`` before any lookup: the key's existence is never asked. Once a
    method passes, the item is looked up, a missing one a 404."""
    from scrud_django.core import resources

    resource_type = json_ld()
    requirement = [GrantsLeaf(JSON_LD)]
    looked_up = []
    get = resources.Resource.objects.filter

    def recording_filter(*args, **kwargs):
        looked_up.append(kwargs)
        return get(*args, **kwargs)

    monkeypatch.setattr(resources.Resource.objects, "filter", recording_filter)
    nothing = request_of("OPTIONS", user_with())
    assert resources.allowed_methods_for(
        nothing, resource_type, "999999999", requirement
    ) == ["OPTIONS"]
    assert looked_up == []
    reader = request_of("OPTIONS", users()["reader-a"])
    with pytest.raises(Http404):
        resources.allowed_methods_for(reader, resource_type, "999999999", requirement)
    assert looked_up


class CountingGrantsLeaf(GrantsLeaf):
    """A trusted leaf recording the method of each request check asked."""

    def __init__(self, type_iri, asked):
        super().__init__(type_iri)
        self.asked = asked

    def has_permission(self, request, body):
        self.asked.append(request.method)
        return super().has_permission(request, body)


@pytest.mark.django_db
@test_context
def test_allowed_methods_for_asks_each_request_check_once():
    """Each method's request check is asked once (as on DRF), not again when
    the item's methods are answered (`types.md` S9)."""
    from scrud_django.core.resources import allowed_methods_for

    resource_type = json_ld()
    note = store("a")
    asked = []
    requirement = [CountingGrantsLeaf(JSON_LD, asked)]
    request = request_of("OPTIONS", users()["superuser"])
    assert allowed_methods_for(request, resource_type, str(note.pk), requirement) == [
        "GET",
        "PUT",
        "DELETE",
        "OPTIONS",
        "HEAD",
    ]
    assert sorted(asked) == ["DELETE", "GET", "PUT"]
