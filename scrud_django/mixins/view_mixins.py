import hashlib
from collections.abc import Mapping
from urllib.parse import urlencode

import reversion
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.db.models import Max
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.reverse import reverse
from scoped_rbac import access, problems
from scoped_rbac.filters import RbacFilter
from scoped_rbac.permissions import drf_error

from scrud_django.exceptions import RbacContextInvalidError, RequestBodyMissingError
from scrud_django.headers import get_context_uri_for, get_schema_uri_for
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import Resource
from scrud_django.models.resource import MAX_CONTEXT_LENGTH
from scrud_django.paginations import StandardResultsSetPagination
from scrud_django.permissions import resource_check
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    lock_if_workflow_policy,
    type_db,
    write_db,
)
from scrud_django.serializers import ResourceSerializer
from scrud_django.validators import check_policy_is_unique
from scrud_django.workflow import (
    WORKFLOW_POLICY_TYPE_URI,
    lock_workflow_policy_type,
)

# The methods whose lookup, authorization, preconditions, workflow check and
# write run in one transaction with the row locked (`ResourceViewMixin.dispatch`).
LOCKED_WRITE_METHODS = frozenset({"PUT", "PATCH", "DELETE"})


# The methods whose body is the resource to store.
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


def check_body_present(request, view=None):
    """Raise `RequestBodyMissingError` (400) for a POST, PUT or PATCH that
    `view` serves (one it does not is left to its 405) whose body DRF does not
    read: an empty one, or one without a ``Content-Length`` (0 or absent, as
    for a chunked body). DRF reads it as ``{}``: a POST would store an empty
    resource, a PUT wipe the stored content. A body that is not JSON is left
    to the parser (415, or 400)."""
    if request.method not in BODY_METHODS:
        return
    if view is not None and request.method not in view.allowed_methods:
        return
    if request.stream is None:
        raise RequestBodyMissingError()


def check_written_contexts(request):
    """Raise `RbacContextInvalidError` (400) when a POST, PUT or PATCH body's
    ``rbac_context`` is an empty list, which would store the resource in no
    context (only a superuser could read it; a server-side
    ``ResourceRegistration`` may still store one), or names a context longer
    than a ``ResourceContext`` row holds."""
    if request.method not in ("POST", "PUT", "PATCH"):
        return
    data = request.data
    if not isinstance(data, Mapping):
        return
    context = data.get("rbac_context")
    if isinstance(context, (list, tuple)) and not context:
        raise RbacContextInvalidError(
            "rbac_context must name at least one context; leave it out (or give "
            '"") for the default context.'
        )
    if any(len(c) > MAX_CONTEXT_LENGTH for c in rbac_contexts_of(data)):
        raise RbacContextInvalidError(
            f"An rbac_context may be at most {MAX_CONTEXT_LENGTH} characters long."
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

        # scrudful's own 400, 412 and 428 (a malformed, failed or missing
        # precondition), which it answers before the view runs, are problem
        # details served as application/problem+json, as scoped-rbac's own
        # viewsets answer them: the other errors are problem details too.
        error = problems.problem_entity()
        error_body_func = problems.conditional_problem
        list_error = problems.problem_entity()
        list_error_body_func = problems.conditional_problem

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

    # The database of the request's locked write transaction (`dispatch`), or
    # ``None`` outside one.
    locked_write_db: str | None = None

    def dispatch(self, request, *args, **kwargs):
        """For PUT, PATCH and DELETE, run the whole request in one transaction
        on `write_db`: DRF's permission check, the ``WorkflowPolicy`` type lock
        (`initial`), the lookup with the row locked (`get_queryset`),
        scrudful's precondition evaluation (If-Match, If-Unmodified-Since)
        against that row, the workflow check and the write of that same
        instance. A concurrent write of the row waits for it to commit, and
        then evaluates its own preconditions and transition against what it
        committed. A response that is not a success (an error, a 412)
        rolls the transaction back. Other methods are not wrapped."""
        if request.method not in LOCKED_WRITE_METHODS:
            return super().dispatch(request, *args, **kwargs)
        db = write_db()
        with transaction.atomic(using=db):
            self.locked_write_db = db
            try:
                response = super().dispatch(request, *args, **kwargs)
            finally:
                self.locked_write_db = None
            if not 200 <= response.status_code < 300:
                transaction.set_rollback(True, using=db)
        return response

    def initial(self, request, *args, **kwargs):
        """DRF's, then, inside a locked write (`dispatch`), the
        ``WorkflowPolicy`` type lock (`lock_if_workflow_policy`) before the
        row is read: every ``WorkflowPolicy`` write locks the type row and then
        the policy's row, in that order, so two cannot deadlock."""
        super().initial(request, *args, **kwargs)
        if self.locked_write_db is not None:
            lock_if_workflow_policy(self.get_resource_type(), self.locked_write_db)

    def get_resource_type(self):
        """The view's resource type. Inside a locked write it is read on the
        type's write database (`type_db`), not through the read router."""
        using = type_db() if self.locked_write_db is not None else None
        return get_resource_type_for(self.resource_type_name, using=using)

    def get_queryset(self):
        """The type's resources. Inside a locked write (`dispatch`) they are
        read on the write database with their rows locked
        (``select_for_update``) until the request's transaction ends."""
        queryset = Resource.objects.filter(resource_type=self.get_resource_type())
        if self.locked_write_db is not None:
            queryset = queryset.using(self.locked_write_db).select_for_update()
        return queryset

    def get_object(self):
        """DRF's ``get_object()`` (the filtered queryset, the lookup by
        ``lookup_field``, a 404 if missing), except that the object check is
        given the resource as a `ResourceCheck`, a read-only mapping over its
        content: ``check_object_permissions``, every ``has_object_permission``
        and ``has_context_permission`` it calls, and the readability check of
        a denied resource (404 or 403), so that an operator reads the content
        as on the request check. The `Resource` is returned."""
        queryset = self.filter_queryset(self.get_queryset())
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        obj = get_object_or_404(
            queryset, **{self.lookup_field: self.kwargs[lookup_url_kwarg]}
        )
        self.check_object_permissions(self.request, resource_check(obj))
        return obj

    def get_instance(self, request, slug: str):
        """The resource named by the URL: DRF's ``get_object()``, filtered and
        permission-checked (an unreadable resource is a 404). scrudful already
        looked it up for the validators; the same instance is returned. For a
        PUT or DELETE its row is locked (`dispatch`)."""
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
        `obj` is in, for each post-save permission. ``rbac_contexts`` is any-of
        for reads: a write may land the resource only where the writer may
        write, in each of those contexts.

        It asks each permission itself, so that an ``IsAuthorized`` subclass's
        overrides apply, giving it the saved resource as a `ResourceCheck`, as
        the detail route's object check does: its ``has_object_permission``
        must allow it (as scoped-rbac's own post-save check asks; superusers
        only for a resource in no usable context), and its
        ``has_context_permission`` must allow it in each context its
        ``contexts_for`` names."""
        check = resource_check(obj)
        for leaf in self.get_post_save_permissions():
            contexts = access.usable_contexts(leaf.contexts_for(check))
            allowed = leaf.has_object_permission(request, self, check) and all(
                leaf.has_context_permission(request, self, check, context)
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
        # The request's locked write transaction, if any (`dispatch`); else
        # the un-hinted write alias, as there (instance-hint routing of
        # scrud_django's models is not supported for the API).
        db = self.locked_write_db or write_db()
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
        """Update a Resource: the locked instance (`dispatch`), checked and
        written in the request's transaction."""
        instance = self.get_instance(request, slug)
        self.check_every_context_of_policy(request, instance)
        serializer = self.get_serializer(
            data=request.data, instance=instance, many=False
        )
        serializer.is_valid(raise_exception=True)
        db = self.locked_write_db or write_db()
        instance = self.save_checked(
            lambda: ResourceRegistration.update_locked(
                instance,
                request.data,
                resource_type=self.get_resource_type(),
                using=db,
            ),
            instance=instance,
        )
        serializer = self.get_serializer(instance=instance, many=False)
        return Response(serializer.data)

    def destroy(self, request, slug: str):
        """Delete a Resource: the locked instance (`dispatch`)."""
        instance = self.get_instance(request, slug)
        self.check_every_context_of_policy(request, instance)
        ResourceRegistration.delete_locked(
            instance,
            resource_type=self.get_resource_type(),
            using=self.locked_write_db or write_db(),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    def retrieve(self, request, slug: str):
        """Return the resource for the given resource type name."""
        instance = self.get_instance(request, slug)
        serializer = self.get_serializer(instance=instance, many=False)
        return Response(serializer.data)

    def check_permissions(self, request):
        """Refuse an empty body (`check_body_present`) and a body whose
        ``rbac_context`` cannot be stored (`check_written_contexts`), with
        400, before scoped-rbac's request check (which would authorize an
        empty body as ``{}`` and refuse an empty context list with 403 for
        everyone): they depend on the body alone."""
        check_body_present(request, self)
        check_written_contexts(request)
        super().check_permissions(request)

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

        # scrudful's own 400, 412 and 428 (a malformed, failed or missing
        # precondition), which it answers before the view runs, are problem
        # details served as application/problem+json, as scoped-rbac's own
        # viewsets answer them: the other errors are problem details too.
        error = problems.problem_entity()
        error_body_func = problems.conditional_problem
        list_error = problems.problem_entity()
        list_error_body_func = problems.conditional_problem

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
