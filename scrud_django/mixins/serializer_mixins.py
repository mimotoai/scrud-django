from functools import cached_property
import json
from datetime import timezone

from django.utils.http import http_date, quote_etag
from rest_framework.reverse import reverse

from scrud_django.headers import get_link_header_for
from scrud_django.registration import get_resource_type_for


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
        last_modified = http_date(
            instance.modified_at.replace(tzinfo=timezone.utc).timestamp()
        )
        return {
            'href': self.link_to_self(instance, request),
            'last_modified': last_modified,
            'etag': quote_etag(instance.etag),
            'link': get_link_header_for(self.resource_type, request),
            'content': content,
        }

    def to_internal_value(self, data):
        return data

    def create(self, validated_data):
        """Create and return instance"""
        return NotImplementedError()

    def update(self, instance, validated_data):
        """Update and return instance"""
        return NotImplementedError()


class SerializerMixin(GenericSerializerMixin):
    @cached_property
    def resource_type(self):
        return self._resource_type if self._resource_type is not None else super().resource_type()

    def to_content(self, instance, context=None):
        if type(instance.content) is str:
            content = json.loads(instance.content)
        else:
            content = instance.content
        return content
