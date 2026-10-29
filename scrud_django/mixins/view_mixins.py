import reversion
from rest_framework import status
from rest_framework.response import Response
from rest_framework.reverse import reverse

from scrud_django.headers import get_context_uri_for, get_schema_uri_for
from scrud_django.models import Resource
from scrud_django.paginations import StandardResultsSetPagination
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
)
from scrud_django.serializers import ResourceSerializer


class ResourceViewMixin:
    queryset = Resource.objects.all()
    serializer_class = ResourceSerializer
    pagination_class = StandardResultsSetPagination
    # The detail route's URL keyword; it names the primary key.
    lookup_url_kwarg = "slug"

    # scrud variable
    resource_type_name: str | None = None

    class Meta:
        # scrudful computes the item validators from the object ``get_object()``
        # returns, before evaluating preconditions: the lookup is scoped and
        # permission-checked first, so an unreadable resource is a 404 and never
        # reveals a validator (304) or its existence (412/428).
        object_validators = True

        def etag_func(obj):
            return obj.etag

        def last_modified_func(obj):
            return obj.modified_at

        # Link callbacks also run for create and the list route's OPTIONS, where
        # there is no slug: they take ``**kwargs``.
        def schema_link_or_func(view_instance, request, **kwargs):
            if view_instance.resource_type_name:
                resource_type = view_instance.get_resource_type()
                return get_schema_uri_for(resource_type, request)
            return None

        def context_link_or_func(view_instance, request, **kwargs):
            if view_instance.resource_type_name:
                resource_type = view_instance.get_resource_type()
                return get_context_uri_for(resource_type, request)
            return None

        def list_etag_func(view_instance, request, **kwargs):
            resource_type = view_instance.get_resource_type()
            return resource_type.etag

        def list_last_modified_func(view_instance, request, **kwargs):
            resource_type = view_instance.get_resource_type()
            return resource_type.modified_at

        def list_schema_link_or_func(view_instance, request, **kwargs):
            resource_type = view_instance.get_resource_type()
            return reverse(
                "collections-json-schema",
                args=[resource_type.slug],
                request=request,
            )

        def list_context_link_or_func(view_instance, request, **kwargs):
            resource_type = view_instance.get_resource_type()
            return reverse(
                "collections-json-ld",
                args=[resource_type.slug],
                request=request,
            )

    def get_resource_type(self):
        return get_resource_type_for(self.resource_type_name)

    def get_queryset(self):
        return Resource.objects.filter(resource_type=self.get_resource_type())

    def get_instance(self, request, slug: str):
        """The resource named by the URL: DRF's ``get_object()``, filtered and
        permission-checked (an unreadable resource is a 404). scrudful already
        looked it up for the validators; the same instance is returned."""
        return self.get_object()

    def get_serializer(self, *args, **kwargs):
        kwargs["resource_type"] = self.get_resource_type()
        return super().get_serializer(*args, **kwargs)

    def perform_create(self, serializer):
        if self.get_resource_type().uses_reversion:
            with reversion.create_revision():
                response = serializer.save()
                reversion.set_comment('Add Resource')
            return response
        return serializer.save()

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
        instance = self.get_instance(request, slug)
        serializer = self.get_serializer(
            data=request.data, instance=instance, many=False
        )
        serializer.is_valid(raise_exception=True)
        instance = ResourceRegistration.update(
            content=request.data,
            register_type=self.resource_type_name,
            slug=slug,
        )
        serializer = self.get_serializer(instance=instance, many=False)
        return Response(serializer.data)

    def destroy(self, request, slug: str):
        """Delete a Resource."""
        self.get_instance(request, slug)
        ResourceRegistration.delete(
            register_type=self.resource_type_name,
            slug=slug,
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


class GenericViewMixin:
    """SCRUD views over a model of your own (decorate with ``scrudful_viewset``).

    Subclasses provide DRF's ``get_queryset()`` (and ``lookup_field`` /
    ``lookup_url_kwarg``), not a ``get_instance``: ``Meta.object_validators``
    makes scrudful read the item validators from the object ``get_object()``
    returns, so that lookup must be scoped and permission-checked (precede
    ``GenericAPIView`` with scoped-rbac's ``AccessControlledAPIView``).
    """

    # scrud variable
    resource_type_name: str | None = None

    class Meta:
        # scrudful computes the item validators from the object ``get_object()``
        # returns, before evaluating preconditions: the lookup is scoped and
        # permission-checked first, so an unreadable resource is a 404 and never
        # reveals a validator (304) or its existence (412/428).
        object_validators = True

        def etag_func(obj):
            return obj.etag

        def last_modified_func(obj):
            return obj.modified_at

        # Link callbacks also run for create and the list route's OPTIONS, where
        # there is no slug: they take ``**kwargs``.
        def schema_link_or_func(view_instance, request, **kwargs):
            if view_instance.resource_type_name:
                resource_type = view_instance.get_resource_type()
                return get_schema_uri_for(resource_type, request)
            return None

        def context_link_or_func(view_instance, request, **kwargs):
            if view_instance.resource_type_name:
                resource_type = view_instance.get_resource_type()
                return get_context_uri_for(resource_type, request)
            return None

        def list_etag_func(view_instance, request, **kwargs):
            resource_type = view_instance.get_resource_type()
            return resource_type.etag

        def list_last_modified_func(view_instance, request, **kwargs):
            resource_type = view_instance.get_resource_type()
            return resource_type.modified_at

        def list_schema_link_or_func(view_instance, request, **kwargs):
            resource_type = view_instance.get_resource_type()
            return reverse(
                "collections-json-schema",
                args=[resource_type.slug],
                request=request,
            )

        def list_context_link_or_func(view_instance, request, **kwargs):
            resource_type = view_instance.get_resource_type()
            return reverse(
                "collections-json-ld",
                args=[resource_type.slug],
                request=request,
            )

    def get_resource_type(self):
        return get_resource_type_for(self.resource_type_name)

    def get_serializer(self, *args, **kwargs):
        kwargs["resource_type"] = self.get_resource_type()
        return super().get_serializer(*args, **kwargs)

    def create(self, request):
        """Add a Resource."""
        raise NotImplementedError()

    def update(self, request, slug: str):
        """Update a Resource."""
        raise NotImplementedError()

    def destroy(self, request, slug: str):
        """Update a Resource."""
        raise NotImplementedError()

    def retrieve(self, request, slug: str):
        """Return the resource for the given resource type name and slug."""
        raise NotImplementedError()

    def list(self, request):
        """Return the list of resources for the given resource type name."""
        raise NotImplementedError()
