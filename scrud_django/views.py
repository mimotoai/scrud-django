from typing import Optional

from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.reverse import reverse, reverse_lazy
from rest_framework.views import APIView

from scrud_django import serializers, services
from scrud_django.decorators import scrudful_api_view, scrudful_viewset
from scrud_django.models import Resource, ResourceType
from scrud_django.paginations import StandardResultsSetPagination
from scrud_django.registration import ResourceRegistration


def get_schema_uri_for(resource_type, request):
    uri = None
    # prefer a local version to a remote version
    if resource_type.schema:
        schema = resource_type.schema
        uri = reverse_lazy(
            schema.resource_type.route_name_detail(),
            args=[schema.id],
            request=request,
        )
    elif resource_type.schema_uri:
        uri = resource_type.schema_uri
    return uri


def get_context_uri_for(resource_type, request):
    uri = None
    # prefer a local version to a remote version
    if resource_type.context:
        context = resource_type.context
        uri = reverse_lazy(
            context.resource_type.route_name_detail(),
            args=[context.id],
            request=request,
        )
    elif resource_type.context_uri:
        uri = resource_type.context_uri
    return uri


# RESOURCE


@scrudful_viewset
class ResourceViewSet(viewsets.ModelViewSet):
    queryset = Resource.objects.all()
    serializer_class = serializers.ResourceSerializer
    pagination_class = StandardResultsSetPagination

    # scrud variable
    resource_type_name: Optional[str] = None

    class Meta:
        def etag_func(view_instance, request, slug: str):
            instance = view_instance.get_instance(request, slug)
            return instance.etag

        def last_modified_func(view_instance, request, slug: str):
            instance = view_instance.get_instance(request, slug)
            return instance.modified_at

        def schema_link_or_func(view_instance, request, slug: str = None):
            if view_instance.resource_type_name:
                resource_type = get_object_or_404(
                    ResourceType, slug=view_instance.resource_type_name
                )
                return get_schema_uri_for(resource_type, request)
            return None

        def context_link_or_func(view_instance, request, slug: str = None):
            if view_instance.resource_type_name:
                resource_type = get_object_or_404(
                    ResourceType, slug=view_instance.resource_type_name
                )
                return get_context_uri_for(resource_type, request)

        def list_etag_func(view_instance, request, *args, **kwargs):
            resource_type = get_object_or_404(
                ResourceType, slug=view_instance.resource_type_name
            )
            return resource_type.etag

        def list_last_modified_func(view_instance, request, *args, **kwargs):
            resource_type = get_object_or_404(
                ResourceType, slug=view_instance.resource_type_name
            )
            return resource_type.modified_at

        def list_schema_link_or_func(view_instance, request, *args, **kwargs):
            resource_type = get_object_or_404(
                ResourceType, slug=view_instance.resource_type_name
            )
            return reverse_lazy(
                "collections-json-schema",
                args=[resource_type.slug],
                request=request,
            )

        def list_context_link_or_func(view_instance, request, *args, **kwargs):
            resource_type = get_object_or_404(
                ResourceType, slug=view_instance.resource_type_name
            )
            return reverse_lazy(
                "collections-json-ld",
                args=[resource_type.slug],
                request=request,
            )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.resource_type_name = kwargs.get('resource_type_name')

    def get_permissions(self):
        """
        Returns the list of permissions that this view requires.
        """
        if self.action in ['list', 'retrieve']:
            permission_classes = [AllowAny]
        else:
            permission_classes = [AllowAny]
        return [permission() for permission in permission_classes]

    def get_instance(self, request, slug: str):
        resource_type = get_object_or_404(
            ResourceType, slug=self.resource_type_name
        )
        instance = get_object_or_404(
            Resource, resource_type=resource_type, pk=int(slug)
        )
        return instance

    def create(self, request):
        """Create a Resource."""
        instance = ResourceRegistration.register(
            content=request.data, register_type=self.resource_type_name
        )
        serializer = self.get_serializer(instance=instance, many=False)
        headers = {
            'Location': reverse_lazy(
                instance.resource_type.route_name_detail(),
                args=[instance.id],
                request=request,
            )
        }
        return Response(
            serializer.data, headers=headers, status=status.HTTP_201_CREATED
        )

    def update(self, request, slug: str):
        """Update a Resource."""
        instance = ResourceRegistration.update(
            content=request.data,
            register_type=self.resource_type_name,
            slug=slug,
        )
        serializer = self.get_serializer(instance=instance, many=False)
        return Response(serializer.data)

    def destroy(self, request, slug: str):
        """Update a Resource."""
        ResourceRegistration.delete(
            register_type=self.resource_type_name, slug=slug,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    def retrieve(self, request, slug: str):
        """Return the resource for the given resource type name."""
        instance = self.get_instance(request, slug)
        serializer = self.get_serializer(instance=instance, many=False)
        return Response(serializer.data)

    def list(self, request):
        """Return the resource for the given resource type name."""
        resource_type = get_object_or_404(
            ResourceType, slug=self.resource_type_name
        )

        queryset = Resource.objects.filter(resource_type=resource_type)
        page = self.paginate_queryset(queryset)

        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return Response(self.get_paginated_response(serializer.data))

        serializer = self.get_serializer(instance=queryset, many=True)
        return Response(serializer.data)


# JSON-SCHEMA


class JSONSchemaViewSet(viewsets.ViewSet):
    """View set for JSON-Schema requests."""

    serializer_class = serializers.JSONSchemaSerializer
    permission_classes_mapping = {
        'list': [AllowAny],
        'create': [IsAuthenticated],
        'retrieve': [AllowAny],
        'update': [IsAuthenticated],
        'partial_update': [IsAuthenticated],
        'destroy': [IsAuthenticated],
    }

    def get_queryset(self):
        return ResourceType.objects.all()

    def create(self, request, data: dict):
        """Return the JSON Schema for the given Resource."""
        raise NotImplementedError('``Create`` not implemented yet.')

    def retrieve(self, request, slug: str):
        """Return the JSON Schema for the given Resource."""
        resource_type = ResourceType.objects.filter(type_uri=slug)
        instance = Resource.objects.filter(resource_type=resource_type)
        serializer = self.serializer_class(instance=instance, many=False)
        return Response(serializer.data)

    def list(self, request):
        """Return the JSON Schema for the given Resource."""
        instance = Resource.objects.filter()
        serializer = self.serializer_class(instance=instance, many=True)
        return Response(serializer.data)


# JSON-SCHEMA


class JSONLDViewSet(viewsets.ViewSet):
    """View set for JSON-LD requests."""

    serializer_class = serializers.JSONLDSerializer
    permission_classes_mapping = {
        'list': [AllowAny],
        'create': [IsAuthenticated],
        'retrieve': [AllowAny],
        'update': [IsAuthenticated],
        'partial_update': [IsAuthenticated],
        'destroy': [IsAuthenticated],
    }

    def get_queryset(self):
        return ResourceType.objects.all()

    def create(self, request, data: dict):
        """Return the JSON LD for the given Resource."""
        raise NotImplementedError('``Create`` not implemented yet.')

    def retrieve(self, request, slug: str):
        """Return the JSON LD for the given Resource."""
        resource_type = ResourceType.objects.filter(type_uri=slug)
        instance = Resource.objects.filter(resource_type=resource_type)
        serializer = self.serializer_class(instance=instance, many=False)
        return Response(serializer.data)

    def list(self, request):
        """Return the JSON LD for the given Resource."""
        instance = Resource.objects.filter()
        serializer = self.serializer_class(instance=instance, many=True)
        return Response(serializer.data)


class ResourceCollectionSchemaView(APIView):
    def get(self, request, slug: str):
        resource_type = get_object_or_404(ResourceType, slug=slug,)

        if resource_type.schema:
            content_defn = resource_type.schema.content
        elif resource_type.schema_uri:
            content_defn = {"$ref": resource_type.schema_uri}
        else:
            content_defn = {"type": "any"}

        schema = {
            "$id": "https://api.openteams.com/json-schema/ResourceCollection"
            f"?contents_type={resource_type.type_uri}",
            "$schema": "http://json-schema.org/draft-04/schema",
            "description": f"A listing of resources of type {resource_type.type_uri}.",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "The total number of items in the collection.",
                },
                "page_count": {
                    "type": "integer",
                    "description": "The total number of pages in the collection.",
                },
                "first": {
                    "type": "string",
                    "format": "uri",
                    "description": "URL of the first page.",
                },
                "previous": {
                    "type": "string",
                    "format": "uri",
                    "description": "URL of the previous page, if any.",
                },
                "next": {
                    "type": "string",
                    "format": "uri",
                    "description": "URL of the next page, if any.",
                },
                "last": {
                    "type": "string",
                    "format": "uri",
                    "description": "URL of the last page.",
                },
                "content": {
                    "properties": {
                        "type": "array",
                        "description": f"Listing of {resource_type.type_uri} "
                        "resources in Envelopes.",
                        "items": {
                            "properties": {
                                "href": {
                                    "type": "string",
                                    "format": "uri",
                                    "description": "URL of the nested resource.",
                                },
                                "etag": {
                                    "type": "string",
                                    "description": "HTTP ETag header of the nested "
                                    "resource.",
                                },
                                "last_modified": {
                                    "type": "string",
                                    "description": "HTTP Last-Modified header of the "
                                    "nested resource.",
                                },
                                "content": content_defn,
                            },
                        },
                    },
                },
            },
        }

        return Response(schema)


class ResourceCollectionContextView(APIView):
    def get(self, request, slug: str):
        resource_type = get_object_or_404(ResourceType, slug=slug,)
        return Response(
            {
                "openteams": "https://api.openteams.com/json-ld/",
                "count": {"@id": "openteams:count"},
                "page_count": {"@id": "openteams:page_count"},
                "first": {"@id": "opententeams:first"},
                "previous": {"@id": "opententeams:previous"},
                "next": {"@id": "opententeams:next"},
                "last": {"@id": "opententeams:last"},
                "content": {
                    "@id": "openteams:Envelope",
                    "@container": "@list",
                    "openteams:envelopeContents": resource_type.type_uri,
                },
            }
        )


@scrudful_api_view(
    etag_func=lambda *args, **kwargs: services.etag,
    last_modified_func=lambda *args, **kwargs: services.last_modified,
)
@permission_classes([AllowAny])
def get_service_list(request, *args, **kwargs):
    catalog = {}
    for k, v in services.services.items():
        catalog[k] = reverse(f'{v}-list', request=request)
    return Response(catalog)
