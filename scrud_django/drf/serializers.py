"""The DRF serializers of resources and the mixins they are built from
(``GenericSerializerMixin``, ``SerializerMixin``)."""

import json
from collections.abc import Mapping
from functools import cached_property

from django.db import transaction
from django.db.models import Manager
from django.utils.http import quote_etag
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.reverse import reverse
from rest_framework.settings import api_settings

from scrud_django import models
from scrud_django.core.writes import JsonValidator
from scrud_django.drf.validators import WorkflowValidator
from scrud_django.headers import get_link_header_for
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    lock_if_workflow_policy,
    write_db,
)
from scrud_django.utils import http_date_for


class GenericSerializerMixin:
    def __init__(self, *args, **kwargs):
        self.envelope = kwargs.pop("envelope", False)
        self._resource_type = kwargs.pop("resource_type", None)
        super().__init__(*args, **kwargs)

    @cached_property
    def resource_type(self):
        if self._resource_type is not None:
            return self._resource_type
        slug = getattr(self.Meta, "resource_type_slug", None)
        return get_resource_type_for(slug) if slug is not None else None

    def link_to_self(self, instance, request):
        return reverse(
            self.resource_type.route_name_detail(),
            args=[instance.id],
            request=request,
        )

    def to_content(self, instance, context=None):
        return super().to_representation(instance)

    def to_representation(self, instance, envelope=False, context=None):
        content = self.to_content(instance, context=context)
        if not envelope and not self.envelope:
            return content
        request = self._context.get("request", None)
        if request is None and context is not None:
            request = context.get("request", None)
        last_modified = http_date_for(instance.modified_at)
        return {
            'href': self.link_to_self(instance, request),
            'last_modified': last_modified,
            'etag': quote_etag(instance.etag),
            'link': get_link_header_for(self.resource_type, request),
            'content': content,
        }

    def to_internal_value(self, data):
        # A resource is a JSON object: its ``rbac_context`` decides where it is
        # stored, and conditions read its fields.
        if not isinstance(data, Mapping):
            raise ValidationError(
                {api_settings.NON_FIELD_ERRORS_KEY: ["Expected a JSON object."]},
                code="invalid",
            )
        return data

    def create(self, validated_data):
        """Create and return instance"""
        return NotImplementedError()

    def update(self, instance, validated_data):
        """Update and return instance"""
        return NotImplementedError()


class SerializerMixin(GenericSerializerMixin):
    # ``resource_type`` is GenericSerializerMixin's: the type given as the
    # ``resource_type`` keyword, else the one ``Meta.resource_type_slug``
    # names, else ``None``.

    def to_content(self, instance, context=None):
        if type(instance.content) is str:
            content = json.loads(instance.content)
        else:
            content = instance.content
        return content


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
        """Write `validated_data` to `instance` (``serializer.save()`` on an
        existing resource) through `ResourceRegistration.update_locked`, in a
        transaction on `write_db` (the router's database for resource writes,
        given the instance as its hint; the instance's own database only
        when no router decides, as Django has it), after the
        ``WorkflowPolicy`` type lock. The instance is
        written as it is, as the views write theirs: to keep what was
        validated from changing underneath, read it with its row locked
        (`get_locked_resource`) inside a transaction there, as the views do."""
        resource_type = self.resource_type or instance.resource_type
        db = write_db(instance)
        with transaction.atomic(using=db):
            lock_if_workflow_policy(resource_type, db)
            return ResourceRegistration.update_locked(
                instance, validated_data, resource_type=resource_type, using=db
            )


class JSONSchemaSerializer(serializers.Serializer):
    """Serializer for JSON-Schema data."""

    class Meta:
        fields = ["$id", "$schema", "title", "description", "properties"]


class JSONLDSerializer(serializers.Serializer):
    """Serializer for JSON-LD data."""

    class Meta:
        fields = ["$id", "$schema", "title", "description", "properties"]
