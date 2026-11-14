"""A list without DRF (`scrud_django.core.lists`): the resources a requester
may list, filtered on query parameters, and the list's validators and
preconditions, asked with a plain Django request and a neutral requirement,
as the DRF routes answer them."""

import datetime
from uuid import uuid4

import pytest
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import QueryDict
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.http import quote_etag
from rest_framework.test import APIClient
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access
from scoped_rbac.models import Role, RoleAssignment

from scrud_django import collection_type_uri_for
from scrud_django.core.requirements import GrantsLeaf
from scrud_django.exceptions import ScrudException
from scrud_django.filters import ui_parameters
from scrud_django.models import Resource, ResourceRemoval
from scrud_django.registration import ResourceRegistration, get_resource_type_for

from .factories import UserFactory
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES
from .steps import *  # noqa: F403, F401

PP = TYPE_URI_PARTNER_PROFILES
PP_READER = {"http.GET": [PP, TYPE_URI_LIST_PARTNER_PROFILES]}
AN_HOUR_AGO = datetime.timedelta(hours=1)


def user_with(*grants):
    """A user holding each ``(role definition, context)`` of `grants`."""
    user = UserFactory()
    for definition, context in grants:
        role = Role(definition=definition, etag=uuid4().hex, modified_at=timezone.now())
        role.save()
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=context,
            etag=uuid4().hex,
            modified_at=timezone.now(),
        ).save()
    return user


def profile(name, context):
    """A partner profile in `context`, stored an hour ago."""
    reverse("partner-profiles-list")
    resource = ResourceRegistration.register(
        {"name": name, "rbac_context": context}, "partner-profiles"
    )
    Resource.objects.filter(pk=resource.pk).update(
        modified_at=timezone.now() - AN_HOUR_AGO
    )
    resource.refresh_from_db()
    return resource


def get_request(user, path="/"):
    request = RequestFactory().get(path)
    request.user = user
    return request


class RefusingLeaf(GrantsLeaf):
    """A leaf whose object check refuses the resource named `name`."""

    overrides_object_checks = True

    def __init__(self, type_iri, name):
        super().__init__(type_iri)
        self.name = name

    def has_object_permission(self, request, obj):
        return obj.get("name") != self.name


@pytest.mark.django_db
@test_context
def test_list_etag_matches_drf():
    from scrud_django.core.lists import list_etag

    for n in range(3):
        profile(f"seen-{n}", "a")
    for n in range(2):
        profile(f"hidden-{n}", "b")
    client = APIClient()
    client.force_authenticate(user_with((PP_READER, "a")))
    response = client.get(reverse("partner-profiles-list") + "?page=2&page_size=1")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["count"] == 3
    (entry,) = body["content"]
    item = Resource.objects.get(content__name=entry["content"]["name"])
    rtype = get_resource_type_for("partner-profiles")
    expected = list_etag(
        rtype, items=[item], count=3, query=[("page", ["2"]), ("page_size", ["1"])]
    )
    assert response["ETag"] == quote_etag(expected)


@pytest.mark.django_db
@test_context
def test_readable_queryset_object_checks():
    from scrud_django.core.lists import readable_queryset

    refused, shown = profile("refused", "a"), profile("shown", "a")
    profile("elsewhere", "b")
    rtype = get_resource_type_for("partner-profiles")
    request = get_request(user_with((PP_READER, "a")))
    # The grants alone: what the RBAC filter lets through.
    plain = readable_queryset(request, rtype, [GrantsLeaf(PP)])
    assert sorted(plain.values_list("pk", flat=True)) == sorted([refused.pk, shown.pk])
    requirement = [GrantsLeaf(PP), RefusingLeaf(PP, "refused")]
    listed = readable_queryset(request, rtype, requirement)
    assert list(listed.values_list("pk", flat=True)) == [shown.pk]
    # The refused resource was examined: it counts against the cap.
    capped = dict(settings.SCOPED_RBAC, CONDITIONAL_LIST_MAX_ROWS=1)
    with override_settings(SCOPED_RBAC=capped):
        with pytest.raises(access.ConditionalListTooLarge):
            readable_queryset(request, rtype, requirement)
        # Without object checks the outright grants list in SQL, uncapped.
        assert readable_queryset(request, rtype, [GrantsLeaf(PP)]).count() == 2


@pytest.mark.django_db
@test_context
def test_content_filtered_ignores_paging_and_ui():
    from scrud_django.core.lists import content_filtered

    reverse("json-ld-list")
    a = ResourceRegistration.register({"text": "a", "rbac_context": "a"}, "json-ld")
    ResourceRegistration.register({"text": "b", "rbac_context": "a"}, "json-ld")
    queryset = Resource.objects.filter(resource_type=a.resource_type)
    ignored = frozenset({"page", "page_size", "format"}) | ui_parameters()
    assert "ui-mode" in ignored
    params = QueryDict("page=1&ui-mode=edit&text=a")
    filtered = content_filtered(queryset, params, ignored=ignored)
    assert list(filtered.values_list("pk", flat=True)) == [a.pk]
    # An integer key is a 400, as the DRF filter answers it.
    with pytest.raises(ScrudException) as raised:
        content_filtered(queryset, QueryDict("1=x"), ignored=ignored)
    assert raised.value.status_code == 400
    assert raised.value.errors == {
        "1": ["An integer is not a content key a list can be filtered on."]
    }


@pytest.mark.django_db
@test_context
def test_list_last_modified_includes_removal():
    from scrud_django.core.lists import list_last_modified, readable_queryset

    older, gone = profile("older", "a"), profile("gone", "a")
    Resource.objects.filter(pk=older.pk).update(
        modified_at=timezone.now() - 2 * AN_HOUR_AGO
    )
    rtype = get_resource_type_for("partner-profiles")
    request = get_request(user_with((PP_READER, "a")))
    requirement = [GrantsLeaf(PP)]

    def last_modified():
        queryset = readable_queryset(request, rtype, requirement)
        return list_last_modified(request, rtype, queryset, requirement)

    before = last_modified()
    gone.refresh_from_db()
    assert before == gone.modified_at
    ResourceRegistration.delete("partner-profiles", gone.pk)
    after = last_modified()
    removal = ResourceRemoval.objects.filter(resource_type=rtype).get()
    assert after == removal.removed_at > before
    # A removal in a context the requester does not read does not move it.
    other = get_request(user_with((PP_READER, "b")))
    assert (
        list_last_modified(
            other, rtype, readable_queryset(other, rtype, requirement), requirement
        )
        is None
    )


@pytest.mark.django_db
@test_context
def test_evaluate_list_preconditions_reads_validators_only_when_needed():
    from scrud_django.core.lists import evaluate_list_preconditions

    calls = []

    def validators():
        calls.append(1)
        return "abc", None

    # No list precondition: nothing to evaluate.
    assert evaluate_list_preconditions("POST", {}, validators) is None
    # "*" is decided by the list existing, without its validators.
    assert evaluate_list_preconditions("POST", {"if-match": "*"}, validators) is None
    failed = evaluate_list_preconditions("POST", {"if-none-match": "*"}, validators)
    assert failed.status == 412
    assert calls == []
    # An entity-tag needs them.
    assert (
        evaluate_list_preconditions("POST", {"if-match": '"abc"'}, validators) is None
    )
    stale = evaluate_list_preconditions("POST", {"if-match": '"old"'}, validators)
    assert stale.status == 412
    assert calls == [1, 1]


JSON_LD = "http://www.w3.org/ns/json-ld#context"
JSON_LD_LIST = collection_type_uri_for(JSON_LD)


def json_ld_in(*contexts):
    """A JSON-LD resource in each of `contexts`, and the type."""
    reverse("json-ld-list")  # importing tests.urls registers the types
    stored = [
        ResourceRegistration.register({"rbac_context": context}, "json-ld")
        for context in contexts
    ]
    return get_resource_type_for("json-ld"), stored


def request_by(method, user):
    request = getattr(RequestFactory(), method.lower())("/")
    request.user = user
    return request


def drf_list_status(user):
    client = APIClient()
    if user.is_authenticated:
        client.force_authenticate(user)
    return client.get(reverse("json-ld-list"), HTTP_ACCEPT="application/json")


@pytest.mark.django_db
@test_context
def test_readable_queryset_asks_the_list_grant():
    """A user granted ``http.GET`` on JSON_LD in "a" but not on its list
    type: the DRF list is a 403, and `readable_queryset` of a GET or HEAD
    refuses (``access.AccessDenied``) rather than list the resource in "a".
    The anonymous user is refused too (DRF: 401)."""
    from scrud_django.core.lists import readable_queryset

    rtype, _ = json_ld_in("a")
    reader = user_with(({"http.GET": [JSON_LD]}, "a"))
    assert drf_list_status(reader).status_code == 403
    assert drf_list_status(AnonymousUser()).status_code == 401
    for user in (reader, AnonymousUser()):
        for method in ("GET", "HEAD"):
            with pytest.raises(access.AccessDenied):
                readable_queryset(
                    request_by(method, user), rtype, [GrantsLeaf(JSON_LD)]
                )


@pytest.mark.django_db
@test_context
def test_readable_queryset_lists_for_the_lister():
    """ "lister-a" (``http.GET`` on JSON_LD and its list type in "a") gets
    the resource in "a" only, as the DRF list."""
    from scrud_django.core.lists import readable_queryset

    rtype, (in_a, _, _) = json_ld_in("a", "b", ["b", "c"])
    lister = user_with(({"http.GET": [JSON_LD, JSON_LD_LIST]}, "a"))
    response = drf_list_status(lister)
    assert response.status_code == 200, response.content
    assert [entry["content"] for entry in response.json()["content"]] == [in_a.content]
    for method in ("GET", "HEAD"):
        listed = readable_queryset(
            request_by(method, lister), rtype, [GrantsLeaf(JSON_LD)]
        )
        assert list(listed.values_list("pk", flat=True)) == [in_a.pk]


@pytest.mark.django_db
@test_context
def test_readable_queryset_asks_the_requirements_request_checks():
    """Beside the list grant, each entry of the requirement must allow the
    list's GET (``has_permission``): a leaf refusing it refuses the list,
    whatever the grants allow, a superuser's included."""
    from scrud_django.core.lists import readable_queryset

    rtype, (in_a,) = json_ld_in("a")
    superuser = UserFactory(is_staff=True, is_superuser=True)
    request = request_by("GET", superuser)
    assert (
        readable_queryset(request, rtype, [GrantsLeaf(JSON_LD)])
        .filter(pk=in_a.pk)
        .exists()
    )
    with pytest.raises(access.AccessDenied):
        readable_queryset(request, rtype, [GrantsLeaf(JSON_LD), RefusingGrantsLeaf()])


@pytest.mark.django_db
@test_context
def test_readable_queryset_of_a_write_asks_no_list_grant():
    """A write's list validators (a conditional create's): `readable_queryset`
    given a POST asks no list grant, the write's own request check
    (`validate_write`'s) governing it, as DRF evaluates them without it. A
    creator in "a" without the list grant gets the resources of its GET
    copy, those in "a"; a requirement asking no grants is still refused."""
    from django.core.exceptions import ImproperlyConfigured

    from scrud_django.core.lists import readable_queryset

    rtype, (in_a, _) = json_ld_in("a", "b")
    creator = user_with(({"http.GET": [JSON_LD], "http.POST": [JSON_LD]}, "a"))
    request = request_by("POST", creator)
    listed = readable_queryset(request, rtype, [GrantsLeaf(JSON_LD)])
    assert list(listed.values_list("pk", flat=True)) == [in_a.pk]
    with pytest.raises(access.AccessDenied):
        readable_queryset(request_by("GET", creator), rtype, [GrantsLeaf(JSON_LD)])
    with pytest.raises(ImproperlyConfigured, match="asks no grants"):
        readable_queryset(request, rtype, [])


class RefusingRequests:
    """A leaf (of an untrusted class) whose request check refuses every
    request and whose object check allows every object."""

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
    ``has_context_permission`` nor ``contexts_for``) whose request check
    refuses every request."""

    def __init__(self, type_iri=JSON_LD):
        super().__init__(type_iri)

    def has_permission(self, request, body):
        return False


class OwnContexts(GrantsLeaf):
    """A `GrantsLeaf` reading contexts of its own, not a resource's stored
    ones."""

    def contexts_for(self, obj):
        return ["a"]


@pytest.mark.django_db
@test_context
def test_readable_queryset_refuses_a_leaf_with_its_own_contexts():
    """The SQL filter and the removal stamps read a resource's stored
    contexts (its ``ResourceContext`` rows): `readable_queryset` and
    `list_last_modified` refuse (``ImproperlyConfigured``), whatever the
    method or the requester, a requirement with a leaf that may read others,
    an ``AnyOf`` option's included: a `GrantsLeaf` subclass overriding
    ``contexts_for``, a leaf given one of its own, a leaf of a class that is
    neither a `GrantsLeaf`, a type's hooks (`HookLeaf`) nor a DRF view's
    (`ViewLeaf`), and a `ViewLeaf` whose permission overrides
    ``contexts_for``. A subclass keeping its base's ``contexts_for`` lists."""
    from django.core.exceptions import ImproperlyConfigured

    from scrud_django.core.lists import list_last_modified, readable_queryset
    from scrud_django.core.types import HookLeaf, TypeOptions
    from scrud_django.drf_requirements import requirement_of
    from scrud_django.views import ResourceViewSet

    from .urls import TeamIsAuthorized

    in_a = profile("in-a", "a")
    rtype = get_resource_type_for("partner-profiles")
    given = GrantsLeaf(PP)
    given.contexts_for = lambda obj: ["a"]
    team_view = ResourceViewSet(resource_type_name="partner-profiles")
    team_view.permission_classes = [TeamIsAuthorized]
    team_view.action = "list"
    plain_view = ResourceViewSet(resource_type_name="partner-profiles")
    plain_view.action = "list"
    custom = {
        "subclass": [OwnContexts(PP)],
        "instance": [given],
        "other class": [GrantsLeaf(PP), RefusingRequests()],
        "AnyOf option": [access.AnyOf([[GrantsLeaf(PP)], [OwnContexts(PP)]])],
        "view leaf": requirement_of(team_view),
    }
    queryset = Resource.objects.filter(resource_type=rtype)
    superuser = UserFactory(is_staff=True, is_superuser=True)
    for user in (superuser, user_with((PP_READER, "a")), AnonymousUser()):
        for method in ("GET", "HEAD", "POST"):
            request = request_by(method, user)
            plain_view.request = team_view.request = request
            for requirement in custom.values():
                with pytest.raises(ImproperlyConfigured, match="contexts"):
                    readable_queryset(request, rtype, requirement)
                with pytest.raises(ImproperlyConfigured, match="contexts"):
                    list_last_modified(request, rtype, queryset, requirement)
    request = request_by("GET", user_with((PP_READER, "a")))
    plain_view.request = request
    hooks = HookLeaf(rtype, TypeOptions())
    for requirement in (
        [RefusingLeaf(PP, "elsewhere"), hooks],
        [access.AnyOf([[GrantsLeaf(PP)], [RefusingLeaf(PP, "x")]])],
        requirement_of(plain_view),
    ):
        listed = readable_queryset(request, rtype, requirement)
        assert list(listed.values_list("pk", flat=True)) == [in_a.pk]
        assert list_last_modified(request, rtype, listed, requirement) == (
            in_a.modified_at
        )
