"""A write must land only in contexts the request was authorized to write.

scoped-rbac authorizes a POST or PUT by the ``rbac_context`` its body names. A
form body naming ``rbac_context`` twice was checked by its last value but stored
as a list, which ``Resource.rbac_contexts`` reads as any-of, so a writer in one
context could plant (or move) a resource into a context they cannot write. The
scrud viewsets now read JSON bodies only, refuse a body that is not a JSON
object, and check, after the save and before it commits, every context the
stored resource ends up in.
"""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from django.urls import clear_url_caches, reverse
from django.utils.http import quote_etag
from django.utils.timezone import now
from rest_framework import status
from safetydance_django.test import *  # noqa: F403, F401
from scoped_rbac.models import Role, RoleAssignment
from scoped_rbac.rbac_contexts import DEFAULT_CONTEXT

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration, ResourceTypeRegistration
from scrud_django.scrud_signals import scrud_post_save

from .factories import UserFactory
from .fixtures import *  # noqa: F401, F403
from .steps import *  # noqa: F403, F401

TODO = "tests://ToDo"
JSON_LD = "http://www.w3.org/ns/json-ld#context"
JSON_SCHEMA = "http://json-schema.org/draft-04/schema"
ALL_METHODS = ("http.GET", "http.POST", "http.PUT", "http.DELETE")

# The types whose schema does not pin ``rbac_context`` to a string, where the
# smuggled list was stored as it was sent.
UNPINNED_TYPES = [("json-ld", JSON_LD), ("json-schema", JSON_SCHEMA)]


def user_with(definition, context):
    role = Role(definition=definition, etag=uuid4().hex, modified_at=now())
    role.save()
    user = UserFactory()
    RoleAssignment(
        user=user, role=role, rbac_context=context, etag=uuid4().hex, modified_at=now()
    ).save()
    return user


def attacker_and_victim(type_uri):
    """A writer in context "attacker" and a user who may read and write
    ``type_uri`` in context "victim", where the attacker may not write."""
    attacker = user_with({m: [type_uri] for m in ALL_METHODS}, "attacker")
    victim = user_with({m: [type_uri] for m in ALL_METHODS}, "victim")
    return attacker, victim


SMUGGLING_FORM = "foo=pwn&rbac_context=victim&rbac_context=attacker"


@pytest.mark.django_db
@pytest.mark.parametrize("slug,type_uri", UNPINNED_TYPES)
@test_context
def test_form_post_naming_two_contexts_is_refused(slug, type_uri):
    reverse("todos-list")  # registers the built-in types' routes
    attacker, _ = attacker_and_victim(type_uri)
    before = Resource.objects.count()
    Given.http.force_authenticate(attacker)
    When.http.post(
        f"/{slug}/",
        SMUGGLING_FORM,
        content_type="application/x-www-form-urlencoded",
    )
    Then.http.status_code_is(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
    assert Resource.objects.count() == before


@pytest.mark.django_db
@pytest.mark.parametrize("slug,type_uri", UNPINNED_TYPES)
@test_context
def test_multipart_post_naming_two_contexts_is_refused(slug, type_uri):
    reverse("todos-list")
    attacker, _ = attacker_and_victim(type_uri)
    before = Resource.objects.count()
    Given.http.force_authenticate(attacker)
    When.http.post(
        f"/{slug}/",
        {"foo": "pwn", "rbac_context": ["victim", "attacker"]},
        format="multipart",
    )
    Then.http.status_code_is(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
    assert Resource.objects.count() == before


@pytest.mark.django_db
@pytest.mark.parametrize("slug,type_uri", UNPINNED_TYPES)
@pytest.mark.parametrize(
    "body,content_type",
    [
        (SMUGGLING_FORM, "application/x-www-form-urlencoded"),
        (None, "multipart"),
    ],
    ids=["form", "multipart"],
)
@test_context
def test_form_put_naming_two_contexts_is_refused(slug, type_uri, body, content_type):
    reverse("todos-list")
    attacker, _ = attacker_and_victim(type_uri)
    own = ResourceRegistration.register(
        {"foo": "mine", "rbac_context": "attacker"}, slug
    )
    stored = dict(own.content)
    Given.http.force_authenticate(attacker)
    if content_type == "multipart":
        When.http.put(
            f"/{slug}/{own.id}/",
            {"foo": "moved", "rbac_context": ["victim", "attacker"]},
            format="multipart",
            HTTP_IF_MATCH=quote_etag(own.etag),
        )
    else:
        When.http.put(
            f"/{slug}/{own.id}/",
            body,
            content_type=content_type,
            HTTP_IF_MATCH=quote_etag(own.etag),
        )
    Then.http.status_code_is(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE)
    own.refresh_from_db()
    assert own.content == stored
    assert own.rbac_contexts == ["attacker"]


@pytest.mark.django_db
@test_context
def test_json_list_body_is_400(regular_login):
    reverse("todos-list")
    before = Resource.objects.count()
    Given.http.force_authenticate(regular_login)
    When.http.post("/json-ld/", [{"foo": "x"}], format="json")
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    When.http.post(reverse("todos-list"), [{"title": "x"}], format="json")
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    assert Resource.objects.count() == before


@pytest.mark.django_db
@test_context
def test_json_list_body_put_is_400(regular_login):
    reverse("todos-list")
    own = ResourceRegistration.register({"foo": "mine"}, "json-ld")
    stored = dict(own.content)
    Given.http.force_authenticate(regular_login)
    When.http.put(
        f"/json-ld/{own.id}/",
        [{"foo": "x"}],
        format="json",
        HTTP_IF_MATCH=quote_etag(own.etag),
    )
    Then.http.status_code_is(status.HTTP_400_BAD_REQUEST)
    own.refresh_from_db()
    assert own.content == stored


@pytest.mark.django_db
@pytest.mark.parametrize("slug,type_uri", UNPINNED_TYPES)
@test_context
def test_json_post_in_a_permitted_context_is_created(slug, type_uri):
    # Positive control: a JSON body naming a context the writer holds is stored.
    reverse("todos-list")
    attacker, _ = attacker_and_victim(type_uri)
    Given.http.force_authenticate(attacker)
    When.http.post(f"/{slug}/", {"foo": "x", "rbac_context": "attacker"}, format="json")
    Then.http.status_code_is(status.HTTP_201_CREATED)
    created = Resource.objects.order_by("-id").first()
    assert created.content == {"foo": "x", "rbac_context": "attacker"}
    assert created.rbac_contexts == ["attacker"]


@pytest.mark.django_db
@test_context
def test_json_todo_post_in_a_permitted_context_is_created():
    reverse("todos-list")
    writer = user_with({m: [TODO] for m in ALL_METHODS}, "attacker")
    Given.http.force_authenticate(writer)
    When.http.post(
        reverse("todos-list"),
        {"title": "x", "rbac_context": "attacker"},
        format="json",
    )
    Then.http.status_code_is(status.HTTP_201_CREATED)


# A throwaway type whose schema gives ``rbac_context`` a default. fastjsonschema
# fills the default in, but DRF runs a serializer's validators on a copy of the
# body (``Serializer.run_validators``), and scrud stores the body itself: the
# default never reaches the stored resource, which stays in the context the
# request was authorized in. Should it ever be stored, the post-save check
# answers 403 instead (as for a write moved after authorization, below).
DEFAULTED_TYPE = "tests://ContextDefaulted"


@pytest.fixture
def context_defaulted_type(db):
    # Imported here: importing the URLconf registers types, which needs the DB.
    from . import urls as test_urls

    registration = ResourceTypeRegistration(
        DEFAULTED_TYPE,
        revision="1",
        slug="context-defaulted",
        schema_func=lambda: {
            "$schema": JSON_SCHEMA,
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "rbac_context": {"type": "string", "default": "other"},
            },
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


@pytest.mark.django_db
@test_context
def test_schema_default_context_is_not_stored(context_defaulted_type):
    writer = user_with({m: [DEFAULTED_TYPE] for m in ALL_METHODS}, DEFAULT_CONTEXT)
    Given.http.force_authenticate(writer)
    When.http.post("/context-defaulted/", {"name": "x"}, format="json")
    Then.http.status_code_is(status.HTTP_201_CREATED)
    created = Resource.objects.get(resource_type=context_defaulted_type)
    assert created.content == {"name": "x"}
    assert created.rbac_contexts == [DEFAULT_CONTEXT]

    When.http.put(
        f"/context-defaulted/{created.id}/",
        {"name": "y"},
        format="json",
        HTTP_IF_MATCH=quote_etag(created.etag),
    )
    Then.http.status_code_is(status.HTTP_200_OK)
    created.refresh_from_db()
    assert created.content == {"name": "y"}
    assert created.rbac_contexts == [DEFAULT_CONTEXT]


TODO_DATA = {
    "title": "Buy milk",
    "status": "not started",
    "priority": "medium",
    "assignee": "http://somewhere.example/someone",
}


@contextmanager
def todos_moved_after_save():
    """A ``scrud_post_save`` receiver (server-side code that runs inside the
    write, after the request was authorized) that moves every saved ToDo to the
    context "other"."""

    def move(sender, instance, resource_type_uri=None, **kwargs):
        if resource_type_uri != TODO:
            return
        instance.content = {**instance.content, "rbac_context": "other"}
        type(instance).objects.filter(pk=instance.pk).update(content=instance.content)

    scrud_post_save.connect(move, weak=False, dispatch_uid="test-move-after-save")
    try:
        yield
    finally:
        scrud_post_save.disconnect(dispatch_uid="test-move-after-save")


def default_context_todo_writer():
    return user_with({m: [TODO] for m in ALL_METHODS}, DEFAULT_CONTEXT)


@pytest.mark.django_db
@test_context
def test_create_moved_to_a_context_the_writer_cannot_write_is_403():
    reverse("todos-list")
    before = Resource.objects.count()
    Given.http.force_authenticate(default_context_todo_writer())
    with todos_moved_after_save():
        When.http.post(reverse("todos-list"), TODO_DATA, format="json")
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    assert Resource.objects.count() == before


@pytest.mark.django_db
@test_context
def test_update_moved_to_a_context_the_writer_cannot_write_is_403():
    reverse("todos-list")
    own = ResourceRegistration.register(dict(TODO_DATA), "todos")
    stored = dict(own.content)
    Given.http.force_authenticate(default_context_todo_writer())
    with todos_moved_after_save():
        When.http.put(
            reverse("todos-detail", args=[own.id]),
            TODO_DATA,
            format="json",
            HTTP_IF_MATCH=quote_etag(own.etag),
        )
    Then.http.status_code_is(status.HTTP_403_FORBIDDEN)
    own.refresh_from_db()
    assert own.content == stored
    assert own.rbac_contexts == [DEFAULT_CONTEXT]
