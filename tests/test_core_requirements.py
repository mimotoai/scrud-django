"""The neutral requirement (`scrud_django.core.requirements`): what a scrud
front end asks of an object without a DRF view. `GrantsLeaf` answers as
scoped-rbac's ``IsAuthorized`` does on a scrud resource view, and a view's
own permissions (`scrud_django.drf_requirements.requirement_of`, DRF's ``&``
and ``|`` included) narrow a list as the view-based functions of
`scrud_django.filters` narrow it. DRF's ``~`` has no neutral form."""

import json
from uuid import uuid4

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import RequestFactory
from django.urls import reverse
from django.utils.timezone import now
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.permissions import IsAuthorized

from scrud_django import collection_type_uri_for, filters
from scrud_django.models import Resource
from scrud_django.permissions import (
    _not_checking_write,
    checks_read_content,
    content_of,
    reading_content,
)
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .steps import *  # noqa: F403, F401

JSON_LD = "http://www.w3.org/ns/json-ld#context"


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


def store(content):
    reverse("json-ld-list")
    return ResourceRegistration.register(content, "json-ld")


def users():
    """The user matrix: a superuser, a reader in "a", a reader whose grants
    are conditioned (in "a" on the resource being in "b", which it never is
    there; in "b" on it being in "b"), a writer in "a", and the anonymous
    user."""
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
    }


def resource_view(*permission_classes, request=None, action="retrieve"):
    from scrud_django.views import ResourceViewSet

    view = ResourceViewSet(resource_type_name="json-ld")
    if permission_classes:
        view.permission_classes = list(permission_classes)
    view.action = action
    if request is not None:
        view.request = request
    return view


def get_request(user):
    request = RequestFactory().get("/")
    request.user = user
    return request


@pytest.mark.django_db
@test_context
def test_as_get_is_a_shallow_get_copy():
    from scrud_django.core.requirements import as_get

    request = RequestFactory().put("/x/?a=1")
    request.user = user = UserFactory()
    read = as_get(request)
    assert read is not request
    assert read.method == "GET" and request.method == "PUT"
    assert read.user is user and list(read.GET.lists()) == [("a", ["1"])]


@pytest.mark.django_db
@test_context
def test_grants_leaf_matches_is_authorized():
    from scrud_django.core.requirements import GrantsLeaf, as_get, readable_by_grants

    leaf = GrantsLeaf(JSON_LD)
    assert leaf.type_iri == JSON_LD
    assert leaf.overrides_object_checks is False
    assert leaf.reads_each_context is True
    contents = [
        content_of(store({"foo": n, "rbac_context": context}))
        for n, context in enumerate(["", "a", ["a", "b"], "b"])
    ]
    readable = {}
    for name, user in users().items():
        drf = Request(RequestFactory().put("/"))
        drf.user = user
        view = resource_view(request=drf, action="update")
        (is_authorized,) = reading_content([IsAuthorized()])
        for content in contents:
            key = (name, json.dumps(content["rbac_context"]))
            # The view's path, as ListValidatorsMixin.object_readable asks it.
            with (
                _not_checking_write(),
                checks_read_content(view),
                filters.as_detail_read(view, drf) as read,
            ):
                expected = filters.readable_by_grants(view, read, content)
                object_permission = is_authorized.has_object_permission(
                    read, view, content
                )
                in_context = {
                    c: is_authorized.has_context_permission(read, view, content, c)
                    for c in ("", "a", "b")
                }
            request = RequestFactory().put("/")
            request.user = user
            assert readable_by_grants([leaf], as_get(request), content) == expected, key
            assert leaf.has_object_permission(as_get(request), content) == (
                object_permission
            ), key
            assert {
                c: leaf.has_context_permission(as_get(request), content, c)
                for c in ("", "a", "b")
            } == in_context, key
            readable[key] = expected
    # The matrix tells readers apart.
    assert readable[("superuser", '""')] and readable[("superuser", '"b"')]
    assert readable[("reader-a", '"a"')] and readable[("reader-a", '["a", "b"]')]
    assert not readable[("reader-a", '""')] and not readable[("reader-a", '"b"')]
    assert readable[("conditioned", '"b"')]
    assert readable[("conditioned", '["a", "b"]')]  # in "b", as it is there
    assert not readable[("conditioned", '"a"')]
    assert not any(v for (name, _), v in readable.items() if name == "anonymous")


def drf_write(method, user, body, view):
    """A DRF request of `method` with the JSON `body`, as `view`'s
    ``IsAuthorized.has_permission`` reads it."""
    factory = getattr(APIRequestFactory(), method.lower())
    request = Request(factory("/", body, format="json"), parsers=[JSONParser()])
    request.user = user
    view.request = request
    return request


@pytest.mark.django_db
@test_context
def test_grants_leaf_request_check_matches_is_authorized():
    from scrud_django.core.requirements import GrantsLeaf

    reverse("json-ld-list")
    leaf = GrantsLeaf(JSON_LD)
    bodies = [
        {"rbac_context": "a"},
        {"rbac_context": "b"},
        {"rbac_context": ["a", "b"]},
        {},
        "not an object",
    ]
    for name, user in users().items():
        for method, action in (("POST", "create"), ("PUT", "update")):
            for body in bodies:
                view = resource_view(action=action)
                setattr(view, method.lower(), getattr(view, action))
                drf = drf_write(method, user, body, view)
                expected = IsAuthorized().has_permission(drf, view)
                factory = getattr(RequestFactory(), method.lower())
                request = factory(
                    "/", json.dumps(body), content_type="application/json"
                )
                request.user = user
                assert leaf.has_permission(request, body) == expected, (
                    name,
                    method,
                    body,
                )
        view = resource_view(action="retrieve")
        view.get = view.retrieve
        drf = Request(RequestFactory().get("/"))
        drf.user = user
        view.request = drf
        assert leaf.has_permission(get_request(user), None) == (
            IsAuthorized().has_permission(drf, view)
        ), name
    # A body naming a reserved context is refused before any grant, as an
    # access error (the DRF adapter maps it).
    request = RequestFactory().post(
        "/", json.dumps({"rbac_context": "__x"}), content_type="application/json"
    )
    request.user = user_with(superuser=True)
    with pytest.raises(access.ReservedContext):
        leaf.has_permission(request, {"rbac_context": "__x"})


def composed_views():
    """The views whose permissions DRF's ``&`` and ``|`` compose over
    ``IsAuthorized`` subclasses: `tests.urls.AppendingResourceViewSet`
    (``IsAuthorized`` and ``IsAuthorized & IsAuthenticated``), and
    ``IsAuthorized | ContextDenyingIsAuthorized`` as
    `tests.test_list_object_checks` builds it, with two whose object checks
    narrow."""
    from .urls import (
        AppendingResourceViewSet,
        ContextDenyingIsAuthorized,
        LockedDenyingIsAuthorized,
    )

    appending = AppendingResourceViewSet(resource_type_name="json-ld")
    return {
        "appending": appending,
        "or": resource_view(IsAuthorized | ContextDenyingIsAuthorized),
        "and": resource_view(ContextDenyingIsAuthorized & IsAuthenticated),
        "or-narrowing": resource_view(
            LockedDenyingIsAuthorized | ContextDenyingIsAuthorized
        ),
    }


@pytest.mark.django_db
@test_context
def test_view_leaf_composed_permissions():
    from scrud_django.core.requirements import filter_by_object_checks
    from scrud_django.drf_requirements import requirement_of

    stored = {
        "a": store({"n": "a", "rbac_context": "a"}),
        "denied": store({"n": "denied", "rbac_context": "denied"}),
        "a+denied": store({"n": "a+denied", "rbac_context": ["a", "denied"]}),
        "locked-a": store({"n": "locked-a", "rbac_context": "a", "locked": True}),
        "locked-denied": store(
            {"n": "locked-denied", "rbac_context": "denied", "locked": True}
        ),
    }
    names = {resource.pk: name for name, resource in stored.items()}
    reads = {"http.GET": [JSON_LD]}
    readers = {
        "a+denied": user_with((reads, "a"), (reads, "denied")),
        "a": user_with((reads, "a")),
    }
    everything = set(stored)
    in_a = {"a", "a+denied", "locked-a"}
    expected = {
        ("appending", "a+denied"): everything,
        ("appending", "a"): in_a,
        ("or", "a+denied"): everything,
        ("or", "a"): in_a,
        ("and", "a+denied"): in_a,
        ("and", "a"): in_a,
        ("or-narrowing", "a+denied"): everything - {"locked-denied"},
        ("or-narrowing", "a"): in_a,
    }
    queryset = Resource.objects.filter(pk__in=[r.pk for r in stored.values()])
    for (view_name, reader), listed in expected.items():
        request = get_request(readers[reader])
        view = composed_views()[view_name]
        view.action = "list"
        view.request = request
        neutral = filter_by_object_checks(
            queryset, request, requirement_of(view), content_of
        )
        assert {names[pk] for pk in neutral.values_list("pk", flat=True)} == listed, (
            view_name,
            reader,
        )
        assert view.action == "list" and view.request is request
        drf = filters.filter_by_object_checks(
            queryset, request, view, view.rbac_requirement(), content_of
        )
        assert set(drf.values_list("pk", flat=True)) == set(
            neutral.values_list("pk", flat=True)
        ), (view_name, reader)


@pytest.mark.django_db
@test_context
def test_object_check_requirement_of_composed_views():
    from scrud_django.core.requirements import object_check_requirement
    from scrud_django.drf_requirements import ViewLeaf, requirement_of

    views = composed_views()
    for name, narrows in (
        ("appending", False),
        ("or", True),
        ("and", True),
        ("or-narrowing", True),
    ):
        requirement = requirement_of(views[name])
        assert (object_check_requirement(requirement) is not None) is narrows, name
        assert (filters.object_check_requirement(views[name]) is not None) is narrows
    (entry,) = requirement_of(views["or"])
    assert isinstance(entry, access.AnyOf)
    assert [[type(leaf) for leaf in option] for option in entry.options] == [
        [ViewLeaf],
        [ViewLeaf],
    ]


@pytest.mark.django_db
@test_context
def test_not_permission_is_not_neutral():
    from scrud_django.drf_requirements import NotNeutral, requirement_of

    from .urls import ContextDenyingIsAuthorized

    reverse("json-ld-list")
    for permission in (
        ~IsAuthorized,
        IsAuthorized & ~ContextDenyingIsAuthorized,
        IsAuthorized | ~IsAuthorized,
    ):
        with pytest.raises(NotNeutral):
            requirement_of(resource_view(permission))
    # A ~ over no IsAuthorized is neutral: it requires nothing of RBAC.
    assert requirement_of(resource_view(~IsAuthenticated)) == []
    # The view-based functions keep DRF's path for such a view: without a
    # requirement, any IsAuthorized under ~ reading the object decides.
    content = content_of(store({"rbac_context": "a"}))
    for user, readable in (
        (user_with(({"http.GET": [JSON_LD]}, "a")), True),
        (user_with(({"http.GET": [JSON_LD]}, "b")), False),
    ):
        drf = Request(RequestFactory().get("/"))
        drf.user = user
        view = resource_view(~IsAuthorized, request=drf)
        with checks_read_content(view), filters.as_detail_read(view, drf) as read:
            assert filters.readable_by_grants(view, read, content) is readable


OTHER = "tests://Other"


class RequestRefusingIsAuthorized(IsAuthorized):
    """An ``IsAuthorized`` whose request check refuses every request; its
    object check is ``IsAuthorized``'s."""

    def has_permission(self, request, view):
        return False


class LackedIsAuthorized(IsAuthorized):
    """An ``IsAuthorized`` as a grant the requester lacks answers: it refuses
    the request and every object."""

    def has_permission(self, request, view):
        return False

    def has_object_permission(self, request, view, obj):
        return False


class ObjectRefusingIsAuthorized(IsAuthorized):
    """An ``IsAuthorized`` whose object check refuses every object; its
    request check is ``IsAuthorized``'s."""

    def has_object_permission(self, request, view, obj):
        return False


def request_refusing_leaf(type_iri):
    """A trusted leaf (a `GrantsLeaf` subclass) refusing every request, its
    object check the grants'."""
    from scrud_django.core.requirements import GrantsLeaf

    class RequestRefusingLeaf(GrantsLeaf):
        def has_permission(self, request, body):
            return False

    return RequestRefusingLeaf(type_iri)


def object_refusing_leaf(type_iri):
    """A trusted leaf (a `GrantsLeaf` subclass) refusing every object, its
    request check the grants'."""
    from scrud_django.core.requirements import GrantsLeaf

    class ObjectRefusingLeaf(GrantsLeaf):
        overrides_object_checks = True

        def has_object_permission(self, request, obj):
            return False

    return ObjectRefusingLeaf(type_iri)


def drf_detail_status(permission, user, pk, method="GET"):
    """The status of a `method` of json-ld's detail route `pk` on a view
    whose permission is `permission` (DRF's composition), for `user`."""
    from rest_framework.test import force_authenticate

    from scrud_django.views import ResourceViewSet

    class Composed(ResourceViewSet):
        permission_classes = [permission]

    factory = getattr(APIRequestFactory(), method.lower())
    request = factory("/", HTTP_ACCEPT="application/json")
    force_authenticate(request, user)
    detail = Composed.as_view(
        {"get": "retrieve", "delete": "destroy"}, resource_type_name="json-ld"
    )
    return detail(request, slug=pk).status_code


def core_status(call):
    """The status the facade's refusal by `call` maps to (``None`` for
    none)."""
    from django.http import Http404

    try:
        call()
    except Http404:
        return 404
    except access.AccessDenied:
        return 403
    return None


@pytest.mark.django_db
@test_context
def test_anyof_option_refused_at_request_level_is_not_satisfied():
    """An ``AnyOf`` option is satisfied only when each of its entries allows
    the request (``has_permission``) and then the object, as DRF's ``|``
    asks ``op.has_permission`` before ``op.has_object_permission``: an
    option whose request check refuses does not allow the object, whatever
    its object check says. The first option here refuses the request and
    allows the object; the second is a grant the reader lacks: the facade
    refuses the object (`check_object`) and its route (`load_for_read`)
    with the status DRF's ``|`` of the equivalent ``IsAuthorized``
    subclasses gives. With a second option allowing the request and
    refusing the object, both refuse too, and the facade's list leaves the
    object out."""
    from scrud_django.core import resources
    from scrud_django.core.requirements import GrantsLeaf, meets_object_checks
    from scrud_django.registration import get_resource_type_for

    stored = store({"rbac_context": "a"})
    rtype = get_resource_type_for("json-ld")
    reads = {"http.GET": [JSON_LD, collection_type_uri_for(JSON_LD)]}
    reader = user_with((reads, "a"))
    request = get_request(reader)
    first = request_refusing_leaf(JSON_LD)
    # The first option's object check alone allows the object.
    assert first.has_object_permission(request, content_of(stored))

    lacked = [access.AnyOf([[first], [GrantsLeaf(OTHER)]])]
    checked = core_status(
        lambda: resources.check_object(request, rtype, stored, lacked, covered=False)
    )
    assert checked in (403, 404)
    drf = drf_detail_status(
        RequestRefusingIsAuthorized | LackedIsAuthorized, reader, stored.pk
    )
    assert drf == 403
    assert checked == drf
    routed = core_status(
        lambda: resources.load_for_read(
            request, rtype, stored.pk, requirement=lacked, method="GET"
        )
    )
    assert routed == drf

    refusing = [access.AnyOf([[first], [object_refusing_leaf(JSON_LD)]])]
    # A pinned divergence, stricter on the facade (follow-up
    # drf-anyof-list-object-check-only): its readability asks each option's
    # request check, as DRF's own detail GET (|) does, so the object is
    # unreadable and its refusal a 404; DRF's readability asks the object
    # checks alone (the first option's allows), so DRF answers 403.
    assert (
        core_status(
            lambda: resources.check_object(
                request, rtype, stored, refusing, covered=False
            )
        )
        == 404
    )
    assert (
        core_status(
            lambda: resources.load_for_read(
                request, rtype, stored.pk, requirement=refusing, method="GET"
            )
        )
        == 404
    )
    assert (
        drf_detail_status(
            RequestRefusingIsAuthorized | ObjectRefusingIsAuthorized, reader, stored.pk
        )
        == 403
    )
    assert not resources.readable_queryset(request, rtype, refusing).exists()
    # DRF's own path is unchanged: its object checks ask no request check.
    assert meets_object_checks(refusing, request, content_of(stored))
    # An option allowing both still allows the object.
    allowing = [access.AnyOf([[first], [GrantsLeaf(JSON_LD)]])]
    resources.check_object(request, rtype, stored, allowing, covered=False)
    assert (
        resources.load_for_read(
            request, rtype, stored.pk, requirement=allowing, method="GET"
        )
        == stored
    )
    assert list(
        resources.readable_queryset(request, rtype, allowing).values_list(
            "pk", flat=True
        )
    ) == [stored.pk]


@pytest.mark.django_db
@test_context
def test_anyof_option_request_check_of_a_put_reads_its_body():
    """An ``AnyOf`` option's request check of a PUT reads the body
    `load_for_read` (or `check_object`) is given, as DRF's ``|`` reads
    ``request.data``: a writer in "a" may PUT a body writing "a", not one
    writing "b"; without a body it is the check ``Allow`` asks, whether the
    writer may PUT in some context."""
    from scrud_django.core import resources
    from scrud_django.core.requirements import GrantsLeaf
    from scrud_django.registration import get_resource_type_for

    stored = store({"rbac_context": "a"})
    rtype = get_resource_type_for("json-ld")
    writer = users()["writer-a"]
    request = RequestFactory().put("/")
    request.user = writer
    requirement = [access.AnyOf([[GrantsLeaf(OTHER)], [GrantsLeaf(JSON_LD)]])]

    def check(**kwargs):
        resources.check_object(
            request, rtype, stored, requirement, covered=True, **kwargs
        )

    check(body={"rbac_context": "a"})
    check()
    with pytest.raises(access.AccessDenied):
        check(body={"rbac_context": "b"})
    for body in ({"rbac_context": "a"}, None):
        assert (
            resources.load_for_read(
                request,
                rtype,
                stored.pk,
                requirement=requirement,
                method="PUT",
                body=body,
            )
            == stored
        )


@pytest.mark.django_db
@test_context
def test_not_neutral_compares_the_permissions_not_their_count():
    from scrud_django.drf_requirements import NotNeutral, requirement_of

    required, hidden = IsAuthorized(), IsAuthorized()

    class OffsetView:
        """A view whose requirement repeats a permission while another one
        (under a ``~``) is left out: as many leaves as permissions, but not
        the same ones."""

        def rbac_requirement(self):
            return [required, required]

        def _rbac_permissions(self):
            return [required, hidden]

    with pytest.raises(NotNeutral):
        requirement_of(OffsetView())
