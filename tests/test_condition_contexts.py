"""A grant's condition that reads ``rbac_context`` reads the context it is
evaluated in (scrud-django#2).

Each check gives the condition the object as it is in that context: its
content with ``rbac_context`` set to that context, when the context is one of
the object's own (``rbac_contexts_of``), so a resource stored as a list, or in
the default context ``""`` because its ``rbac_context`` is not a string, is
read one context at a time. scoped-rbac's request check already reads a list
of strings so (0.11.2); scrud's post-save, workflow and object checks and its
lists now read every context so too.
"""

from uuid import uuid4

import pytest
from django.urls import reverse
from django.utils.http import quote_etag
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
from .steps import *  # noqa: F403, F401

JSON_LD = "http://www.w3.org/ns/json-ld#context"
ALL_METHODS = ("http.GET", "http.POST", "http.PUT", "http.DELETE")


def user_with(*grants):
    """A user holding each ``(role definition, context)`` of `grants`."""
    user = UserFactory()
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


def on_json_ld(condition, methods=ALL_METHODS):
    """A role definition granting `methods` on JSON-LD contexts under the CEL
    `condition`."""
    return {m: {JSON_LD: {"condition": condition}} for m in methods}


def everywhere_but(context):
    """A grant in every context whose condition refuses `context`, with
    ``http.GET`` on the JSON-LD list outright (lists need it)."""
    condition = f'rbac_context != "{context}"'
    return (
        {
            **on_json_ld(condition),
            "http.GET": {
                JSON_LD: {"condition": condition},
                collection_type_uri_for(JSON_LD): True,
            },
        },
        "*",
    )


# An outright GET of JSON-LD contexts in a context none of the tests' resources
# is in.
READS_ELSEWHERE = ({"http.GET": [JSON_LD]}, "elsewhere")


def store(content):
    reverse("json-ld-list")
    return ResourceRegistration.register(content, "json-ld")


# Bodies whose rbac_context is not a string (or a list holding one that is
# not): scrud stores them in the default context "".
INTO_THE_DEFAULT_CONTEXT = [
    pytest.param(None, id="null"),
    pytest.param(5, id="number"),
    pytest.param({"@id": "x"}, id="object"),
    pytest.param(["public", 1], id="list-holding-a-number"),
    pytest.param(["public", None], id="list-holding-null"),
]


@pytest.mark.django_db
@pytest.mark.parametrize("rbac_context", INTO_THE_DEFAULT_CONTEXT)
@test_context
def test_a_create_into_the_default_context_reads_it(rbac_context):
    reverse("json-ld-list")
    Given.http.force_authenticate(user_with(everywhere_but("")))
    before = Resource.objects.count()
    When.http.post(
        reverse("json-ld-list"),
        {"foo": "in default", "rbac_context": rbac_context},
        format="json",
    )
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert Resource.objects.count() == before

    When.http.post(
        reverse("json-ld-list"),
        {"foo": "elsewhere", "rbac_context": "public"},
        format="json",
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)


@pytest.mark.django_db
@pytest.mark.parametrize("rbac_context", INTO_THE_DEFAULT_CONTEXT)
@test_context
def test_an_update_into_the_default_context_reads_it(rbac_context):
    own = store({"foo": "mine", "rbac_context": "public"})
    Given.http.force_authenticate(user_with(everywhere_but("")))
    When.http.put(
        reverse("json-ld-detail", args=[own.pk]),
        {"foo": "moved", "rbac_context": rbac_context},
        format="json",
        HTTP_IF_MATCH=quote_etag(own.etag),
    )
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    own.refresh_from_db()
    assert own.content == {"foo": "mine", "rbac_context": "public"}


@pytest.mark.django_db
@test_context
def test_a_resource_stored_as_a_list_is_not_overwritten_from_outside():
    victims = store({"foo": "theirs", "rbac_context": ["victim"]})
    Given.http.force_authenticate(user_with(everywhere_but("victim")))
    When.http.put(
        reverse("json-ld-detail", args=[victims.pk]),
        {"foo": "taken", "rbac_context": "public"},
        format="json",
        HTTP_IF_MATCH=quote_etag(victims.etag),
    )
    Then.http.status_code_is(status.HTTP_404_NOT_FOUND)
    victims.refresh_from_db()
    assert victims.content == {"foo": "theirs", "rbac_context": ["victim"]}
    assert victims.rbac_contexts == ["victim"]


@pytest.mark.django_db
@test_context
def test_a_resource_stored_as_a_list_is_read_per_context():
    shared = store({"foo": "shared", "rbac_context": ["victim", "public"]})
    victims = store({"foo": "theirs", "rbac_context": ["victim"]})
    as_string = store({"foo": "plain", "rbac_context": "victim"})
    public = store({"foo": "public", "rbac_context": ["public"]})
    # scoped-rbac refuses a detail GET whose only grants are conditional
    # before any object check (defers_to_object_checks is for lists only):
    # an outright GET elsewhere gets the detail route to its object check.
    reader = user_with(everywhere_but("victim"), READS_ELSEWHERE)
    Given.http.force_authenticate(reader)
    When.http.get(reverse("json-ld-list"))
    Then.http.status_code_is(status.HTTP_200_OK)
    # The built-in JSON-LD contexts (in "") are listed too; they have no "foo".
    listed = {item["content"].get("foo") for item in Http.response.json()["content"]}
    assert listed >= {"shared", "public"}
    assert not listed & {"theirs", "plain"}

    for readable in (shared, public):
        When.http.get(reverse("json-ld-detail", args=[readable.pk]))
        Then.http.status_code_is(status.HTTP_200_OK)
    for refused in (victims, as_string):
        When.http.get(reverse("json-ld-detail", args=[refused.pk]))
        Then.http.status_code_is(status.HTTP_404_NOT_FOUND)


def in_its_own_context():
    """Grants in "a" and in "b", each conditioned on the object being in the
    context the grant is in (and `READS_ELSEWHERE`)."""
    return user_with(
        (on_json_ld('rbac_context == "a"'), "a"),
        (on_json_ld('rbac_context == "b"'), "b"),
        READS_ELSEWHERE,
    )


@pytest.mark.django_db
@test_context
def test_a_list_context_is_written_where_each_context_allows_it():
    reverse("json-ld-list")
    Given.http.force_authenticate(in_its_own_context())
    When.http.post(
        reverse("json-ld-list"),
        {"foo": "both", "rbac_context": ["a", "b"]},
        format="json",
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)
    created = Resource.objects.get(content__foo="both")
    assert created.rbac_contexts == ["a", "b"]

    When.http.get(reverse("json-ld-detail", args=[created.pk]))
    Then.http.status_code_is(status.HTTP_200_OK)

    When.http.post(
        reverse("json-ld-list"),
        {"foo": "with c", "rbac_context": ["a", "c"]},
        format="json",
    )
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)


@pytest.mark.django_db
@test_context
def test_the_workflow_check_reads_each_context():
    # A stored WorkflowPolicy in "a" and in "b" for JSON-LD contexts: the
    # workflow check evaluates the writer's grants in each of them.
    workflow_policy_resource_type()
    for context in ("a", "b"):
        ResourceRegistration.register(
            {
                "resource_type": JSON_LD,
                "initial_states": {"any": "true"},
                "states": {},
                "transitions": {},
                "rbac_context": context,
            },
            "workflow-policies",
        )
    reverse("json-ld-list")
    Given.http.force_authenticate(in_its_own_context())
    When.http.post(
        reverse("json-ld-list"),
        {"foo": "both", "rbac_context": ["a", "b"]},
        format="json",
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)
    assert Resource.objects.get(content__foo="both").rbac_contexts == ["a", "b"]


def test_in_context_sets_the_context_a_condition_is_evaluated_in():
    from scrud_django.permissions import in_context

    as_string = {"foo": 1, "rbac_context": "a"}
    assert in_context(as_string, "a") is as_string
    assert in_context(as_string, "b") is as_string  # not one of its contexts

    as_list = {"foo": 1, "rbac_context": ["a", "b"]}
    assert in_context(as_list, "b") == {"foo": 1, "rbac_context": "b"}
    assert as_list == {"foo": 1, "rbac_context": ["a", "b"]}  # not changed
    assert in_context(as_list, "c") is as_list

    # A value that is not a string, or none, stores it in the default context.
    for stored in ({"rbac_context": None}, {"rbac_context": ["a", 1]}, {}):
        assert in_context(stored, "")["rbac_context"] == ""
        assert in_context(stored, "a") is stored

    # Anything else (a model instance a GenericViewMixin view checks) as is.
    instance = object()
    assert in_context(instance, "a") is instance
    assert type(in_context(as_list, "a")) is dict


def test_reading_content_reads_each_context_without_changing_the_class():
    from scoped_rbac.permissions import IsAuthorized

    from scrud_django.filters import overrides_object_checks
    from scrud_django.permissions import reading_content

    class Recording(IsAuthorized):
        def has_context_permission(self, request, view, obj, context):
            seen.append((obj, context))
            return False

    seen = []
    (permission,) = reading_content([Recording()])
    wrapped = vars(permission)["has_context_permission"]
    reading_content([permission])  # idempotent: not wrapped twice
    assert vars(permission)["has_context_permission"] is wrapped
    assert type(permission) is Recording
    assert overrides_object_checks(permission)
    assert not overrides_object_checks(reading_content([IsAuthorized()])[0])

    permission.has_context_permission(None, None, {"rbac_context": ["a", "b"]}, "b")
    assert seen == [({"rbac_context": "b"}, "b")]
