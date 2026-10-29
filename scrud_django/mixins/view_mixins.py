import hashlib
from urllib.parse import urlencode

import reversion
from django.core.exceptions import ImproperlyConfigured
from django.db import router
from django.db.models import Max
from rest_framework import status
from rest_framework.response import Response
from rest_framework.reverse import reverse
from scoped_rbac import access
from scoped_rbac.filters import RbacFilter
from scoped_rbac.permissions import drf_error, view_permission

from scrud_django.headers import get_context_uri_for, get_schema_uri_for
from scrud_django.models import Resource
from scrud_django.paginations import StandardResultsSetPagination
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
)
from scrud_django.serializers import ResourceSerializer
from scrud_django.validators import check_policy_is_unique
from scrud_django.workflow import (
    WORKFLOW_POLICY_TYPE_URI,
    lock_workflow_policy_type,
)


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

        # The list's validators describe the page the requester gets, from the
        # same RBAC-filtered queryset (`list_queryset`): a write to a resource
        # they cannot read changes neither.
        def list_etag_func(view_instance, request, **kwargs):
            return view_instance.list_etag(request)

        def list_last_modified_func(view_instance, request, **kwargs):
            return view_instance.list_last_modified()

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

    def check_every_context_of_policy(self, request, instance):
        """For a ``WorkflowPolicy``, deny (403) an update or delete unless the
        request's action is allowed in **every** context the stored policy
        applies to: it governs each of them. The object check is any-of; a
        policy written through the API has one context, but one stored with a
        list (before 0.12.0, or server-side) has several."""
        if instance.resource_type.type_uri != WORKFLOW_POLICY_TYPE_URI:
            return
        try:
            self.check_post_save_permissions(request, instance)
        except access.AccessError as exc:
            raise drf_error(exc, request) from exc

    def serialized_policy_save(self, save, db):
        """`save` for a ``WorkflowPolicy``: inside the write's transaction,
        take `lock_workflow_policy_type`, save, and re-check that the saved
        policy is the only one for its type in its contexts
        (`check_policy_is_unique`, 400 and rolled back otherwise). The
        validator's check ran before the lock; a concurrent write of the same
        type and context committed since is seen here."""

        def locked_save():
            lock_workflow_policy_type(db)
            saved = save()
            check_policy_is_unique(saved.content, saved.pk)
            return saved

        return locked_save

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
        if self.get_resource_type().type_uri == WORKFLOW_POLICY_TYPE_URI:
            save = self.serialized_policy_save(save, db)
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
        self.check_every_context_of_policy(request, instance)
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
        instance = self.get_instance(request, slug)
        self.check_every_context_of_policy(request, instance)
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

    def list_queryset(self):
        """The list's resources: ``filter_queryset(get_queryset())``, so that
        the RBAC filter (`scrud_django.filters.ResourceRbacFilter`) and the
        other filter backends run, built once per request (the validators and
        the page share it). A view without an ``RbacFilter`` among its
        ``filter_backends`` is refused (``ImproperlyConfigured``) rather than
        listing every resource."""
        queryset = getattr(self, "_scrud_list_queryset", None)
        if queryset is None:
            if not any(
                isinstance(backend, type) and issubclass(backend, RbacFilter)
                for backend in self.filter_backends
            ):
                raise ImproperlyConfigured(
                    f"{type(self).__name__} lists resources without an RBAC filter: "
                    "put scrud_django.filters.ResourceRbacFilter in its "
                    "filter_backends."
                )
            queryset = self.filter_queryset(self.get_queryset())
            self._scrud_list_queryset = queryset
        return queryset

    def list_page(self):
        """The page of `list_queryset` the request asks for (``None`` without a
        paginator), computed once per request."""
        if not hasattr(self, "_scrud_list_page"):
            self._scrud_list_page = self.paginate_queryset(self.list_queryset())
        return self._scrud_list_page

    def list_etag(self, request):
        """The list's ETag: a hash of what the requester gets, the page's
        resources (key and ETag, in page order), the list's count, the query
        parameters and the type's link targets. Nothing else changes it."""
        page = self.list_page()
        if page is None:
            items = list(self.list_queryset())
            count = len(items)
        else:
            items = page
            count = self.paginator.page.paginator.count
        resource_type = self.get_resource_type()
        parts = [
            resource_type.pk,
            resource_type.revision,
            resource_type.schema_id,
            resource_type.schema_uri,
            resource_type.context_id,
            resource_type.context_uri,
            count,
            urlencode(sorted(request.query_params.lists()), doseq=True),
            *(f"{obj.pk}:{obj.etag}" for obj in items),
        ]
        digest = hashlib.sha256("\n".join(map(str, parts)).encode("utf-8"))
        return digest.hexdigest()

    def list_last_modified(self):
        """The list's Last-Modified: the newest ``modified_at`` of every
        resource the requester may list (``None`` for none). A deletion does
        not change it (use the ETag)."""
        return self.list_queryset().aggregate(newest=Max("modified_at"))["newest"]

    def list(self, request):
        """The resources of the type the requester may read, a page at a time."""
        page = self.list_page()

        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return Response(self.get_paginated_response(serializer.data))

        serializer = self.get_serializer(instance=self.list_queryset(), many=True)
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
    every context the saved object is in is one the request may write in, as
    scoped-rbac's ``AccessControlledModelViewSet.perform_create``/
    ``perform_update`` do. (``ResourceViewMixin.save_checked`` does the same,
    but only for ``Resource``.)
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
