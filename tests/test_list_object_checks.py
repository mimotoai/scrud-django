"""A list shows what the detail route shows also for a view whose
``IsAuthorized`` subclass overrides ``has_object_permission`` or
``has_context_permission`` (``tests.urls.ContextDenyingIsAuthorized`` refuses
the context "denied", ``LockedDenyingIsAuthorized`` a resource with
``"locked": true``): after the RBAC filter, each listed object is given to the
permission class as the detail route gives it (its content, a ``dict``, for
a resource, the object itself for a model of your own), with the item type's
``http.GET``. The list's validators follow what is listed. A view whose
permission class overrides neither keeps the SQL-only list."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils.http import http_date
from django.utils.timezone import now
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .models import Note
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES

PP = TYPE_URI_PARTNER_PROFILES
PP_LIST = TYPE_URI_LIST_PARTNER_PROFILES
LD = "http://www.w3.org/ns/json-ld#context"
LD_LIST = "https://api.openteams.com/json-schema/ResourceCollection?contents_type=" + LD
NOTE = "tests://Note"
NOTE_LIST = f"{NOTE}/list"


def client_with(*grants, superuser=False):
    """A client whose user holds each ``(definition, context)`` of `grants`."""
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
    client = APIClient()
    client.force_authenticate(user)
    return client


def profile(name, context):
    reverse("partner-profiles-list")
    return ResourceRegistration.register(
        {"name": name, "rbac_context": context}, "partner-profiles"
    )


def listed(response, key="name"):
    assert response.status_code == 200, response.content
    return sorted(entry["content"][key] for entry in response.json()["content"])


PP_READER = {"http.GET": [PP, PP_LIST]}


@pytest.mark.django_db
def test_a_list_leaves_out_what_has_context_permission_refuses():
    secret, shown = profile("secret", "denied"), profile("shown", "a")
    client = client_with((PP_READER, "denied"), (PP_READER, "a"))
    detail = reverse("denying-partner-profiles-detail", args=[secret.pk])
    # The detail route hides it too (404, not 403): the permission class
    # refuses a read of it, as the list does.
    assert client.get(detail).status_code == 404
    response = client.get(reverse("denying-partner-profiles-list"))
    assert listed(response) == ["shown"]
    assert response.json()["count"] == 1
    head = client.head(reverse("denying-partner-profiles-list"))
    assert head.status_code == 200
    assert head["ETag"] == response["ETag"]
    # The plain view's list (IsAuthorized) shows both, as its detail route.
    assert listed(client.get(reverse("partner-profiles-list"))) == ["secret", "shown"]
    assert shown.pk != secret.pk


@pytest.mark.django_db
def test_a_superusers_list_leaves_out_what_the_permission_class_refuses():
    profile("secret", "denied"), profile("shown", "a")
    client = client_with(superuser=True)
    response = client.get(reverse("denying-partner-profiles-list"))
    assert listed(response) == ["shown"]


@pytest.mark.django_db
def test_a_list_leaves_out_what_has_object_permission_refuses():
    reverse("partner-profiles-list")
    locked = ResourceRegistration.register(
        {"@context": {}, "locked": True, "rbac_context": "a", "name": "locked"},
        "json-ld",
    )
    ResourceRegistration.register(
        {"@context": {}, "locked": False, "rbac_context": "a", "name": "open"},
        "json-ld",
    )
    client = client_with(({"http.GET": [LD, LD_LIST]}, "a"))
    detail = reverse("locking-json-ld-detail", args=[locked.pk])
    # The detail route hides it too (404, not 403), as the list does.
    assert client.get(detail).status_code == 404
    response = client.get(reverse("locking-json-ld-list"))
    assert listed(response) == ["open"]


@pytest.mark.django_db
def test_the_list_validators_follow_what_the_permission_class_lets_through():
    secret, shown = profile("secret", "denied"), profile("shown", "a")
    Resource.objects.filter(pk=shown.pk).update(modified_at=now() - timedelta(days=2))
    client = client_with((PP_READER, "denied"), (PP_READER, "a"))
    url = reverse("denying-partner-profiles-list")
    response = client.get(url)
    assert listed(response) == ["shown"]
    etag, last_modified = response["ETag"], response["Last-Modified"]
    # The refused resource is newer, and does not decide Last-Modified.
    shown.refresh_from_db()
    assert last_modified == http_date(shown.modified_at.timestamp())
    # A write of the refused resource changes neither validator.
    ResourceRegistration.update(
        {"name": "secret, changed", "rbac_context": "denied"},
        "partner-profiles",
        secret.pk,
    )
    assert client.get(url, HTTP_IF_NONE_MATCH=etag).status_code == 304
    again = client.get(url)
    assert again["ETag"] == etag
    assert again["Last-Modified"] == last_modified
    # Nor does its deletion: its removal stamp is not read for this view.
    ResourceRegistration.delete("partner-profiles", secret.pk)
    again = client.get(url)
    assert again["ETag"] == etag
    assert again["Last-Modified"] == last_modified


@pytest.mark.django_db
def test_the_permission_class_gets_each_listed_resource_as_its_content():
    from .urls import RecordingIsAuthorized  # importing it registers the routes

    shown = profile("shown", "a")
    client = client_with((PP_READER, "a"))
    RecordingIsAuthorized.SEEN.clear()
    response = client.get(reverse("recording-partner-profiles-list"))
    assert listed(response) == ["shown"]
    seen = [obj for name, obj in RecordingIsAuthorized.SEEN]
    assert seen, RecordingIsAuthorized.SEEN
    assert all(type(obj) is dict for obj in seen), seen
    assert all(obj == shown.content for obj in seen), seen


@pytest.mark.django_db
def test_every_listed_object_counts_against_the_cap_where_the_class_decides():
    profile("one", "a"), profile("two", "a")
    client = client_with((PP_READER, "a"))
    capped = dict(settings.SCOPED_RBAC, CONDITIONAL_LIST_MAX_ROWS=1)
    with override_settings(SCOPED_RBAC=capped):
        response = client.get(reverse("denying-partner-profiles-list"))
        assert response.status_code == 400, response.content
        assert response.json()["code"] == "conditional_list_too_large"
        # The plain view lists outright readable resources in SQL, uncapped.
        response = client.get(reverse("partner-profiles-list"))
        assert listed(response) == ["one", "two"]


def a_note(text, rbac_context):
    reverse("generic-notes-list")  # tests.urls registers the routes
    return Note.objects.create(
        text=text, rbac_context=rbac_context, etag=uuid4().hex, modified_at=now()
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "route",
    ["generic-denying-notes-list", "generic-denying-notes-from-list-queryset"],
)
def test_a_generic_view_list_leaves_out_what_the_permission_class_refuses(route):
    secret = a_note("secret", "denied")
    a_note("shown", "a")
    reader = {"http.GET": [NOTE, NOTE_LIST]}
    client = client_with((reader, "denied"), (reader, "a"))
    detail = reverse("generic-denying-notes-detail", args=[secret.pk])
    # The detail route hides it too (404, not 403), as the list does.
    assert client.get(detail).status_code == 404
    response = client.get(reverse(route))
    assert response.status_code == 200, response.content
    assert [note["text"] for note in response.json()] == ["shown"]
    # The plain generic view (IsAuthorized) lists both.
    response = client.get(reverse("generic-notes-list"))
    assert sorted(note["text"] for note in response.json()) == ["secret", "shown"]


# Importing tests.urls registers resource types.
@pytest.mark.django_db
def test_only_a_class_overriding_an_object_check_lists_object_by_object():
    from scoped_rbac.permissions import IsAuthorized

    from scrud_django.filters import object_check_requirement
    from scrud_django.views import ResourceViewSet

    from .urls import (
        ContextDenyingIsAuthorized,
        LockedDenyingIsAuthorized,
        TeamIsAuthorized,
    )

    def view(*permission_classes):
        instance = ResourceViewSet()
        instance.permission_classes = list(permission_classes)
        return instance

    # The grants (and contexts_for) decide these lists in SQL.
    assert object_check_requirement(view(IsAuthorized)) is None
    assert object_check_requirement(view(TeamIsAuthorized)) is None
    # These ask the permission class for each listed object.
    for permission_class in (ContextDenyingIsAuthorized, LockedDenyingIsAuthorized):
        requirement = object_check_requirement(view(permission_class))
        assert [type(entry) for entry in requirement] == [permission_class]
    requirement = object_check_requirement(
        view(IsAuthorized | ContextDenyingIsAuthorized)
    )
    assert requirement is not None and len(requirement) == 1


@pytest.mark.django_db
def test_an_object_check_that_raises_refuses_that_object_only():
    # A has_object_permission that raises PermissionDenied refuses that
    # resource, as on its detail route, not the whole list. It refuses a
    # read, so the detail route hides it (404, not 403), as the list does.
    raising, shown = profile("raise", "a"), profile("shown", "a")
    client = client_with((PP_READER, "a"))
    detail = reverse("raising-partner-profiles-detail", args=[raising.pk])
    assert client.get(detail).status_code == 404
    assert (
        client.get(
            reverse("raising-partner-profiles-detail", args=[shown.pk])
        ).status_code
        == 200
    )
    response = client.get(reverse("raising-partner-profiles-list"))
    assert listed(response) == ["shown"]


@pytest.mark.django_db
def test_an_object_check_that_raises_not_found_hides_that_object_only():
    # DRF's NotFound (an APIException, not Http404) raised for one resource
    # hides it on its detail route; the list leaves it out, rather than
    # answering 404 for every reader.
    hidden, shown = profile("hide", "a"), profile("shown", "a")
    client = client_with((PP_READER, "a"))
    detail = reverse("raising-partner-profiles-detail", args=[hidden.pk])
    assert client.get(detail).status_code == 404
    assert (
        client.get(
            reverse("raising-partner-profiles-detail", args=[shown.pk])
        ).status_code
        == 200
    )
    response = client.get(reverse("raising-partner-profiles-list"))
    assert listed(response) == ["shown"]
    assert response.json()["count"] == 1
