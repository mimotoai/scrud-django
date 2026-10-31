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


# A revert (reversion's Version.revert() or Revision.revert(), the admin's
# "recover" and "revert") is a new write of old content: it gets a fresh ETag
# and Last-Modified, never the version's, so a client revalidating with
# If-Modified-Since, or a list whose newest resource it is, sees the change.
# loaddata keeps a dump's validators.


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
def test_loaddata_keeps_the_dumped_validators(tmp_path):
    from django.core.management import call_command

    resource = ResourceRegistration.register({"name": "kept"}, "partner-profiles")
    dump = tmp_path / "resource.json"
    call_command(
        "dumpdata",
        "scrud_django.Resource",
        pks=str(resource.pk),
        output=str(dump),
        verbosity=0,
    )
    Resource.objects.filter(pk=resource.pk).update(content={"name": "changed"})
    call_command("loaddata", str(dump), verbosity=0)
    loaded = stored(resource)
    assert loaded.content == {"name": "kept"}
    # Django's JSON serializer keeps milliseconds.
    dumped_at = resource.modified_at.replace(
        microsecond=resource.modified_at.microsecond // 1000 * 1000
    )
    assert (loaded.etag, loaded.modified_at) == (resource.etag, dumped_at)
