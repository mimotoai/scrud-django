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
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac import access
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.core import workflow as core_workflow
from scrud_django.core.requirements import GrantsLeaf
from scrud_django.core.writes import create, destroy, update
from scrud_django.exceptions import ResourceInUseError
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration, get_resource_type_for

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


@pytest.mark.django_db
@test_context
def test_create_and_post_save_denial_rolls_back():
    rtype = profiles_type()
    before = Resource.objects.filter(resource_type=rtype).count()
    request = request_by("post", user_with(({"http.POST": [PP]}, "a")))
    with pytest.raises(access.AccessDenied):
        create(
            request,
            rtype,
            {"text": "x", "rbac_context": ["a", "b"]},
            requirement=[GrantsLeaf(PP)],
            using="default",
        )
    assert Resource.objects.filter(resource_type=rtype).count() == before


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
