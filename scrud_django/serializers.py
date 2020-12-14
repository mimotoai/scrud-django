import json
from datetime import timezone

from django.db.models import Manager
from django.utils.http import http_date, quote_etag
from rest_framework import serializers
from rest_framework.reverse import reverse_lazy

from scrud_django import models


class EnvelopeSerializer(serializers.ListSerializer):
    def to_representation(self, data):
        iterable = data.all() if isinstance(data, Manager) else data
        return [
            self.child.to_representation(item, envelope=True)
            for item in iterable
        ]


class ResourceSerializer(serializers.Serializer):
    class Meta:
        model = models.Resource
        list_serializer_class = EnvelopeSerializer

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
            'href': reverse_lazy(
                instance.resource_type.route_name_detail(),
                args=[instance.id],
                request=request,
            ),
            'last_modified': last_modified,
            'etag': quote_etag(instance.etag),
            'content': content,
        }


class JSONSchemaSerializer(serializers.Serializer):
    """Serializer for JSON-Schema data."""

    class Meta:
        fields = ["$id", "$schema", "title", "description", "properties"]


class JSONLDSerializer(serializers.Serializer):
    """Serializer for JSON-LD data."""

    class Meta:
        fields = ["$id", "$schema", "title", "description", "properties"]
