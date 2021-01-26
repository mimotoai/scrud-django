from typing import Optional

from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.reverse import reverse
from rest_framework.views import APIView
from scoped_rbac.rest import AccessControlledAPIView

from scrud_django import collection_type_uri_for, serializers, services
from scrud_django.decorators import scrudful_api_view, scrudful_viewset
from scrud_django.headers import get_context_uri_for, get_schema_uri_for
from scrud_django.models import Resource
from scrud_django.paginations import StandardResultsSetPagination
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
)
from scrud_django.validators import JsonValidator


@scrudful_viewset
class ResourceViewSet(AccessControlledAPIView, viewsets.ModelViewSet):
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
                resource_type = view_instance.get_resource_type()
                return get_schema_uri_for(resource_type, request)
            return None

        def context_link_or_func(view_instance, request, slug: str = None):
            if view_instance.resource_type_name:
                resource_type = view_instance.get_resource_type()
                return get_context_uri_for(resource_type, request)
            return None

        def list_etag_func(view_instance, request, *args, **kwargs):
            resource_type = view_instance.get_resource_type()
            return resource_type.etag

        def list_last_modified_func(view_instance, request, *args, **kwargs):
            resource_type = view_instance.get_resource_type()
            return resource_type.modified_at

        def list_schema_link_or_func(view_instance, request, *args, **kwargs):
            resource_type = view_instance.get_resource_type()
            return reverse(
                "collections-json-schema", args=[resource_type.slug], request=request,
            )

        def list_context_link_or_func(view_instance, request, *args, **kwargs):
            resource_type = view_instance.get_resource_type()
            return reverse(
                "collections-json-ld", args=[resource_type.slug], request=request,
            )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.resource_type_name = kwargs.get('resource_type_name')
        self.validator = JsonValidator()

    def get_resource_type(self):
        return get_resource_type_for(self.resource_type_name)

    def get_instance(self, request, slug: str):
        resource_type = self.get_resource_type()
        instance = get_object_or_404(
            Resource, resource_type=resource_type, pk=int(slug)
        )
        return instance

    def get_serializer(self, *args, **kwargs):
        kwargs["resource_type"] = self.get_resource_type()
        return super().get_serializer(*args, **kwargs)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = {
            'Location': reverse(
                serializer.instance.resource_type.route_name_detail(),
                args=[serializer.instance.id],
                request=request,
            )
        }
        return Response(
            serializer.data, headers=headers, status=status.HTTP_201_CREATED
        )

    def update(self, request, slug: str):
        """Update a Resource."""
        instance = ResourceRegistration.update(
            content=request.data, register_type=self.resource_type_name, slug=slug,
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
        resource_type = self.get_resource_type()

        queryset = Resource.objects.filter(resource_type=resource_type)
        page = self.paginate_queryset(queryset)

        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return Response(self.get_paginated_response(serializer.data))

        serializer = self.get_serializer(instance=queryset, many=True)
        return Response(serializer.data)

    @property
    def resource_type_iri(self):
        resource_type = self.get_resource_type()
        return resource_type.type_uri

    @property
    def list_type_iri(self):
        return collection_type_uri_for(self.resource_type_iri)


class ResourceCollectionSchemaView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, slug: str):
        resource_type = get_resource_type_for(slug)

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
    permission_classes = [AllowAny]

    def get(self, request, slug: str):
        resource_type = get_resource_type_for(slug)
        return Response(
            {
                "openteams": "https://api.openteams.com/json-ld/",
                "@id": collection_type_uri_for(resource_type.type_uri),
                "http://www.w3.org/2000/01/rdf-schema#subClassOf": {
                    "@id": "https://api.openteams.com/json-ld/ResourceCollection"
                },
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
