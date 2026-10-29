import copy
import hashlib
from collections.abc import Mapping
from contextlib import contextmanager
from urllib.parse import urlencode

import reversion
from django.core.exceptions import FieldError, ImproperlyConfigured, ValidationError
from django.db import DEFAULT_DB_ALIAS, router, transaction
from django.db.models import Max
from django.http import Http404
from rest_framework import status
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.reverse import reverse
from scoped_rbac import access, problems
from scoped_rbac.filters import RbacFilter
from scoped_rbac.permissions import AccessAPIError, drf_error
from scrudful.rest_framework import ScrudfulViewFunc

from scrud_django.exceptions import RbacContextInvalidError, RequestBodyMissingError
from scrud_django.filters import (
    filter_by_object_checks,
    object_check_requirement,
    readable_removal,
)
from scrud_django.headers import get_context_uri_for, get_schema_uri_for
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import Resource
from scrud_django.models.resource import MAX_CONTEXT_BYTES, context_fits
from scrud_django.paginations import StandardResultsSetPagination
from scrud_django.permissions import BodyCheck, resource_check
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
    than its rows hold (`context_fits`: 2048 bytes of UTF-8)."""
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
    if not all(context_fits(c) for c in rbac_contexts_of(data)):
        raise RbacContextInvalidError(
            f"An rbac_context may be at most {MAX_CONTEXT_BYTES} bytes long in UTF-8."
        )


@contextmanager
def read_only_body(request):
    """While the block runs, `request`'s ``data`` (a POST, PUT or PATCH body)
    is a `BodyCheck`, read-only at every level, for a JSON object (a deep
    copy for a JSON array, which scoped-rbac's request check passes on as a
    ``list``): the permission classes and the operators of their grants'
    conditions read the body, and cannot change what is then stored. The
    body is restored after. (DRF's ``Request.data`` reads ``_full_data``.)"""
    if request.method not in BODY_METHODS:
        yield
        return
    data = request.data
    if isinstance(data, Mapping):
        checked = BodyCheck(data)
    elif isinstance(data, list):
        checked = copy.deepcopy(data)
    else:
        yield
        return
    request._full_data = checked
    try:
        yield
    finally:
        request._full_data = data


class ScrudfulMeta:
    """The ``Meta`` scrudful reads (``scrudful_viewset``) for `ResourceViewMixin`
    and `GenericViewMixin` views."""

    # scrudful computes the item validators from the object ``get_object()``
    # returns, before evaluating preconditions: the lookup is scoped and
    # permission-checked first, so an unreadable resource is a 404 and never
    # reveals a validator (304) or its existence (412/428).
    object_validators = True

    # scrudful's own 400, 412 and 428 (a malformed, failed or missing
    # precondition), which it answers before the view runs, are problem
    # details served as application/problem+json, as scoped-rbac's own
    # viewsets answer them, while SCOPED_RBAC["UNIFORM_ERRORS"] is on
    # (UniformErrorsMixin); off, they are scrudful's native bodies.
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
    # same RBAC-filtered queryset (`ListValidatorsMixin.list_queryset`): a
    # write to an object they cannot read changes neither.
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


class UniformErrorsViewFunc(ScrudfulViewFunc):
    """A scrudful handler whose ``error`` entity and ``error_body_func`` (the
    problem details of `ScrudfulMeta`) apply only while
    ``SCOPED_RBAC["UNIFORM_ERRORS"]`` is on for DRF, read per request, as
    scoped-rbac's own viewsets have it. Off, scrudful's 400, 412 and 428 are
    its native ``{"detail": ...}`` as ``application/json``, and the errors the
    view raises are not relabelled as problem details."""

    @property
    def error(self):
        return self._scrud_error if problems.uniform_errors("drf") else None

    @error.setter
    def error(self, value):
        self._scrud_error = value

    @property
    def error_body_func(self):
        if problems.uniform_errors("drf"):
            return self._scrud_error_body_func
        return None

    @error_body_func.setter
    def error_body_func(self, value):
        self._scrud_error_body_func = value


def gate_uniform_errors(cls):
    """Make every scrudful handler of `cls` (and its bases), which
    ``scrudful_viewset`` installed, a `UniformErrorsViewFunc`. Idempotent."""
    for klass in cls.__mro__:
        for value in list(vars(klass).values()):
            if type(value) is ScrudfulViewFunc:
                # Keep the configured values under the properties' names
                # before the class changes, so no request sees neither.
                value._scrud_error = value.__dict__.get("error")
                value._scrud_error_body_func = value.__dict__.get("error_body_func")
                value.__class__ = UniformErrorsViewFunc


class UniformErrorsMixin:
    """Gate the scrudful handlers' problem-details errors on
    ``SCOPED_RBAC["UNIFORM_ERRORS"]`` (`UniformErrorsViewFunc`): ``as_view``,
    which runs after ``scrudful_viewset`` decorated the class, converts
    them."""

    @classmethod
    def as_view(cls, *args, **kwargs):
        gate_uniform_errors(cls)
        return super().as_view(*args, **kwargs)


class LockedWriteMixin:
    """PUT, PATCH and DELETE in one transaction with the row locked: for
    `ResourceViewMixin` and `GenericViewMixin` views.

    `dispatch` runs the whole request in one transaction on
    `locked_write_database`: DRF's permission check, the lookup with the row
    locked (`locked`, which the view applies to the queryset it looks the
    object up in), scrudful's precondition evaluation (If-Match,
    If-Unmodified-Since) against that row, the object check and the write of
    that same instance. A concurrent write of the row waits for it to
    commit, and then evaluates its own preconditions against what it
    committed. A response that is not a success (an error, a 412) rolls the
    transaction back. Other methods are not wrapped. A subclass overriding
    ``dispatch`` must call ``super()``."""

    # The database of the request's locked write transaction (`dispatch`), or
    # ``None`` outside one.
    locked_write_db: str | None = None

    def locked_write_database(self):
        """The database a locked write's transaction opens on."""
        raise NotImplementedError()

    def dispatch(self, request, *args, **kwargs):
        if request.method not in LOCKED_WRITE_METHODS:
            return super().dispatch(request, *args, **kwargs)
        db = self.locked_write_database()
        with transaction.atomic(using=db):
            self.locked_write_db = db
            try:
                response = super().dispatch(request, *args, **kwargs)
            finally:
                self.locked_write_db = None
            if not 200 <= response.status_code < 300:
                transaction.set_rollback(True, using=db)
        return response

    def locked(self, queryset):
        """`queryset`, inside a locked write (`dispatch`) read on its
        database with its rows locked (``select_for_update``) until the
        request's transaction ends."""
        if self.locked_write_db is not None:
            queryset = queryset.using(self.locked_write_db).select_for_update()
        return queryset


class ListValidatorsMixin:
    """A list's ``ETag`` and ``Last-Modified`` computed from what the requester
    may list (`list_queryset`, the RBAC-filtered queryset) and the page they
    get (`list_page`): for `ResourceViewMixin` and `GenericViewMixin` views,
    whose ``Meta`` (`ScrudfulMeta`) gives scrudful `list_etag` and
    `list_last_modified`. A write to an object the requester cannot list
    changes neither. A ``list`` should serve `list_page` (or `list_queryset`
    without a paginator), so that the validators describe what it sends.

    A list (`filter_queryset` of a GET or HEAD outside ``get_object()``) of
    a view whose ``IsAuthorized`` overrides ``has_object_permission`` or
    ``has_context_permission`` keeps only the objects that object check
    allows, as the detail route asks it (`filter_by_object_checks`, given
    `object_for_checks`), so that it shows what the detail route shows."""

    # True while ``get_object()`` runs: its lookup is not a list.
    _scrud_in_get_object = False

    def object_for_checks(self, obj):
        """`obj` as the detail route gives it to the object check: itself
        (`ResourceViewMixin` gives a `ResourceCheck`)."""
        return obj

    def lists_by_object_checks(self):
        """The RBAC requirement a list's objects are each checked against
        (`scrud_django.filters.object_check_requirement`), for a GET or HEAD
        outside ``get_object()``; ``None`` for none."""
        if self._scrud_in_get_object:
            return None
        if self.request.method not in ("GET", "HEAD"):
            return None
        return object_check_requirement(self)

    def filter_queryset(self, queryset):
        """DRF's (each of ``filter_backends`` in turn), except that a list
        whose objects are each checked (`lists_by_object_checks`) is
        narrowed by that check right after the first ``RbacFilter`` (after
        them all without one): before other backends, such as query
        parameter filters, so that the count against
        ``SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]`` (a 400 past it) does not
        depend on them."""
        requirement = self.lists_by_object_checks()
        if requirement is None:
            return super().filter_queryset(queryset)
        narrowed = False
        for backend in list(self.filter_backends):
            queryset = backend().filter_queryset(self.request, queryset, self)
            if (
                not narrowed
                and isinstance(backend, type)
                and issubclass(backend, RbacFilter)
            ):
                queryset = self._narrow_by_object_checks(queryset, requirement)
                narrowed = True
        if not narrowed:
            queryset = self._narrow_by_object_checks(queryset, requirement)
        return queryset

    def _narrow_by_object_checks(self, queryset, requirement):
        try:
            return filter_by_object_checks(
                queryset, self.request, self, requirement, self.object_for_checks
            )
        except access.ConditionalListTooLarge as exc:
            raise AccessAPIError(exc) from exc

    def list_queryset(self):
        """The list's objects: ``filter_queryset(get_queryset())``, so that
        the RBAC filter (an ``RbacFilter``, for resources
        `scrud_django.filters.ResourceRbacFilter`) and the other filter
        backends run, built once per request (the validators and the page
        share it). A view without an ``RbacFilter`` among its
        ``filter_backends`` is refused (``ImproperlyConfigured``) rather than
        listing every object."""
        queryset = getattr(self, "_scrud_list_queryset", None)
        if queryset is None:
            if not any(
                isinstance(backend, type) and issubclass(backend, RbacFilter)
                for backend in self.filter_backends
            ):
                raise ImproperlyConfigured(
                    f"{type(self).__name__} lists without an RBAC filter: put "
                    "scoped_rbac.filters.RbacFilter (for resources, "
                    "scrud_django.filters.ResourceRbacFilter) in its "
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
        objects (key and ETag, in page order), the list's count, the query
        parameters and the resource type's link targets. Nothing else changes
        it."""
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
        object the requester may list (``None`` for none). A deletion does not
        change it (the ETag does)."""
        return self.list_queryset().aggregate(newest=Max("modified_at"))["newest"]


class ResourceViewMixin(UniformErrorsMixin, LockedWriteMixin, ListValidatorsMixin):
    queryset = Resource.objects.all()
    serializer_class = ResourceSerializer
    pagination_class = StandardResultsSetPagination
    # The detail route's URL keyword; it names the primary key.
    lookup_url_kwarg = "slug"

    # scrud variable
    resource_type_name: str | None = None

    class Meta(ScrudfulMeta):
        pass

    def locked_write_database(self):
        """The database of a locked write: `write_db`, where resources are
        written."""
        return write_db()

    def initial(self, request, *args, **kwargs):
        """Forget the resource types `get_resource_type` read (a view
        instance serves one request; this keeps a reused one from serving a
        stale type), DRF's, then, inside a locked write (`dispatch`), the
        ``WorkflowPolicy`` type lock (`lock_if_workflow_policy`) before the
        row is read: every ``WorkflowPolicy`` write locks the type row and then
        the policy's row, in that order, so two cannot deadlock."""
        self._scrud_resource_types = {}
        super().initial(request, *args, **kwargs)
        if self.locked_write_db is not None:
            lock_if_workflow_policy(self.get_resource_type(), self.locked_write_db)

    def get_resource_type(self):
        """The view's resource type, read once per request and database (the
        filter, the serializer, the validators, the Links and the write all
        ask for it). Inside a locked write it is read on the type's write
        database (`type_db`), not through the read router."""
        using = type_db() if self.locked_write_db is not None else None
        read = self.__dict__.setdefault("_scrud_resource_types", {})
        if using not in read:
            read[using] = get_resource_type_for(self.resource_type_name, using=using)
        return read[using]

    def get_queryset(self):
        """The type's resources. Inside a locked write (`dispatch`) they are
        read on the write database with their rows locked
        (``select_for_update``) until the request's transaction ends."""
        return self.locked(
            Resource.objects.filter(resource_type=self.get_resource_type())
        )

    def get_object(self):
        """DRF's ``get_object()`` (the filtered queryset, the lookup by
        ``lookup_field``, a 404 if missing), except that the object check is
        given the resource as a `ResourceCheck`, a read-only mapping over its
        content: ``check_object_permissions``, every ``has_object_permission``
        and ``has_context_permission`` it calls, and the readability check of
        a denied resource (404 or 403), so that an operator reads the content
        as on the request check. The `Resource` is returned."""
        previous, self._scrud_in_get_object = self._scrud_in_get_object, True
        try:
            queryset = self.filter_queryset(self.get_queryset())
        finally:
            self._scrud_in_get_object = previous
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        obj = get_object_or_404(
            queryset, **{self.lookup_field: self.kwargs[lookup_url_kwarg]}
        )
        resource_type = self.get_resource_type()
        if obj.resource_type_id == resource_type.pk:
            # The type the view read (on the same database), not read again.
            Resource.resource_type.field.set_cached_value(obj, resource_type)
        self.check_object_permissions(self.request, self.object_for_checks(obj))
        return obj

    def object_for_checks(self, obj):
        """`obj` as object checks get it: a `ResourceCheck`."""
        return resource_check(obj)

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

    def get_rbac_permissions(self):
        """Every ``IsAuthorized`` (or subclass) of ``get_permissions()``,
        nested ones (DRF's ``&``, ``|``, ``~``) included: the permissions the
        workflow check (``WorkflowValidator``) authorizes a transition
        through."""
        return self._rbac_permissions()

    def get_post_save_permissions(self):
        """The permissions `check_post_save_permissions` applies to a saved
        resource: as scoped-rbac's ``AccessControlledModelViewSet``, every
        ``IsAuthorized`` of ``get_permissions()``, nested ones included
        (`get_rbac_permissions`)."""
        return self.get_rbac_permissions()

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
        everyone): they depend on the body alone. The request check then
        reads the body as a `BodyCheck` (`read_only_body`)."""
        check_body_present(request, self)
        check_written_contexts(request)
        with read_only_body(request):
            super().check_permissions(request)

    def list_last_modified(self):
        """The list's Last-Modified: the newest ``modified_at`` of every
        resource the requester may list, or the newest time a resource left a
        context in which they may list every resource
        (`scrud_django.filters.readable_removal`: deleted, or moved out),
        whichever is later; ``None`` for neither. A list that lost a resource
        they could list is therefore modified; a removal in a context they
        read only by a condition, or not at all, does not move it (the ETag
        follows what they see)."""
        newest = super().list_last_modified()
        removed = readable_removal(
            self.request,
            self,
            self.get_resource_type(),
            using=self.list_queryset().db,
        )
        return max((t for t in (newest, removed) if t is not None), default=None)

    def list(self, request):
        """The resources of the type the requester may read, a page at a time."""
        page = self.list_page()

        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return Response(self.get_paginated_response(serializer.data))

        serializer = self.get_serializer(instance=self.list_queryset(), many=True)
        return Response(serializer.data)


class GenericViewMixin(UniformErrorsMixin, LockedWriteMixin, ListValidatorsMixin):
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

    As for ``ResourceViewMixin``:

    - a PUT, PATCH or DELETE runs in one transaction with the row locked
      (`LockedWriteMixin`): the transaction opens on
      `locked_write_database` (where the ``queryset``'s model is written) and
      `get_object` locks the row the URL names (`lock_object_row`, a plain
      query, whatever the view's queryset is) before it looks it up, so
      scrudful's preconditions, the object check and your
      ``update``/``destroy`` see one row, and a response that is not a
      success rolls the write back;
    - the list's ``ETag`` and ``Last-Modified`` describe what the requester
      may list (`ListValidatorsMixin`): an ``RbacFilter`` must be among the
      ``filter_backends``, and ``list`` should serve `list_page` (or
      `list_queryset`). The model needs ``etag`` and ``modified_at``. A
      deletion changes the ``ETag``, not ``Last-Modified``;
    - scrudful's 400, 412 and 428 are problem details.
    """

    # The detail route's URL keyword, as the detail stubs below take it.
    lookup_url_kwarg = "slug"

    # scrud variable
    resource_type_name: str | None = None

    class Meta(ScrudfulMeta):
        pass

    def locked_write_database(self):
        """The database of a locked write: where the view's model is written
        (``router.db_for_write`` of its ``queryset``'s model). A view without a
        ``queryset`` attribute (one that only overrides ``get_queryset()``)
        writes on the default database unless it overrides this."""
        queryset = getattr(self, "queryset", None)
        if queryset is None:
            return DEFAULT_DB_ALIAS
        return router.db_for_write(queryset.model)

    def filter_queryset(self, queryset):
        """DRF's, inside a locked write (`dispatch`) read on its database (the
        row itself is locked by `lock_object_row`, not through this
        queryset)."""
        queryset = super().filter_queryset(queryset)
        if self.locked_write_db is not None:
            queryset = queryset.using(self.locked_write_db)
        return queryset

    def get_object(self):
        """DRF's ``get_object()``; inside a locked write (`dispatch`) the row
        is locked first (`lock_object_row`), so that the lookup, scrudful's
        preconditions, the object check and the write read the row as the
        last writer committed it. A lookup that finds a row the lock did not
        take (one that came to match the URL after the key was read) is a
        404."""
        previous, self._scrud_in_get_object = self._scrud_in_get_object, True
        try:
            if self.locked_write_db is None:
                return super().get_object()
            locked = self.lock_object_row()
            obj = super().get_object()
            if locked is not None and obj.pk not in locked:
                raise Http404
            return obj
        finally:
            self._scrud_in_get_object = previous

    def lock_object_row(self):
        """Lock (``select_for_update``) the rows of the view's model that the
        URL's lookup names, on the locked write's database, with a plain
        query of the model's base manager: PostgreSQL refuses
        ``FOR UPDATE`` on a queryset with ``DISTINCT``, ``GROUP BY`` or the
        nullable side of an outer join, which the view's own queryset may
        have. A ``lookup_field`` the base manager cannot filter by (an
        annotation of the view's queryset) is first resolved to keys through
        the view's filtered queryset, unlocked, and those keys are locked.
        Returns the set of locked keys, or ``None`` when the lookup value is
        one the field cannot take (nothing is locked; the lookup then answers
        404). The lookup that follows reads the row after the lock is held. A
        row the requester may not read is locked for the length of their
        request, as for resources."""
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        lookup = {self.lookup_field: self.kwargs[lookup_url_kwarg]}
        queryset = self.get_queryset()
        rows = queryset.model._base_manager.using(self.locked_write_db)
        try:
            try:
                rows = rows.filter(**lookup)
            except FieldError:
                keys = (
                    self.filter_queryset(queryset)
                    .filter(**lookup)
                    .values_list("pk", flat=True)
                )
                rows = rows.filter(pk__in=list(keys))
            return set(rows.select_for_update().values_list("pk", flat=True))
        except (TypeError, ValueError, ValidationError):
            return None

    def get_resource_type(self):
        """The view's resource type (``resource_type_name``), read once per
        view instance (one request)."""
        read = self.__dict__.setdefault("_scrud_resource_types", {})
        if None not in read:
            read[None] = get_resource_type_for(self.resource_type_name)
        return read[None]

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
        """Delete a Resource."""
        raise NotImplementedError()

    def retrieve(self, request, slug: str):
        """Return the resource for the given resource type name and slug."""
        raise NotImplementedError()

    def list(self, request):
        """Return the list of resources for the given resource type name."""
        raise NotImplementedError()
