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
    # May not GET it: a 404 whatever the method.
    other = store("b")
    for method in ("GET", "PUT", "DELETE"):
        with pytest.raises(Http404):
            load_for_read(
                request_of(method, matrix["reader-a"]),
                resource_type,
                other.pk,
                requirement=requirement,
                method=method,
            )
    # Anonymous: a 404.
    for method in ("GET", "PUT"):
        with pytest.raises(Http404):
            load("anonymous", method)
    # A conditioned reader of "b" reads nothing in "a".
    with pytest.raises(Http404):
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
