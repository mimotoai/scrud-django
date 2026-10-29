"""JSON Schema ``default`` values are never applied: neither stored nor seen by
the workflow check.

fastjsonschema fills a schema's defaults into the data it validates.
``JsonValidator`` validates a deep copy of the body, so no default reaches the
stored resource (top-level or nested), and the ``WorkflowValidator`` reads the
body as sent, which is what is stored: the contexts and the state it checks
are the stored resource's.
"""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from django.urls import clear_url_caches
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework import status
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    ResourceTypeRegistration,
    workflow_policy_resource_type,
)

from .factories import UserFactory
from .steps import *  # noqa: F403, F401

JSON_SCHEMA = "http://json-schema.org/draft-04/schema"
WORKFLOW_ERROR = "http://api.openteams.com/http-problem/workflow-transition-invalid"
WORKFLOW_POLICY = "http://api.openteams.com/json-ld/WorkflowPolicy"
ALL_METHODS = ("http.GET", "http.POST", "http.PUT", "http.DELETE")

CONTEXT_DEFAULTED = "tests://SchemaDefaults/Context"
STATE_DEFAULTED = "tests://SchemaDefaults/State"
NESTED_DEFAULTED = "tests://SchemaDefaults/Nested"


@contextmanager
def throwaway_type(type_uri, slug, properties):
    """A resource type served at ``/<slug>/`` whose schema has `properties`."""
    from . import urls as test_urls

    registration = ResourceTypeRegistration(
        type_uri,
        revision="1",
        slug=slug,
        schema_func=lambda: {
            "$schema": JSON_SCHEMA,
            "type": "object",
            "properties": properties,
        },
        uses_reversion=False,
        register_service=False,
    )
    test_urls.urlpatterns.extend(registration.urls)
    clear_url_caches()
    try:
        yield registration.resource_type
    finally:
        for pattern in registration.urls:
            test_urls.urlpatterns.remove(pattern)
        clear_url_caches()


@pytest.fixture
def context_defaulted_type(db):
    with throwaway_type(
        CONTEXT_DEFAULTED,
        "context-defaulted-g2",
        {
            "name": {"type": "string"},
            "rbac_context": {"type": "string", "default": "other"},
        },
    ) as resource_type:
        yield resource_type


@pytest.fixture
def state_defaulted_type(db):
    with throwaway_type(
        STATE_DEFAULTED,
        "state-defaulted",
        {
            "name": {"type": "string"},
            "state": {"type": "string", "default": "open"},
        },
    ) as resource_type:
        yield resource_type


@pytest.fixture
def nested_defaulted_type(db):
    with throwaway_type(
        NESTED_DEFAULTED,
        "nested-defaulted",
        {
            "name": {"type": "string"},
            "meta": {
                "type": "object",
                "properties": {"flag": {"type": "string", "default": "x"}},
            },
        },
    ) as resource_type:
        yield resource_type


def writer_of(type_uri, context=DEFAULT_CONTEXT):
    """May do anything with `type_uri`, and read workflow policies (so that
    workflow errors say what failed), in `context`."""
    definition = {m: [type_uri] for m in ALL_METHODS}
    definition["http.GET"].append(WORKFLOW_POLICY)
    role = Role(
        definition=definition,
        etag=uuid4().hex,
        modified_at=now(),
    )
    role.save()
    user = UserFactory()
    RoleAssignment(
        user=user, role=role, rbac_context=context, etag=uuid4().hex, modified_at=now()
    ).save()
    return user


def store_policy(content):
    workflow_policy_resource_type()
    return ResourceRegistration.register(content, "workflow-policies")


def stored(resource_type):
    return Resource.objects.filter(resource_type=resource_type).order_by("pk")


@pytest.mark.django_db
@test_context
def test_a_defaulted_context_does_not_decide_the_workflow_check(
    context_defaulted_type,
):
    # "other" refuses every create; "" only a body named "ok". The schema
    # defaults rbac_context to "other", but a body without it is stored in ""
    # and checked there.
    store_policy(
        {
            "resource_type": CONTEXT_DEFAULTED,
            "rbac_context": "other",
            "initial_states": {"never": "false"},
            "states": {},
            "transitions": {},
        }
    )
    store_policy(
        {
            "resource_type": CONTEXT_DEFAULTED,
            "initial_states": {"named": 'name == "ok"'},
            "states": {},
            "transitions": {},
        }
    )
    Given.http.force_authenticate(writer_of(CONTEXT_DEFAULTED))
    When.http.post("/context-defaulted-g2/", {"name": "x"}, format="json")
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert Http.response.json()["type"] == WORKFLOW_ERROR
    assert not stored(context_defaulted_type).exists()

    When.http.post("/context-defaulted-g2/", {"name": "ok"}, format="json")
    Then.http.status_code_is(status.HTTP_201_CREATED)
    created = stored(context_defaulted_type).get()
    assert created.content == {"name": "ok"}
    assert created.rbac_contexts == [DEFAULT_CONTEXT]


@pytest.mark.django_db
@test_context
def test_a_defaulted_state_is_not_the_state_checked(state_defaulted_type):
    store_policy(
        {
            "resource_type": STATE_DEFAULTED,
            "initial_states": {"open": 'state == "open"'},
            "states": {"closed": 'state == "closed"'},
            "transitions": {"close": {"from": "open", "to": "closed"}},
        }
    )
    Given.http.force_authenticate(writer_of(STATE_DEFAULTED))
    # Stored without "state", the body is in no state: not a valid create,
    # whatever the schema's default would make it.
    When.http.post("/state-defaulted/", {"name": "x"}, format="json")
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    body = Http.response.json()
    assert body["type"] == WORKFLOW_ERROR, body
    assert body["message"] == "The request is not a valid initial state.", body
    assert not stored(state_defaulted_type).exists()

    When.http.post("/state-defaulted/", {"name": "x", "state": "open"}, format="json")
    Then.http.status_code_is(status.HTTP_201_CREATED)
    created = stored(state_defaulted_type).get()
    assert created.content == {"name": "x", "state": "open"}

    # An update that leaves "state" out leaves every state: refused, though
    # the default would have kept it "open".
    When.http.put(
        f"/state-defaulted/{created.pk}/",
        {"name": "y"},
        format="json",
        HTTP_IF_MATCH=quote_etag(created.etag),
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    body = Http.response.json()
    assert body["type"] == WORKFLOW_ERROR, body
    assert body["message"] == "The request does not match a valid state", body
    assert Resource.objects.get(pk=created.pk).content == created.content


@pytest.mark.django_db
@test_context
def test_nested_defaults_are_not_stored(nested_defaulted_type):
    Given.http.force_authenticate(writer_of(NESTED_DEFAULTED))
    When.http.post("/nested-defaulted/", {"name": "x", "meta": {}}, format="json")
    Then.http.status_code_is(status.HTTP_201_CREATED)
    assert Http.response.json() == {"name": "x", "meta": {}}
    created = stored(nested_defaulted_type).get()
    assert created.content == {"name": "x", "meta": {}}

    When.http.put(
        f"/nested-defaulted/{created.pk}/",
        {"name": "y", "meta": {}},
        format="json",
        HTTP_IF_MATCH=quote_etag(created.etag),
    )
    Then.http.status_code_is(status.HTTP_200_OK)
    assert Http.response.json() == {"name": "y", "meta": {}}
    assert Resource.objects.get(pk=created.pk).content == {"name": "y", "meta": {}}


@pytest.mark.django_db
def test_the_json_validator_leaves_its_data_unchanged(nested_defaulted_type):
    from types import SimpleNamespace

    from scrud_django.validators import JsonValidator

    data = {"name": "x", "meta": {}}
    JsonValidator()(data, SimpleNamespace(resource_type=nested_defaulted_type))
    assert data == {"name": "x", "meta": {}}
