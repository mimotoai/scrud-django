"""Lists are RBAC-filtered: a list shows exactly the resources a detail GET
shows the requester (``http.GET`` on the item type in any of a resource's
usable contexts), and its validators (``ETag``, ``Last-Modified``) come from
what the requester may see."""

import json
from uuid import uuid4

import pytest
from django.conf import settings
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils.timezone import now
from rest_framework import status
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment

from scrud_django import collection_type_uri_for
from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    workflow_policy_resource_type,
)

from .factories import UserFactory
from .fixtures import *  # noqa: F401, F403
from .fixtures import RESOURCE_ENDPOINT_LIST_NAME
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES
from .steps import *  # noqa: F403, F401

PARTNER = TYPE_URI_PARTNER_PROFILES
PARTNER_LIST = TYPE_URI_LIST_PARTNER_PROFILES
READER = {"http.GET": [PARTNER, PARTNER_LIST]}
TODO = "tests://ToDo"
WORKFLOW_POLICY = "http://api.openteams.com/json-ld/WorkflowPolicy"
POLICY_READER = {
    "http.GET": [WORKFLOW_POLICY, collection_type_uri_for(WORKFLOW_POLICY)]
}
PARTNER_WRITER = {
    method: [PARTNER, PARTNER_LIST]
    for method in ("http.GET", "http.POST", "http.PUT", "http.DELETE")
}


def assign(user, definition, context):
    role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
    role.save()
    RoleAssignment(
        user=user, role=role, rbac_context=context, etag=uuid4().hex, modified_at=now()
    ).save()
    return user


def user_with(*grants):
    """A user holding each ``(role definition, context)`` of `grants`."""
    user = UserFactory()
    for definition, context in grants:
        assign(user, definition, context)
    return user


def profile(name, **content):
    """A partner profile stored server-side (no API check), named `name`."""
    reverse(RESOURCE_ENDPOINT_LIST_NAME)  # registers the test types' routes
    return ResourceRegistration.register(
        dict({"name": name, "slug": name}, **content), "partner-profiles"
    )


def detail_url(resource, prefix="partner-profiles"):
    return reverse(f"{prefix}-detail", kwargs={"slug": resource.pk})


def listed_names(path=None, **params):
    """GET the partner profiles list (as the authenticated user); return the
    names it shows (every page of it) and its ``count``."""
    When.http.get(path or reverse(RESOURCE_ENDPOINT_LIST_NAME), params)
    Then.http.status_code_is(status.HTTP_200_OK)
    body = json.loads(Http.response.content)
    return {entry["content"]["name"] for entry in body["content"]}, body["count"]


def readable_on_detail(resources):
    """The names of `resources` a detail GET answers 200 for (else 404)."""
    readable = set()
    for resource in resources:
        When.http.get(detail_url(resource))
        assert Http.response.status_code in (200, 404), Http.response.content
        if Http.response.status_code == 200:
            readable.add(resource.content["name"])
    return readable


@pytest.mark.django_db
@test_context
def test_list_shows_only_resources_readable_in_the_users_contexts():
    in_a = profile("in-a", rbac_context="a")
    in_b = profile("in-b", rbac_context="b")
    in_a_and_b = profile("in-a-and-b", rbac_context=["a", "b"])
    in_default = profile("in-default")
    reader = user_with((READER, "b"))
    Given.http.force_authenticate(reader)
    names, count = listed_names()
    assert names == {"in-b", "in-a-and-b"}
    assert count == 2
    # The list shows exactly what the detail route shows.
    assert readable_on_detail([in_a, in_b, in_a_and_b, in_default]) == names


@pytest.mark.django_db
@test_context
def test_list_with_get_in_no_context_is_403():
    # The view-level check needs http.GET on the list somewhere: without it the
    # list is refused before any resource is looked at.
    profile("in-b", rbac_context="b")
    Given.http.force_authenticate(user_with(({"http.POST": [PARTNER]}, "b")))
    When.http.get(reverse(RESOURCE_ENDPOINT_LIST_NAME))
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)


@pytest.mark.django_db
@test_context
def test_list_in_a_context_without_resources_is_an_empty_page():
    profile("in-a", rbac_context="a")
    Given.http.force_authenticate(user_with((READER, "elsewhere")))
    names, count = listed_names()
    assert names == set()
    assert count == 0


@pytest.mark.django_db
@test_context
def test_conditional_grant_lists_matching_resources_and_outright_ones_are_uncapped():
    # GET outright in "o" (with the list), and only by a condition in "c".
    conditional_reader = {"http.GET": {PARTNER: {"condition": 'name == "keep"'}}}
    user = user_with((READER, "o"), (conditional_reader, "c"))
    outright = [profile(f"o-{n}", rbac_context="o") for n in range(3)]
    keep = profile("keep", rbac_context="c")
    drop = profile("drop", rbac_context="c")
    # In "o" too: granted outright, so not a conditional candidate.
    both = profile("both", rbac_context=["o", "c"])
    rbac = dict(settings.SCOPED_RBAC, CONDITIONAL_LIST_MAX_ROWS=2)
    with override_settings(SCOPED_RBAC=rbac):
        Given.http.force_authenticate(user)
        names, count = listed_names()
        # Four outright rows, past the cap of 2, are listed: only the two
        # conditional candidates count.
        assert names == {"o-0", "o-1", "o-2", "keep", "both"}
        assert count == 5
        assert readable_on_detail([*outright, keep, drop, both]) == names

        # A third candidate in "c" is past the cap.
        profile("drop-too", rbac_context="c")
        When.http.get(reverse(RESOURCE_ENDPOINT_LIST_NAME))
        Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
        assert Http.response.json()["code"] == "conditional_list_too_large"


@pytest.mark.django_db
@test_context
def test_reserved_and_contextless_resources(admin_login):
    reserved = profile("reserved", rbac_context="__x")
    reserved_star = profile("reserved-star", rbac_context="*")
    reserved_in_list = profile("reserved-in-list", rbac_context=["__x"])
    contextless = profile("contextless", rbac_context=[])
    mixed = profile("mixed", rbac_context=["__x", "b"])
    plain = profile("plain", rbac_context="b")
    default = profile("default")
    null_context = profile("null-context", rbac_context=None)
    resources = [
        reserved,
        reserved_star,
        reserved_in_list,
        contextless,
        mixed,
        plain,
        default,
        null_context,
    ]

    # A user with grants in "b": only the usable context counts.
    Given.http.force_authenticate(user_with((READER, "b")))
    names, _ = listed_names()
    assert names == {"mixed", "plain"}
    assert readable_on_detail(resources) == names

    # A user whose grant applies in every context ("*") but who is not a
    # superuser: a resource with no usable context is granted by no role.
    anywhere = dict(
        settings.SCOPED_RBAC,
        POLICY_FOR_AUTHENTICATED="tests.rbac_defaults."
        "policy_read_partner_profiles_anywhere",
    )
    with override_settings(SCOPED_RBAC=anywhere):
        Given.http.force_authenticate(UserFactory())
        names, _ = listed_names()
        assert {"mixed", "plain", "default", "null-context"} <= names
        assert names.isdisjoint(
            {"reserved", "reserved-star", "reserved-in-list", "contextless"}
        )
        assert readable_on_detail(resources) == {
            "mixed",
            "plain",
            "default",
            "null-context",
        }

    # A superuser: a resource with no usable context is listed (as its detail
    # route shows it); one stored in a reserved context is never listed (its
    # detail route is a 404 to everyone).
    Given.http.force_authenticate(admin_login)
    names, _ = listed_names()
    superuser_reads = {
        "reserved-in-list",
        "contextless",
        "mixed",
        "plain",
        "default",
        "null-context",
    }
    assert superuser_reads <= names
    assert names.isdisjoint({"reserved", "reserved-star"})
    assert readable_on_detail(resources) == superuser_reads


@pytest.mark.django_db
@test_context
def test_workflow_policy_list_hides_other_contexts_policies():
    workflow_policy_resource_type()
    reverse("workflow-policies-list")
    policies = {
        context: ResourceRegistration.register(
            {
                "resource_type": TODO,
                "rbac_context": context,
                "initial_states": {"any": "true"},
                "states": {},
                "transitions": {},
            },
            "workflow-policies",
        )
        for context in ("a", "b")
    }
    Given.http.force_authenticate(user_with((POLICY_READER, "b")))
    When.http.get(reverse("workflow-policies-list"))
    Then.http.status_code_is(status.HTTP_200_OK)
    body = json.loads(Http.response.content)
    hrefs = {entry["href"] for entry in body["content"]}
    assert hrefs == {
        "http://testserver" + detail_url(policies["b"], "workflow-policies")
    }
    assert body["count"] == 1
    When.http.get(detail_url(policies["a"], "workflow-policies"))
    Then.http.status_code_is(status.HTTP_404_NOT_FOUND)


@pytest.mark.django_db
@test_context
def test_list_validators_change_only_with_what_the_requester_sees():
    profile("in-b", rbac_context="b")
    in_a = profile("in-a", rbac_context="a")
    reader = user_with((READER, "b"))
    writer = user_with((PARTNER_WRITER, "a"), (PARTNER_WRITER, "b"))
    url = reverse(RESOURCE_ENDPOINT_LIST_NAME)

    Given.http.force_authenticate(reader)
    When.http.get(url)
    Then.http.status_code_is(status.HTTP_200_OK)
    etag = Http.response["ETag"]
    last_modified = Http.response["Last-Modified"]

    # Writes in "a", which the reader cannot read, change nothing they see.
    ResourceRegistration.update(
        dict(in_a.content, display_name="changed"), "partner-profiles", in_a.pk
    )
    profile("new-in-a", rbac_context="a")
    Given.http.force_authenticate(writer)
    When.http.delete(
        detail_url(profile("gone-in-a", rbac_context="a")), HTTP_IF_MATCH="*"
    )
    Then.http.status_code_is(status.HTTP_204_NO_CONTENT)

    Given.http.force_authenticate(reader)
    When.http.get(url)
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Http.response["ETag"] == etag
    assert Http.response["Last-Modified"] == last_modified
    When.http.get(url, HTTP_IF_NONE_MATCH=etag)
    Then.http.status_code_is(status.HTTP_304_NOT_MODIFIED)

    # A write in "b" changes the reader's list and its ETag.
    profile("new-in-b", rbac_context="b")
    When.http.get(url, HTTP_IF_NONE_MATCH=etag)
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Http.response["ETag"] != etag


@pytest.mark.django_db
@test_context
def test_list_etag_follows_an_update_of_a_listed_resource():
    in_b = profile("in-b", rbac_context="b")
    url = reverse(RESOURCE_ENDPOINT_LIST_NAME)
    Given.http.force_authenticate(user_with((READER, "b")))
    When.http.get(url)
    etag = Http.response["ETag"]
    ResourceRegistration.update(
        dict(in_b.content, display_name="changed"), "partner-profiles", in_b.pk
    )
    When.http.get(url, HTTP_IF_NONE_MATCH=etag)
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Http.response["ETag"] != etag


@pytest.mark.django_db
@test_context
def test_list_etag_differs_between_pages():
    for n in range(3):
        profile(f"p-{n}", rbac_context="b")
    url = reverse(RESOURCE_ENDPOINT_LIST_NAME)
    Given.http.force_authenticate(user_with((READER, "b")))
    When.http.get(url, {"page_size": 2})
    first = Http.response["ETag"]
    When.http.get(url, {"page_size": 2, "page": 2})
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Http.response["ETag"] != first


def list_queries(user, count):
    """The queries of one list request by `user` after storing `count`
    profiles in "b"."""
    for _ in range(count):
        profile(f"q-{uuid4().hex}", rbac_context="b")
    Given.http.force_authenticate(user)
    with CaptureQueriesContext(connection) as queries:
        When.http.get(reverse(RESOURCE_ENDPOINT_LIST_NAME))
    Then.http.status_code_is(status.HTTP_200_OK)
    return len(queries)


@pytest.mark.django_db
@test_context
def test_list_queries_do_not_grow_with_the_page():
    user = user_with((READER, "b"), ({"http.GET": [PARTNER]}, "c"))
    few = list_queries(user, 2)
    many = list_queries(user, 30)
    # No query per listed resource; the fixed cost (the user's roles, the
    # resource type, the count, the page, the newest modified_at) is bounded.
    assert many == few, (few, many)
    assert many <= 20, many


@pytest.mark.django_db
@test_context
def test_conditional_list_queries_do_not_grow_with_the_page():
    conditional_reader = {"http.GET": {PARTNER: {"condition": "true"}}}
    user = user_with((READER, "o"), (conditional_reader, "b"))
    few = list_queries(user, 2)
    many = list_queries(user, 30)
    # No query per listed resource; the fixed cost (the user's roles, the
    # resource type, the count, the page, the newest modified_at) is bounded.
    assert many == few, (few, many)
    assert many <= 20, many


@pytest.mark.django_db
@test_context
def test_filtered_list_is_rbac_filtered_and_filters_on_query_parameters():
    profile("keep", rbac_context="b", display_name="Keep")
    profile("other", rbac_context="b", display_name="Other")
    profile("hidden", rbac_context="a", display_name="Keep")
    path = reverse("filtered-partner-profiles-list")
    Given.http.force_authenticate(user_with((READER, "b")))
    names, count = listed_names(path)
    assert names == {"keep", "other"}
    assert count == 2
    names, count = listed_names(path, display_name="Keep")
    assert names == {"keep"}
    assert count == 1
    # Pagination parameters are not content filters.
    names, count = listed_names(path, display_name="Keep", page=1, page_size=10)
    assert names == {"keep"}


@pytest.mark.django_db
@test_context
def test_filtered_list_parameters_are_keys_not_lookups():
    profile("keep", rbac_context="b")
    path = reverse("filtered-partner-profiles-list")
    Given.http.force_authenticate(user_with((READER, "b")))
    # A key, never a lookup: no content has the key "name__startswith".
    names, count = listed_names(path, name__startswith="k")
    assert names == set()
    assert count == 0
    names, _ = listed_names(path, contains="k")
    assert names == set()
    # An integer would be read as an array index.
    When.http.get(path, {"0": "k"})
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)


@pytest.mark.django_db
def test_resource_rows_are_never_listed_through_an_unfiltered_backend():
    # A ResourceViewMixin viewset without an RBAC filter backend is refused
    # rather than listing every resource.
    from django.core.exceptions import ImproperlyConfigured
    from rest_framework.test import APIRequestFactory, force_authenticate

    from scrud_django.views import ResourceViewSet

    class Unfiltered(ResourceViewSet):
        filter_backends = []

    profile("in-a", rbac_context="a")
    request = APIRequestFactory().get("/partner-profiles/")
    force_authenticate(request, user=UserFactory(is_superuser=True))
    view = Unfiltered.as_view({"get": "list"}, resource_type_name="partner-profiles")
    with pytest.raises(ImproperlyConfigured):
        view(request)
    assert Resource.objects.filter(content__name="in-a").exists()
