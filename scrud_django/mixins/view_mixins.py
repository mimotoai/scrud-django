import reversion
from django.db import router
from rest_framework import status
from rest_framework.response import Response
from rest_framework.reverse import reverse
from scoped_rbac import access
from scoped_rbac.permissions import drf_error, view_permission

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

    def get_post_save_permissions(self):
        """The permissions `check_post_save_permissions` applies to a saved
        resource: as scoped-rbac's ``AccessControlledModelViewSet``, every
        ``IsAuthorized`` of ``get_permissions()``, nested ones included."""
        return self._rbac_permissions()

    def check_post_save_permissions(self, request, obj):
        """Deny (``access.AccessDenied``) unless the request's action (POST for
        a create, PUT for an update) is allowed in **every** context the saved
        `obj` is in, for each post-save permission, with its stored content as
        the resource a condition reads. ``rbac_contexts`` is any-of for reads:
        a write may land the resource only where the writer may write, in each
        of those contexts. A resource with no usable context is left to
        superusers, as scoped-rbac's object checks leave it."""
        permission = view_permission(request, self)
        resource = access.body_mapping(obj.content)
        for leaf in self.get_post_save_permissions():
            contexts = access.usable_contexts(leaf.contexts_for(obj))
            if not contexts:
                allowed = access.may_act_on(request, permission, obj, [])
            else:
                allowed = all(
                    access.may_act_in(request, permission, context, resource)
                    for context in contexts
                )
            if not allowed:
                raise access.AccessDenied()

    def save_checked(self, save, instance=None):
        """Run `save` (which returns the saved resource) in a transaction and
        check the resource as stored before it commits: a reserved context is a
        400 and `check_post_save_permissions` a 403, either rolling the write
        back. The request was authorized by the context its body names; this
        checks where the resource actually landed (scoped-rbac's ``SaveCheck``).
        `instance` is the resource an update replaces (``None`` for a create).
        Returns the saved resource as read back."""
        check = access.SaveCheck(
            self.request,
            written_contexts=lambda obj: obj.rbac_contexts,
            recheck=lambda obj, _moved_to: self.check_post_save_permissions(
                self.request, obj
            ),
            queryset=self.get_queryset(),
        )
        db = router.db_for_write(Resource)
        try:
            if instance is None:
                return check.create(save, db=db)
            return check.update(instance, save, db=db)
        except access.AccessError as exc:
            raise drf_error(exc, self.request) from exc

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.save_checked(lambda: self.perform_create(serializer))
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
        instance = self.save_checked(
            lambda: ResourceRegistration.update(
                content=request.data,
                register_type=self.resource_type_name,
                slug=slug,
            ),
            instance=instance,
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

    Subclasses provide DRF's ``get_queryset()``, not a ``get_instance``:
    ``Meta.object_validators`` makes scrudful read the item validators from the
    object ``get_object()`` returns, so that lookup must be scoped and
    permission-checked (precede ``GenericAPIView`` with scoped-rbac's
    ``AccessControlledAPIView``). The detail route's URL keyword is ``slug``, as
    the ``update``/``destroy``/``retrieve`` stubs take it (``lookup_url_kwarg``);
    it is looked up by ``lookup_field`` (DRF's default ``pk``), which a subclass
    may override.

    Set ``parser_classes = [RbacJSONParser]`` (``scoped_rbac.rest``), as the
    scrud viewsets do: a form or multipart body can name ``rbac_context`` more
    than once, and scoped-rbac authorizes the request by its last value only.
    A ``create``/``update`` should also check, before the write commits, that
    every context the saved object is in is one the request may write in (as
    ``ResourceViewMixin.save_checked`` does, or scoped-rbac's
    ``AccessControlledModelViewSet.perform_create``/``perform_update``).
    """

    # The detail route's URL keyword, as the detail stubs below take it.
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
