"""`ResourceSerializer` used directly, outside the views: its resource type
fallback and ``serializer.save()`` on an existing resource."""

import pytest
from rest_framework.test import APIRequestFactory

from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    register_json_ld_resource_type,
)
from scrud_django.serializers import ResourceSerializer


class SluggedSerializer(ResourceSerializer):
    """A serializer that names its type by ``Meta.resource_type_slug``."""

    class Meta(ResourceSerializer.Meta):
        resource_type_slug = "json-ld"


@pytest.mark.django_db
def test_a_serializer_without_a_resource_type_falls_back_to_its_meta():
    register_json_ld_resource_type()
    assert ResourceSerializer().resource_type is None
    assert SluggedSerializer().resource_type == get_resource_type_for("json-ld")
    given = get_resource_type_for("partner-profiles")
    assert SluggedSerializer(resource_type=given).resource_type is given


@pytest.mark.django_db
def test_serializer_save_updates_an_existing_resource():
    resource_type = register_json_ld_resource_type()
    resource = ResourceRegistration.register({"name": "before"}, "json-ld")
    old_etag = resource.etag
    old_type_etag = resource.resource_type.etag
    count = Resource.objects.count()
    request = APIRequestFactory().put("/")
    serializer = ResourceSerializer(
        instance=resource,
        data={"name": "after"},
        resource_type=resource_type,
        context={"request": request},
    )
    assert serializer.is_valid(), serializer.errors
    saved = serializer.save()
    assert saved.pk == resource.pk
    stored = Resource.objects.get(pk=resource.pk)
    assert stored.content == {"name": "after"}
    assert stored.etag == saved.etag != old_etag
    assert stored.resource_type.etag == saved.etag != old_type_etag
    assert Resource.objects.count() == count
