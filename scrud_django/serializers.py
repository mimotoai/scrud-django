from django.db.models import Manager
from rest_framework import serializers

from scrud_django import models
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.mixins.serializer_mixins import SerializerMixin
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.validators import JsonValidator, WorkflowValidator


class EnvelopeSerializer(serializers.ListSerializer):
    def to_representation(self, data):
        iterable = data.all() if isinstance(data, Manager) else data
        return [self.child.to_representation(item, envelope=True) for item in iterable]


class ResourceSerializer(SerializerMixin, serializers.Serializer):
    class Meta:
        model = models.Resource
        list_serializer_class = EnvelopeSerializer
        validators = [JsonValidator(), WorkflowValidator()]

    @staticmethod
    def contexts_from(body):
        """The contexts scoped-rbac's request check authorizes a write of
        `body` in (``IsAuthorized.contexts_from``): `rbac_contexts_of`, the
        contexts the resource is stored in and read in. A body whose
        ``rbac_context`` is absent, ``None``, ``""`` or not a string (or a
        list of strings), for example a JSON-LD term definition, is written
        in the default context ``""``. scoped-rbac's default
        (``access.body_contexts``) would authorize such a value as a context
        itself, which no role grants."""
        return rbac_contexts_of(body)

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
