"""A list's ``Last-Modified`` moves when a resource the requester could list
leaves it: deleted, or moved out of the contexts they read it in. Each
removal stamps the contexts the resource left (`ResourceRemoval`), and a
list takes the newest stamp of the contexts the requester reads every
resource of (an outright grant; every context for a superuser or a grant
in every context). A removal in a context the requester reads only by a
condition, or not at all, does not move it: it would tell them about a
resource they may not have been able to read (the list's ETag still
follows what they see)."""

import datetime
from uuid import uuid4

import pytest
from django.conf import settings
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.http import parse_http_date, quote_etag
from rest_framework.test import APIClient
from scoped_rbac.models import Role, RoleAssignment

from scrud_django.models import Resource, ResourceRemoval
from scrud_django.registration import ResourceRegistration

from .factories import UserFactory
from .rbac_defaults import TYPE_URI_LIST_PARTNER_PROFILES, TYPE_URI_PARTNER_PROFILES

PP = TYPE_URI_PARTNER_PROFILES
PP_LIST = TYPE_URI_LIST_PARTNER_PROFILES
READER = {"http.GET": [PP, PP_LIST]}
WRITER = {m: [PP, PP_LIST] for m in ("http.GET", "http.PUT", "http.DELETE")}
AN_HOUR_AGO = datetime.timedelta(hours=1)


def client_with(*grants, **flags):
    user = UserFactory(**flags)
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
    client = APIClient()
    client.force_authenticate(user)
    return client


def profile(name, **content):
    """A partner profile stored server-side an hour ago, so that a later
    change is a later HTTP date."""
    reverse("partner-profiles-list")
    resource = ResourceRegistration.register(
        dict({"name": name}, **content), "partner-profiles"
    )
    Resource.objects.filter(pk=resource.pk).update(
        modified_at=timezone.now() - AN_HOUR_AGO
    )
    resource.refresh_from_db()
    return resource


def list_url():
    return reverse("partner-profiles-list")


def detail(resource):
    return reverse("partner-profiles-detail", args=[resource.pk])


def last_modified(client, **headers):
    response = client.get(list_url(), **headers)
    assert response.status_code == 200, response.content
    return response["Last-Modified"]


def is_now(header):
    return abs(parse_http_date(header) - timezone.now().timestamp()) < 60


@pytest.mark.django_db
def test_deleting_the_newest_listed_resource_moves_last_modified():
    older = profile("older", rbac_context="a")
    newest = profile("newest", rbac_context="a")
    Resource.objects.filter(pk=older.pk).update(
        modified_at=timezone.now() - 2 * AN_HOUR_AGO
    )
    reader = client_with((READER, "a"))
    before = last_modified(reader)
    assert parse_http_date(before) == int(newest.modified_at.timestamp())
    assert reader.get(list_url(), HTTP_IF_MODIFIED_SINCE=before).status_code == 304

    writer = client_with((WRITER, "a"))
    response = writer.delete(detail(newest), HTTP_IF_MATCH=quote_etag(newest.etag))
    assert response.status_code == 204, response.content

    response = reader.get(list_url(), HTTP_IF_MODIFIED_SINCE=before)
    assert response.status_code == 200, response.content
    assert is_now(response["Last-Modified"]), response["Last-Modified"]
    assert [e["content"]["name"] for e in response.json()["content"]] == ["older"]


@pytest.mark.django_db
def test_moving_a_resource_out_of_the_readers_context_moves_last_modified():
    profile("stays", rbac_context="a")
    leaves = profile("leaves", rbac_context="a")
    reader = client_with((READER, "a"))
    before = last_modified(reader)

    writer = client_with((WRITER, "a"), (WRITER, "b"))
    response = writer.put(
        detail(leaves),
        {"name": "leaves", "rbac_context": "b"},
        format="json",
        HTTP_IF_MATCH=quote_etag(leaves.etag),
    )
    assert response.status_code == 200, response.content

    response = reader.get(list_url(), HTTP_IF_MODIFIED_SINCE=before)
    assert response.status_code == 200, response.content
    assert is_now(response["Last-Modified"])
    assert [e["content"]["name"] for e in response.json()["content"]] == ["stays"]


@pytest.mark.django_db
def test_a_removal_elsewhere_does_not_move_last_modified():
    profile("mine", rbac_context="a")
    theirs = profile("theirs", rbac_context="b")
    reader = client_with((READER, "a"))
    before = last_modified(reader)
    ResourceRegistration.delete("partner-profiles", str(theirs.pk))
    assert ResourceRemoval.objects.filter(rbac_context="b").exists()
    assert reader.get(list_url(), HTTP_IF_MODIFIED_SINCE=before).status_code == 304
    assert last_modified(reader) == before


@pytest.mark.django_db
def test_a_removal_a_condition_decides_does_not_move_last_modified():
    # The reader reads "a" outright and "c" only by a condition: a removal in
    # "c" may be of a resource the condition refuses, so it moves nothing.
    profile("mine", rbac_context="a")
    keep = profile("keep", rbac_context="c")
    hidden = profile("hidden", rbac_context="c")
    reader = client_with(
        (READER, "a"), ({"http.GET": {PP: {"condition": 'name == "keep"'}}}, "c")
    )
    response = reader.get(list_url())
    before, etag = response["Last-Modified"], response["ETag"]
    assert {e["content"]["name"] for e in response.json()["content"]} == {
        "mine",
        "keep",
    }

    ResourceRegistration.delete("partner-profiles", str(hidden.pk))
    response = reader.get(
        list_url(), HTTP_IF_MODIFIED_SINCE=before, HTTP_IF_NONE_MATCH=etag
    )
    assert response.status_code == 304
    assert reader.get(list_url(), HTTP_IF_MODIFIED_SINCE=before).status_code == 304

    # Deleting one it listed changes the ETag.
    ResourceRegistration.delete("partner-profiles", str(keep.pk))
    assert reader.get(list_url(), HTTP_IF_NONE_MATCH=etag).status_code == 200


@pytest.mark.django_db
def test_superusers_and_readers_everywhere_see_every_removal():
    profile("stays", rbac_context="a")
    gone = profile("gone", rbac_context="b")
    contextless = profile("contextless", rbac_context=[])
    superuser = client_with(is_superuser=True, is_staff=True)
    before = last_modified(superuser)

    Resource.objects.get(pk=contextless.pk).delete()
    response = superuser.get(list_url(), HTTP_IF_MODIFIED_SINCE=before)
    assert response.status_code == 200, response.content
    assert is_now(response["Last-Modified"])

    anywhere = dict(
        settings.SCOPED_RBAC,
        POLICY_FOR_AUTHENTICATED="tests.rbac_defaults."
        "policy_read_partner_profiles_anywhere",
    )
    with override_settings(SCOPED_RBAC=anywhere):
        reader = client_with()
        # The contextless deletion is the superusers' only.
        before = last_modified(reader)
        assert reader.get(list_url(), HTTP_IF_MODIFIED_SINCE=before).status_code == 304
        ResourceRegistration.delete("partner-profiles", str(gone.pk))
        response = reader.get(list_url(), HTTP_IF_MODIFIED_SINCE=before)
        assert response.status_code == 200, response.content
        assert is_now(response["Last-Modified"])


@pytest.mark.django_db
def test_each_removal_stamps_the_contexts_the_resource_left():
    resource = profile("x", rbac_context=["a", "b"])
    ResourceRegistration.update(
        {"name": "x", "rbac_context": ["b", "c"]}, "partner-profiles", resource.pk
    )
    stamps = dict(
        ResourceRemoval.objects.filter(
            resource_type=resource.resource_type
        ).values_list("rbac_context", "removed_at")
    )
    assert set(stamps) == {"a"}
    first = stamps["a"]
    Resource.objects.filter(pk=resource.pk).delete()
    stamps = dict(
        ResourceRemoval.objects.filter(
            resource_type=resource.resource_type
        ).values_list("rbac_context", "removed_at")
    )
    assert set(stamps) == {"a", "b", "c"}
    assert stamps["a"] >= first
    # One row per type and context, however many removals.
    other = profile("y", rbac_context="a")
    other.delete()
    assert (
        ResourceRemoval.objects.filter(
            resource_type=resource.resource_type, rbac_context="a"
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_a_bulk_delete_stamps_each_type_once_in_one_order(monkeypatch):
    # One sorted call per type, after the rows are gone, whatever order the
    # resources were collected in: two concurrent bulk deletes then take the
    # stamp rows' locks in the same order and cannot deadlock.
    from scrud_django.models import resource as resource_module

    first = profile("first", rbac_context="b")
    second = profile("second", rbac_context=["c", "a"])
    calls = []
    original = resource_module.stamp_removals

    def recording(resource_type_id, contexts, using):
        calls.append((resource_type_id, list(contexts)))
        return original(resource_type_id, contexts, using)

    monkeypatch.setattr(resource_module, "stamp_removals", recording)
    Resource.objects.filter(pk__in=[first.pk, second.pk]).delete()
    assert calls == [(first.resource_type_id, ["a", "b", "c"])]
    assert not Resource.objects.filter(pk__in=[first.pk, second.pk]).exists()
    assert set(
        ResourceRemoval.objects.filter(
            resource_type_id=first.resource_type_id
        ).values_list("rbac_context", flat=True)
    ) == {"a", "b", "c"}
