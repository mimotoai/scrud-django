"""Adopters' own operators see the request body, as scoped-rbac's request check
gives it, also where the workflow check authorizes a transition: the
`TransitionCheck` it passes is a read-only mapping over the body."""

from uuid import uuid4

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.models import Resource
from scrud_django.permissions import TransitionCheck

from .factories import UserFactory

TODO = "tests://ToDo"
PP = "tests://PartnerProfiles"
PP_LIST = "https://api.openteams.com/json-schema/ResourceCollection?contents_type=" + PP
NOT_STARTED = {
    "title": "ok",
    "status": "not started",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
    "rbac_context": "q",
}


def field_is(resource, configuration):
    """An adopter's operator: allows a body whose `field` is `value`."""
    return resource.get(configuration["field"]) == configuration["value"]


# What each call of the `mutator` operator could not change.
REFUSED = []


def mutator(resource, configuration):
    """An adopter's operator that tries to change what it is given, at the
    top level and in a nested object, and allows."""
    for change in (
        lambda: resource.__setitem__("title", "MUTATED"),
        lambda: resource["nested"].__setitem__("evil", True),
        lambda: resource["tags"].append("evil"),
    ):
        try:
            change()
        except (TypeError, AttributeError, KeyError):
            REFUSED.append(type(resource).__name__)
    return True


@pytest.fixture(autouse=True)
def field_is_operator():
    REFUSED.clear()
    with override_settings(
        SCOPED_RBAC=dict(
            settings.SCOPED_RBAC,
            OPERATORS={"field_is": field_is, "mutator": mutator},
        )
    ):
        yield


def field_is_grant(type_uri, field):
    return {
        type_uri: {"condition": {"operator": "field_is", "field": field, "value": "ok"}}
    }


def user_with(definition, context="q"):
    role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
    role.save()
    user = UserFactory()
    RoleAssignment(
        user=user, role=role, rbac_context=context, etag=uuid4().hex, modified_at=now()
    ).save()
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_transition_check_is_a_read_only_mapping_over_the_body():
    check = TransitionCheck("start", {"name": "ok"})
    assert check.transition == "start"
    assert check.get("name") == "ok"
    assert dict(check) == {"name": "ok"}
    with pytest.raises(TypeError):
        check["name"] = "x"
    with pytest.raises(AttributeError):
        check.transition = "finish"
    assert TransitionCheck(None).get("name") is None


@pytest.mark.django_db
def test_body_operator_on_a_type_without_a_workflow():
    reverse("todos-list")
    client = user_with({"http.POST": field_is_grant(PP, "name")})
    before = Resource.objects.count()
    response = client.post(
        reverse("partner-profiles-list"),
        {"name": "no", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 403, response.content
    assert Resource.objects.count() == before
    response = client.post(
        reverse("partner-profiles-list"),
        {"name": "ok", "rbac_context": "q"},
        format="json",
    )
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_body_operator_on_a_workflow_type():
    reverse("todos-list")
    definition = {
        "http.GET": [TODO],
        "http.POST": field_is_grant(TODO, "title"),
        "http.PUT": {
            TODO: {
                "condition": {
                    "operator": "scrud_workflow",
                    "authorized_transitions": ["cancel"],
                }
            }
        },
    }
    client = user_with(definition)
    response = client.post(
        reverse("todos-list"), dict(NOT_STARTED, title="no"), format="json"
    )
    assert response.status_code == 403, response.content
    response = client.post(reverse("todos-list"), NOT_STARTED, format="json")
    assert response.status_code == 201, response.content

    # The transitions are still those of the scrud_workflow grant.
    todo = Resource.objects.latest("pk")
    detail = reverse("todos-detail", args=[todo.pk])
    response = client.put(
        detail,
        dict(NOT_STARTED, status="done"),
        format="json",
        HTTP_IF_MATCH=quote_etag(todo.etag),
    )
    assert response.status_code == 400, response.content
    canceled = dict(NOT_STARTED, status="canceled")
    response = client.put(
        detail, canceled, format="json", HTTP_IF_MATCH=quote_etag(todo.etag)
    )
    assert response.status_code == 200, response.content
    assert Resource.objects.get(pk=todo.pk).content == canceled


MUTATOR = {"condition": {"operator": "mutator"}}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "type_uri,route,body",
    [
        (PP, "partner-profiles-list", {"name": "x", "rbac_context": "q"}),
        (TODO, "todos-list", NOT_STARTED),
    ],
    ids=["request-check", "workflow-check"],
)
def test_an_operator_cannot_change_what_is_stored(type_uri, route, body):
    # The request check (scoped-rbac's) and the workflow check (a
    # TransitionCheck) give operators read-only views of the body at every
    # level: what is stored is the body as sent.
    reverse("todos-list")
    client = user_with({"http.GET": [type_uri], "http.POST": {type_uri: MUTATOR}})
    sent = dict(body, nested={"a": 1}, tags=["t"])
    response = client.post(reverse(route), sent, format="json")
    assert response.status_code == 201, response.content
    assert Resource.objects.latest("pk").content == sent
    assert REFUSED, "the operator was not asked"
    assert len(REFUSED) % 3 == 0, REFUSED  # every change refused


def test_transition_check_is_read_only_at_every_level():
    body = {"nested": {"a": 1}, "tags": ["t"]}
    check = TransitionCheck("start", body)
    with pytest.raises(TypeError):
        check["nested"]["a"] = 2
    with pytest.raises(AttributeError):
        check["tags"].append("x")
    assert check["nested"] == {"a": 1} and check["tags"] == ["t"]
    assert check.to_cel() == body
    assert isinstance(check.to_cel()["tags"], list)
    check.to_cel()["nested"]["a"] = 2  # a copy
    assert body == {"nested": {"a": 1}, "tags": ["t"]}
    assert check["nested"] == {"a": 1}


def test_read_only_arrays_are_tuples_for_scoped_rbacs_building_blocks():
    # access.body_contexts (scoped-rbac's default contexts_from) reads an
    # array of contexts only as a list or tuple.
    from scoped_rbac import access

    from scrud_django.permissions import BodyCheck

    body = BodyCheck({"rbac_context": ["a", "b"]})
    assert isinstance(body["rbac_context"], tuple)
    assert access.body_contexts(body) == ["a", "b"]
    assert body["rbac_context"] == ["a", "b"]
    assert body["rbac_context"][:1] == ["a"]


LD = "http://www.w3.org/ns/json-ld#context"


@pytest.mark.django_db
def test_a_contexts_from_delegating_to_scoped_rbac_reads_several_contexts():
    reverse("todos-list")
    role = Role(definition={"http.POST": [LD]}, etag=uuid4().hex, modified_at=now())
    role.save()
    user = UserFactory()
    for context in ("a", "b"):
        RoleAssignment(
            user=user,
            role=role,
            rbac_context=context,
            etag=uuid4().hex,
            modified_at=now(),
        ).save()
    client = APIClient()
    client.force_authenticate(user)
    response = client.post(
        reverse("body-contexts-json-ld-list"),
        {"@context": {}, "rbac_context": ["a", "b"]},
        format="json",
    )
    assert response.status_code == 201, response.content
    response = client.post(
        reverse("body-contexts-json-ld-list"),
        {"@context": {}, "rbac_context": ["a", "c"]},
        format="json",
    )
    assert response.status_code == 403, response.content


def nested(depth):
    """A JSON text of `depth` nested arrays."""
    return "[" * depth + "]" * depth


@pytest.mark.django_db
def test_a_body_nested_past_the_limit_is_400_and_one_within_it_is_stored():
    # A body nested about 1000 levels (one the JSON parser reads) is a 400
    # body-too-deep, before any check: Python code that walks it recursively
    # (reversion's serializer, an operator) would fail with RecursionError, a
    # 500. One within MAX_BODY_DEPTH is checked through the read-only views,
    # stored and read back.
    from scrud_django.mixins.view_mixins import MAX_BODY_DEPTH

    reverse("todos-list")
    client = user_with({"http.GET": [PP], "http.POST": {PP: MUTATOR}})
    before = Resource.objects.count()
    for depth in (990, MAX_BODY_DEPTH):
        body = '{"name": "deep", "rbac_context": "q", "x": ' + nested(depth) + "}"
        response = client.generic(
            "POST",
            reverse("partner-profiles-list"),
            body,
            content_type="application/json",
        )
        assert response.status_code == 400, response.content[:200]
        assert response.json()["type"] == (
            "https://api.openteams.com/http-problem/body-too-deep"
        )
    assert Resource.objects.count() == before
    # The object holding the arrays is one level: MAX_BODY_DEPTH in all.
    body = (
        '{"name": "deep", "rbac_context": "q", "x": ' + nested(MAX_BODY_DEPTH - 1) + "}"
    )
    response = client.generic(
        "POST", reverse("partner-profiles-list"), body, content_type="application/json"
    )
    assert response.status_code == 201, response.content[:200]
    stored = Resource.objects.latest("pk")
    reader = user_with({"http.GET": [PP, PP_LIST]})
    assert (
        reader.get(reverse("partner-profiles-detail", args=[stored.pk])).status_code
        == 200
    )
    assert reader.get(reverse("partner-profiles-list")).status_code == 200


def test_read_only_views_of_deep_content_are_built_without_recursion():
    from scrud_django.permissions import (
        ReadOnlyList,
        ReadOnlyMapping,
        read_only,
        writable,
    )

    value = "leaf"
    for depth in range(5000):
        value = {"k": value} if depth % 2 else [value]
    view, plain = read_only(value), writable(read_only(value))
    with pytest.raises(TypeError):
        view["k"] = 1
    levels = 0
    while view != "leaf":
        if isinstance(view, ReadOnlyList):
            assert isinstance(plain, list)
            view, plain = view[0], plain[0]
        else:
            assert isinstance(view, ReadOnlyMapping) and isinstance(plain, dict)
            view, plain = view["k"], plain["k"]
        levels += 1
    assert (levels, plain) == (5000, "leaf")
