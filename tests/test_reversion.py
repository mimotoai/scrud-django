import pytest
from django.db import transaction
from reversion.models import Version

from scrud_django.models import Resource, ResourceRemoval
from scrud_django.registration import ResourceRegistration

from .factories import fake_resource


@pytest.fixture
def resource_creator(db):
    new_resource = fake_resource()
    new_resource = ResourceRegistration.register(
        new_resource.content, new_resource.resource_type.slug
    )

    return new_resource


@pytest.mark.django_db
def test_reversion_create(resource_creator):
    versions = Version.objects.filter(object_id=resource_creator.pk).count()
    assert versions > 0


@pytest.mark.django_db
def test_version_update(resource_creator):
    created_resource = ResourceRegistration.update(
        resource_creator.content,
        resource_creator.resource_type.slug,
        resource_creator.pk,
    )
    versions = Version.objects.filter(object_id=created_resource.pk).count()
    assert versions >= 2


# Every raw save of a resource (reversion's Version.revert() or
# Revision.revert(), the admin's "recover" and "revert", loaddata of a backup)
# is a new write of old content: it gets a fresh ETag and Last-Modified,
# never the stored ones, so a client revalidating with If-Modified-Since, or a
# list whose newest resource it is, sees the change. Clients refetch once.


def stored(resource):
    return Resource.objects.get(pk=resource.pk)


@pytest.mark.django_db
@pytest.mark.parametrize("how", ["version", "revision"])
def test_a_revert_gives_the_resource_fresh_validators(how):
    first = ResourceRegistration.register({"name": "a"}, "partner-profiles")
    second = ResourceRegistration.update(
        {"name": "b"}, "partner-profiles", str(first.pk)
    )
    oldest = Version.objects.get_for_object(first).order_by("pk").first()
    assert oldest.field_dict["etag"] == first.etag
    with transaction.atomic():
        if how == "version":
            oldest.revert()
        else:
            oldest.revision.revert()
    reverted = stored(first)
    assert reverted.content == {"name": "a"}
    assert reverted.etag not in (first.etag, second.etag)
    assert reverted.modified_at > second.modified_at


@pytest.mark.django_db
def test_recovering_a_deleted_resource_gives_it_fresh_validators():
    resource = ResourceRegistration.register(
        {"name": "gone", "rbac_context": "r"}, "partner-profiles"
    )
    ResourceRegistration.delete("partner-profiles", str(resource.pk))
    removed_at = ResourceRemoval.objects.get(rbac_context="r").removed_at
    deleted = Version.objects.get_deleted(Resource).get(object_id=str(resource.pk))
    deleted.revision.revert()
    recovered = stored(resource)
    assert recovered.content == {"name": "gone", "rbac_context": "r"}
    assert recovered.etag != resource.etag
    # Past the deletion's stamp: a list it is back in is modified.
    assert recovered.modified_at > removed_at


@pytest.mark.django_db
def test_loaddata_gives_fresh_validators(tmp_path):
    # Restoring an older backup has a revert's problem: keeping the dump's
    # validators moved ETag and Last-Modified back in time, so a client's
    # If-Modified-Since got 304 for content that changed. The loaded
    # resource gets new ones instead.
    from django.core.management import call_command
    from django.utils import timezone

    resource = ResourceRegistration.register({"name": "kept"}, "partner-profiles")
    dump = tmp_path / "resource.json"
    call_command(
        "dumpdata",
        "scrud_django.Resource",
        pks=str(resource.pk),
        output=str(dump),
        verbosity=0,
    )
    changed = ResourceRegistration.update(
        {"name": "changed"}, "partner-profiles", str(resource.pk)
    )
    before_load = timezone.now()
    call_command("loaddata", str(dump), verbosity=0)
    loaded = stored(resource)
    assert loaded.content == {"name": "kept"}
    assert loaded.etag not in (resource.etag, changed.etag)
    assert loaded.modified_at >= before_load > changed.modified_at


@pytest.mark.django_db
def test_a_restored_list_is_modified_after_every_removal_stamp(tmp_path):
    # A dump/flush/load keeps the removal stamps as dumped; the loaded
    # resources are newer than all of them, so a list's Last-Modified is the
    # load, never earlier than a stamp or than before the load.
    from django.core.management import call_command
    from django.test import Client
    from django.urls import reverse
    from django.utils import timezone
    from django.utils.http import parse_http_date

    from .factories import UserFactory
    from .test_resource_contexts import flush

    reverse("partner-profiles-list")
    kept = ResourceRegistration.register(
        {"name": "kept", "rbac_context": "a"}, "partner-profiles"
    )
    ResourceRegistration.update(
        {"name": "kept", "rbac_context": "b"}, "partner-profiles", str(kept.pk)
    )
    gone = ResourceRegistration.register(
        {"name": "gone", "rbac_context": "a"}, "partner-profiles"
    )
    ResourceRegistration.delete("partner-profiles", str(gone.pk))
    client = Client()
    client.force_login(UserFactory(is_staff=True, is_superuser=True))
    url = reverse("partner-profiles-list")
    listed_before = parse_http_date(client.get(url)["Last-Modified"])
    stamps_before = dict(
        ResourceRemoval.objects.values_list("rbac_context", "removed_at")
    )
    dump = tmp_path / "scrud.json"
    call_command("dumpdata", "scrud_django", output=str(dump), verbosity=0)
    flush()
    before_load = timezone.now()
    call_command("loaddata", str(dump), verbosity=0)
    client.force_login(UserFactory(is_staff=True, is_superuser=True))
    stamps = dict(ResourceRemoval.objects.values_list("rbac_context", "removed_at"))
    assert set(stamps) == set(stamps_before)
    newest_stamp = max(stamps.values())
    for resource in Resource.objects.all():
        assert resource.modified_at >= before_load > newest_stamp, resource.pk
    listed = parse_http_date(client.get(url)["Last-Modified"])
    assert listed >= int(before_load.timestamp()) >= listed_before
