"""Writes without DRF (`scrud_django.core.writes`): a create, an update and
a delete of a resource, saved and checked where they landed before they
commit, asked with a plain Django request and a neutral requirement, as the
DRF routes write them. The DRF routes check a write's workflow once."""

import json
from unittest import mock
from uuid import uuid4

import pytest
from django.test import RequestFactory
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework import status
from reversion.models import Version
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.core import workflow as core_workflow
from scrud_django.core.requirements import GrantsLeaf
from scrud_django.core.workflow import NoWorkflowPolicyDenied
from scrud_django.core.writes import create, destroy, update, validate_write
from scrud_django.exceptions import ResourceInUseError, WorkflowPolicyInvalidError
from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    workflow_policy_resource_type,
)

from .factories import UserFactory
from .fixtures import admin_login  # noqa: F401
from .rbac_defaults import TYPE_URI_PARTNER_PROFILES
from .steps import *  # noqa: F403, F401

PP = TYPE_URI_PARTNER_PROFILES
NOT_STARTED = {
    "title": "Buy milk",
    "status": "not started",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
}


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


def request_by(method, user):
    request = getattr(RequestFactory(), method)("/")
    request.user = user
    return request


def profiles_type():
    reverse("partner-profiles-list")
    return get_resource_type_for("partner-profiles")


def versions():
    return Version.objects.get_for_model(Resource).count()


@pytest.mark.django_db
@test_context
def test_create_and_post_save_denial_rolls_back():
    rtype = profiles_type()
    assert rtype.uses_reversion
    user = user_with(({"http.POST": [PP]}, "a"))
    before = (Resource.objects.filter(resource_type=rtype).count(), versions())
    with pytest.raises(access.AccessDenied):
        create(
            request_by("post", user),
            rtype,
            {"text": "x", "rbac_context": ["a", "b"]},
            requirement=[GrantsLeaf(PP)],
            using="default",
        )
    assert (Resource.objects.filter(resource_type=rtype).count(), versions()) == before
    # The control: the same create in "a" alone is this user's to make.
    created = create(
        request_by("post", user),
        rtype,
        {"text": "x", "rbac_context": ["a"]},
        requirement=[GrantsLeaf(PP)],
        using="default",
    )
    assert Resource.objects.get(pk=created.pk).content == {
        "text": "x",
        "rbac_context": ["a"],
    }
    assert versions() > before[1]


@pytest.mark.django_db
@test_context
def test_update_returns_saved():
    rtype = profiles_type()
    stored = ResourceRegistration.register(
        {"name": "old", "rbac_context": "a"}, "partner-profiles"
    )
    instance = Resource.objects.get(pk=stored.pk)
    body = {"name": "new", "rbac_context": "a"}
    request = request_by("put", UserFactory(is_staff=True, is_superuser=True))
    saved = update(
        request,
        rtype,
        instance,
        body,
        requirement=[GrantsLeaf(PP)],
        using="default",
    )
    assert saved.pk == stored.pk
    assert saved.content == body
    assert saved.etag != stored.etag
    assert saved.modified_at > stored.modified_at
    row = Resource.objects.get(pk=stored.pk)
    assert (row.content, row.etag, row.modified_at) == (
        body,
        saved.etag,
        saved.modified_at,
    )


@pytest.mark.django_db
@test_context
def test_destroy_in_use_is_409():
    schema = Resource.objects.get(pk=profiles_type().schema.pk)
    request = request_by("delete", UserFactory(is_staff=True, is_superuser=True))
    with pytest.raises(ResourceInUseError) as raised:
        destroy(
            request,
            schema.resource_type,
            schema,
            requirement=[GrantsLeaf(schema.resource_type.type_uri)],
            using="default",
        )
    assert raised.value.status_code == status.HTTP_409_CONFLICT
    assert Resource.objects.filter(pk=schema.pk).exists()


@pytest.mark.django_db
@test_context
def test_workflow_evaluated_once(admin_login):  # noqa: F811
    # A PUT from "not started" to "in progress" in the default context, under
    # the settings' ToDo policy: allowed_on runs once per context, so a second
    # evaluation of the workflow check (the serializer's validator and the
    # core both) would show as a second call.
    reverse("todos-list")
    todo = ResourceRegistration.register(dict(NOT_STARTED), "todos")
    with mock.patch(
        "scrud_django.core.workflow.allowed_on", wraps=core_workflow.allowed_on
    ) as allowed_on:
        Given.http.force_authenticate(admin_login)
        When.http.put(
            reverse("todos-detail", args=[todo.pk]),
            data=json.dumps(dict(NOT_STARTED, status="in progress")),
            content_type="application/json",
            HTTP_IF_MATCH=quote_etag(todo.etag),
        )
        Then.http.status_code_is(status.HTTP_200_OK)
    assert allowed_on.call_count == 1, allowed_on.call_args_list
    assert allowed_on.call_args.args[2] == ""


# validate_write builds the workflow check's prior as the DRF adapter does: a
# stored resource whose content is JSON null is an update over content in no
# state and in the default context, never a create.

JSON_LD = "http://www.w3.org/ns/json-ld#context"


def may_initialize_json_ld():
    """A role writing a JSON-LD resource only by ``__INITIALIZE__``."""
    return {
        "http.GET": [JSON_LD],
        "http.PUT": {
            JSON_LD: {
                "condition": {
                    "operator": "scrud_workflow",
                    "authorized_transitions": ["__INITIALIZE__"],
                }
            }
        },
    }


@pytest.mark.django_db
@pytest.mark.parametrize("stored_content", [{}, None], ids=["empty", "null"])
@test_context
def test_validate_write_checks_null_stored_content_as_an_update(stored_content):
    from django.db.models import JSONField, Value

    reverse("json-ld-list")
    workflow_policy_resource_type()
    ResourceRegistration.register(
        {
            "resource_type": JSON_LD,
            "rbac_context": "a",
            "initial_states": {"any": "true"},
            "states": {},
            "transitions": {},
        },
        "workflow-policies",
    )
    stored = ResourceRegistration.register({"x": 1}, "json-ld")
    Resource.objects.filter(pk=stored.pk).update(
        content=Value(stored_content, JSONField())
    )
    stored = Resource.objects.get(pk=stored.pk)
    assert stored.content == stored_content
    # Only scrud_workflow-conditioned grants in "" (where no policy applies)
    # and in "a": moving the resource out of "" is refused there.
    request = request_by(
        "put",
        user_with((may_initialize_json_ld(), ""), (may_initialize_json_ld(), "a")),
    )
    with pytest.raises(NoWorkflowPolicyDenied):
        validate_write(
            request,
            stored.resource_type,
            {"rbac_context": "a"},
            stored=stored,
            requirement=[GrantsLeaf(JSON_LD)],
            readable=None,
        )


@pytest.mark.django_db
@test_context
def test_validate_write_checks_a_workflow_policy():
    reverse("todos-list")
    policy_type = workflow_policy_resource_type()
    policy = {
        "resource_type": "tests://ToDo",
        "rbac_context": "p",
        "initial_states": {"any": "true"},
        "states": {},
        "transitions": {},
    }
    ResourceRegistration.register(dict(policy), "workflow-policies")
    request = request_by("post", UserFactory(is_staff=True, is_superuser=True))
    with pytest.raises(WorkflowPolicyInvalidError, match="already exists in context"):
        validate_write(
            request,
            policy_type,
            dict(policy),
            stored=None,
            requirement=[GrantsLeaf(policy_type.type_uri)],
            readable=None,
        )


# The DRF routes' post-save check is the view's: it names the type as the
# view does (resource_type_iri_for), and a view's own
# check_post_save_permissions and object_for_checks take effect.


def latest_profile():
    return (
        Resource.objects.filter(resource_type=profiles_type()).order_by("-pk").first()
    )


def post_profile(prefix, body):
    When.http.post(
        reverse(f"{prefix}-partner-profiles-list"),
        data=json.dumps(body),
        content_type="application/json",
    )


def put_profile(prefix, stored, body):
    When.http.put(
        reverse(f"{prefix}-partner-profiles-detail", args=[stored.pk]),
        data=json.dumps(body),
        content_type="application/json",
        HTTP_IF_MATCH=quote_etag(stored.etag),
    )


@pytest.mark.django_db
@test_context
def test_drf_post_save_check_names_the_type_as_the_view_does():
    from .urls import OtherIriResourceViewSet

    profiles_type()
    # Grants only on the type the view names, none on the stored type: its
    # IsAuthorized has its own has_context_permission, so the post-save check
    # asks the grants beside it, for the type the view names.
    other = OtherIriResourceViewSet.OTHER_IRI
    Given.http.force_authenticate(user_with(({"http.POST": [other]}, "a")))
    post_profile("other-iri", {"name": "x", "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_201_CREATED)


@pytest.mark.django_db
@test_context
def test_drf_post_save_check_gets_the_views_object_for_checks(admin_login):  # noqa: F811
    profiles_type()
    Given.http.force_authenticate(admin_login)
    post_profile("object-for-checks", {"name": "x", "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_201_CREATED)
    stored = latest_profile()
    put_profile("object-for-checks", stored, {"name": "y", "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Resource.objects.get(pk=stored.pk).content["name"] == "y"


@pytest.mark.django_db
@test_context
def test_drf_post_save_check_is_the_views_own(admin_login):  # noqa: F811
    from .urls import PostSaveRefusingResourceViewSet

    refused = PostSaveRefusingResourceViewSet.REFUSED
    rtype = profiles_type()
    before = Resource.objects.filter(resource_type=rtype).count()
    Given.http.force_authenticate(admin_login)
    post_profile("post-save-refusing", {"name": refused, "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert Resource.objects.filter(resource_type=rtype).count() == before
    post_profile("post-save-refusing", {"name": "x", "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_201_CREATED)
    stored = latest_profile()
    put_profile("post-save-refusing", stored, {"name": refused, "rbac_context": "a"})
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert Resource.objects.get(pk=stored.pk).content["name"] == "x"


def may_stay_json_ld():
    """A role reading JSON-LD resources, and updating one only by a
    transition that stays in its state."""
    return {
        "http.GET": [JSON_LD],
        "http.PUT": {
            JSON_LD: {
                "condition": {
                    "operator": "scrud_workflow",
                    "authorized_transitions": ["__SAME_STATE__"],
                }
            }
        },
    }


@pytest.mark.django_db
@test_context
def test_update_needs_the_workflow_check_for_a_scrud_workflow_grant():
    # The post-save check lets a grant conditioned on scrud_workflow allow
    # only a write the workflow check authorized on the same request.
    reverse("json-ld-list")
    workflow_policy_resource_type()
    ResourceRegistration.register(
        {
            "resource_type": JSON_LD,
            "rbac_context": "a",
            "initial_states": {"any": "true"},
            "states": {},
            "transitions": {},
        },
        "workflow-policies",
    )
    stored = ResourceRegistration.register({"x": 1, "rbac_context": "a"}, "json-ld")
    rtype = stored.resource_type
    user = user_with((may_stay_json_ld(), "a"))
    body = {"x": 2, "rbac_context": "a"}
    requirement = [GrantsLeaf(JSON_LD)]
    with pytest.raises(access.AccessDenied):
        update(
            request_by("put", user),
            rtype,
            Resource.objects.get(pk=stored.pk),
            body,
            requirement=requirement,
            using="default",
        )
    row = Resource.objects.get(pk=stored.pk)
    assert (row.content, row.etag) == (stored.content, stored.etag)
    request = request_by("put", user)
    validate_write(
        request,
        rtype,
        body,
        stored=row,
        requirement=requirement,
        readable=lambda: True,
    )
    saved = update(request, rtype, row, body, requirement=requirement, using="default")
    assert Resource.objects.get(pk=stored.pk).content == body == saved.content


@pytest.mark.django_db
@test_context
def test_update_cannot_move_a_resource_where_the_writer_may_not_write():
    rtype = profiles_type()
    stored = ResourceRegistration.register(
        {"name": "old", "rbac_context": "a"}, "partner-profiles"
    )
    writer = user_with(({"http.GET": [PP], "http.PUT": [PP]}, "a"))
    with pytest.raises(access.AccessDenied):
        update(
            request_by("put", writer),
            rtype,
            Resource.objects.get(pk=stored.pk),
            {"name": "moved", "rbac_context": "b"},
            requirement=[GrantsLeaf(PP)],
            using="default",
        )
    row = Resource.objects.get(pk=stored.pk)
    assert (row.content, row.etag) == (
        {"name": "old", "rbac_context": "a"},
        stored.etag,
    )
