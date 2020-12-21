import json
from datetime import timezone

from django.db.models import Manager
from django.utils.http import http_date, quote_etag
from rest_framework import serializers
from rest_framework.reverse import reverse

from scrud_django import models
from scrud_django.headers import get_link_header_for
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.validators import JsonValidator


class EnvelopeSerializer(serializers.ListSerializer):
    def to_representation(self, data):
        iterable = data.all() if isinstance(data, Manager) else data
        return [self.child.to_representation(item, envelope=True) for item in iterable]


class ResourceSerializer(serializers.Serializer):
    class Meta:
        model = models.Resource
        list_serializer_class = EnvelopeSerializer
        validators = [JsonValidator()]

    def __init__(self, *args, **kwargs):
        self.resource_type = kwargs.pop("resource_type")
        super().__init__(*args, **kwargs)

    def to_representation(self, instance, envelope=False, context=None):
        if type(instance.content) is str:
            content = json.loads(instance.content)
        else:
            content = instance.content
        if not envelope:
            return content
        request = self._context["request"]
        last_modified = http_date(
            instance.modified_at.replace(tzinfo=timezone.utc).timestamp()
        )
        return {
            'href': reverse(
                self.resource_type.route_name_detail(),
                args=[instance.id],
                request=request,
            ),
            'last_modified': last_modified,
            'etag': quote_etag(instance.etag),
            'link': get_link_header_for(self.resource_type, request),
            'content': content,
        }

    def to_internal_value(self, data):
        return data

    def create(self, validated_data):
        instance = Resource(content=validated_data, resource_type=self.resource_type)
        return ResourceRegistration.register_instance(instance)

    def update(self, instance, validated_data):
        return ResourceRegistration.update(
            content=validated_data,
            register_type=self.resource_type.slug,
            slug=instance.slug,
        )


class JSONSchemaSerializer(serializers.Serializer):
    """Serializer for JSON-Schema data."""

    class Meta:
        fields = ["$id", "$schema", "title", "description", "properties"]


class JSONLDSerializer(serializers.Serializer):
    """Serializer for JSON-LD data."""

    class Meta:
        fields = ["$id", "$schema", "title", "description", "properties"]
