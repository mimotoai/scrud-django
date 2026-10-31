"""List filters of the scrud viewsets.

`ResourceRbacFilter` restricts a list of resources to those the requester may
read, as the detail route decides it, using the `ResourceContext` rows to find
them in SQL. `ResourceContentFilter` filters a list on query parameters.
"""

import re
from contextlib import contextmanager

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db.models import (
    BooleanField,
    Case,
    Exists,
    Max,
    OuterRef,
    Q,
    Value,
    When,
)
from django.db.models.fields.json import KeyTransform
from django.http import Http404
from rest_framework.exceptions import (
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from rest_framework.filters import BaseFilterBackend
from rest_framework.request import clone_request
from rest_framework.settings import api_settings
from scoped_rbac import access
from scoped_rbac.conf import conditional_list_max_rows
from scoped_rbac.filters import RbacFilter
from scoped_rbac.permissions import (
    AccessAPIError,
    IsAuthorized,
    http_action_iri_for,
    policy_for,
)
from scoped_rbac.policy import Permission
from scoped_rbac.rbac_contexts import SOME_CONTEXT

from scrud_django.models import Resource, ResourceContext, ResourceRemoval
from scrud_django.permissions import checks_read_content, content_of, reading_content

# What an object check may raise to refuse one object (DRF's and Django's
# idioms): a list leaves that object out, as its detail route refuses it.
REFUSALS = (
    PermissionDenied,
    NotAuthenticated,
    NotFound,
    Http404,
    DjangoPermissionDenied,
)

# Rows fetched per chunk while conditional candidates are evaluated one by one,
# as scoped-rbac's ``access._CANDIDATE_CHUNK_SIZE``.
CANDIDATE_CHUNK_SIZE = 2000


def _rows(*conditions, **filters):
    """``Exists`` of a `ResourceContext` row of the outer resource matching
    `conditions` and `filters`."""
    return Exists(
        ResourceContext.objects.filter(*conditions, resource=OuterRef("pk"), **filters)
    )


def _plainly_reserved():
    """Contexts reserved in their plain form (``__``-prefixed, or ``*``), the
    form SQL can match, as ``scoped_rbac.access.exclude_reserved`` matches
    them. A context reserved only once normalised (e.g. ``"\\u200b__x"``) is
    matched by `access.is_reserved_context` in Python only; the API refuses to
    store one."""
    return Q(rbac_context__startswith="__") | Q(rbac_context="*")


def _usable(contexts):
    """`contexts` (a policy's, from ``list_scope``) without reserved ones: an
    object's reserved context grants nothing (``access.usable_contexts``)."""
    return {c for c in contexts if not access.is_reserved_context(c)}


def _not_stored_in_reserved_context(queryset):
    """`queryset` without the resources whose ``rbac_context`` is one reserved
    string (``__x``, ``*``), which every detail request answers 404 for,
    superusers included (``access.stored_in_reserved_context``; plain forms
    only, as `_plainly_reserved`). A ``Case`` rather than an ``exclude()``: a
    content without the key (or with a non-string value) compares as SQL
    ``NULL`` there, which ``exclude()`` would drop too."""
    reserved = Q(content__rbac_context__startswith="__") | Q(content__rbac_context="*")
    return queryset.alias(
        _scrud_stored_in_reserved_context=Case(
            When(reserved, then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        )
    ).filter(_scrud_stored_in_reserved_context=False)


def filter_readable_resources(queryset, request, permission):
    """`queryset` (of `Resource`) restricted to the resources `permission`
    (``http.GET`` on the item type) is allowed on, as the detail route's
    object check allows it: in **any** of the resource's usable contexts
    (``access.may_act_on``, ``access.usable_contexts``). A resource with no
    usable context is a superuser's only.

    It re-implements ``scoped_rbac.access.filter_authorized`` (its SQL path)
    over the `ResourceContext` rows rather than an ``rbac_context`` column:

    - a superuser gets every resource but those stored in a reserved context;
    - a grant in every context (``ListScope.unrestricted``) keeps every
      resource with a usable context row;
    - otherwise a resource with a row in a context that grants `permission`
      outright is kept without evaluation, and does not count against
      ``SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]``; the others with a row in a
      context that may grant it by a condition (or any, for a condition that
      applies in every context) are evaluated one by one with the resource as
      the condition's resource (its content, `content_of`, as the detail
      route's object check gives it), as
      ``access._filter_per_object`` does. More candidates than the cap raise
      ``access.ConditionalListTooLarge``.

    It stays one queryset, so pagination and ``count()`` see only the readable
    resources."""
    user = access.request_user(request)
    if user is not None and user.is_superuser:
        return _not_stored_in_reserved_context(queryset)
    policy = policy_for(request)
    scope = policy.list_scope(permission, exclude=(SOME_CONTEXT,))
    if scope.unrestricted:
        return queryset.filter(_rows(~_plainly_reserved()))
    outright = _usable(scope.outright)
    conditional = _usable(scope.conditional) - outright
    if not (outright or conditional or scope.conditional_anywhere):
        return queryset.none()
    in_outright = _rows(rbac_context__in=outright)
    allowed_pks = []
    if conditional or scope.conditional_anywhere:
        candidates = queryset.exclude(in_outright) if outright else queryset
        if scope.conditional_anywhere:
            candidates = candidates.filter(_rows(~_plainly_reserved()))
        else:
            candidates = candidates.filter(_rows(rbac_context__in=conditional))
        allowed_pks = _conditionally_allowed(
            candidates, request, policy, permission, scope, conditional
        )
    if not outright:
        return queryset.filter(pk__in=allowed_pks)
    if not allowed_pks:
        return queryset.filter(in_outright)
    return queryset.filter(in_outright | Q(pk__in=allowed_pks))


def _conditionally_allowed(candidates, request, policy, permission, scope, contexts):
    """The keys of the `candidates` a condition allows `permission` on, in one
    of their usable `contexts` (any usable one, for a condition that applies in
    every context), as ``scoped_rbac.access._filter_per_object`` evaluates
    them. Raises ``access.ConditionalListTooLarge`` past the cap."""
    max_rows = conditional_list_max_rows()
    allowed = []
    # One more than the cap tells a list over it, without reading the rest.
    bounded = candidates.order_by()[: max_rows + 1]
    for count, obj in enumerate(
        bounded.iterator(chunk_size=CANDIDATE_CHUNK_SIZE), start=1
    ):
        if count > max_rows:
            raise access.ConditionalListTooLarge()
        if access.stored_in_reserved_context(obj):
            continue
        usable = access.usable_contexts(access.object_contexts(obj))
        evaluated = [c for c in usable if scope.conditional_anywhere or c in contexts]
        if not evaluated:
            continue
        resource = access.cel_resource(request, content_of(obj))
        if any(policy.should_allow(permission, c, resource) for c in evaluated):
            allowed.append(obj.pk)
    return allowed


def filter_readable_resources_per_object(queryset, request, permission, contexts_for):
    """`queryset` (of `Resource`) restricted to the resources `permission` is
    allowed on as an ``IsAuthorized`` whose ``contexts_for`` is overridden
    allows them on the detail route: ``scoped_rbac.access.filter_authorized``
    with a custom `contexts_for` (one ``contexts_for``, or several each of
    which must allow a resource, an ``access.AnyOf`` any option of which
    suffices), evaluated per resource as ``access._filter_per_object`` does,
    except that each ``contexts_for`` and each condition is given the
    resource's content (`content_of`, a ``dict``), as the detail route's object
    check gives it (`ResourceRbacFilter` builds `contexts_for` under
    `checks_read_content`, so scoped-rbac's default reads a resource's
    contexts from it). A
    superuser whose grants are unrestricted gets every resource not stored in
    a reserved context; otherwise every resource examined counts against
    ``SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]``."""
    policy = policy_for(request)
    scope = policy.list_scope(permission, exclude=(SOME_CONTEXT,))
    queryset = _not_stored_in_reserved_context(queryset)
    user = access.request_user(request)
    if scope.unrestricted and user is not None and user.is_superuser:
        return queryset
    if not (
        scope.unrestricted
        or scope.outright
        or scope.conditional
        or scope.conditional_anywhere
    ):
        return queryset.none()
    contexts_fors = access.non_default_contexts_for(contexts_for)

    def allowed(check, contexts, resource):
        contexts = access.usable_contexts(contexts)
        if not contexts:
            return False
        if scope.unrestricted or any(c in scope.outright for c in contexts):
            return True
        conditional = [
            c for c in contexts if scope.conditional_anywhere or c in scope.conditional
        ]
        if not conditional:
            return False
        if not resource:
            resource.append(access.cel_resource(request, check))
        return any(policy.should_allow(permission, c, resource[0]) for c in conditional)

    max_rows = conditional_list_max_rows()
    allowed_pks = []
    bounded = queryset.order_by()[: max_rows + 1]
    for count, obj in enumerate(
        bounded.iterator(chunk_size=CANDIDATE_CHUNK_SIZE), start=1
    ):
        if count > max_rows:
            raise access.ConditionalListTooLarge()
        if access.stored_in_reserved_context(obj):
            continue
        check = content_of(obj)
        resource = []  # the resource's CEL resource, built once if needed

        def allows(contexts_for, check=check, resource=resource):
            if isinstance(contexts_for, access.AnyOf):
                return any(
                    all(map(allows, option))
                    if isinstance(option, (list, tuple))
                    else allows(option)
                    for option in contexts_for.options
                )
            return allowed(check, contexts_for(check), resource)

        if all(map(allows, contexts_fors)):
            allowed_pks.append(obj.pk)
    return queryset.filter(pk__in=allowed_pks)


def overrides_object_checks(permission):
    """Whether `permission` (an ``IsAuthorized``) overrides
    ``has_object_permission`` or ``has_context_permission``: its object check
    may then refuse what the grants allow, which the SQL filter cannot see."""
    cls = type(permission)
    return (
        cls.has_object_permission is not IsAuthorized.has_object_permission
        or cls.has_context_permission is not IsAuthorized.has_context_permission
    )


def _requirement_leaves(requirement):
    for entry in requirement:
        if isinstance(entry, access.AnyOf):
            for option in entry.options:
                yield from _requirement_leaves(
                    option if isinstance(option, (list, tuple)) else [option]
                )
        else:
            yield entry


def object_check_requirement(view):
    """What `view`'s permissions require of an object as far as RBAC is
    concerned (``AccessControlledAPIView.rbac_requirement``: entries that must
    all allow it, an ``access.AnyOf`` of alternatives for DRF's ``|``), when
    an ``IsAuthorized`` in it overrides ``has_object_permission`` or
    ``has_context_permission`` (`overrides_object_checks`); else ``None``,
    and the list is decided by the grants (and ``contexts_for``) alone. A
    view without ``rbac_requirement`` requires each of its ``IsAuthorized``
    permissions."""
    rbac_requirement = getattr(view, "rbac_requirement", None)
    if callable(rbac_requirement):
        requirement = list(rbac_requirement())
    else:
        permissions = getattr(view, "get_permissions", None)
        permissions = permissions() if callable(permissions) else []
        requirement = [p for p in permissions if isinstance(p, IsAuthorized)]
    if any(map(overrides_object_checks, _requirement_leaves(requirement))):
        return requirement
    return None


@contextmanager
def as_detail_route(view):
    """`view` as its detail route has it while the block runs: its
    ``action`` is ``"retrieve"``, so that ``resource_type_iri_for`` (and so
    ``IsAuthorized``'s object check) names the item type, not the list type."""
    missing = object()
    previous = view.__dict__.get("action", missing)
    view.action = "retrieve"
    try:
        yield view
    finally:
        if previous is missing:
            del view.action
        else:
            view.action = previous


@contextmanager
def as_detail_read(view, request):
    """`view` as a GET of its detail route has it while the block runs, for
    asking whether an object is readable whatever the request's method: its
    ``action`` is ``"retrieve"`` (`as_detail_route`) and its ``request`` a
    copy of `request` whose method is GET (DRF's ``clone_request``), which the
    block gets. The view's own ``request`` is restored when it ends."""
    read = clone_request(request, "GET")
    missing = object()
    previous = view.__dict__.get("request", missing)
    view.request = read
    try:
        with as_detail_route(view):
            yield read
    finally:
        if previous is missing:
            del view.request
        else:
            view.request = previous


def meets_object_checks(requirement, request, view, check):
    """Whether every entry of `requirement` (`object_check_requirement`; any
    option of an ``access.AnyOf`` suffices) allows
    ``has_object_permission(request, view, check)``; one that raises a
    refusal (`REFUSALS`) refuses `check` only."""

    def meets(entry):
        if isinstance(entry, access.AnyOf):
            return any(
                all(meets(e) for e in option)
                if isinstance(option, (list, tuple))
                else meets(option)
                for option in entry.options
            )
        try:
            return entry.has_object_permission(request, view, check)
        except REFUSALS:
            # Raised for this object, the detail route refuses it alone.
            return False

    return all(meets(entry) for entry in requirement)


def readable_by_object_checks(view, request, check):
    """Whether `view`'s object checks allow a read of `check` (the object as
    its object check gets it), as its list asks them
    (`filter_by_object_checks`), whatever `request`'s method: with
    `object_check_requirement` built for a GET of the detail route
    (`as_detail_read`), each ``IsAuthorized`` of it reading a resource's
    contexts from its content. True when the view has no such requirement
    (its grants decide alone). They can only refuse: the grants are asked
    apart (``is_readable``)."""
    with as_detail_read(view, request) as read, checks_read_content(view):
        requirement = object_check_requirement(view)
        if requirement is None:
            return True
        reading_content(_requirement_leaves(requirement))
        return meets_object_checks(requirement, read, view, check)


def filter_by_object_checks(queryset, request, view, requirement, object_for_checks):
    """`queryset` (a list, already RBAC-filtered) restricted to the objects
    `view`'s object check allows, as the detail route asks it: each
    ``IsAuthorized`` of `requirement` (`object_check_requirement`; any option
    of an ``access.AnyOf`` suffices) must allow
    ``has_object_permission(request, view, object_for_checks(obj))`` (one that
    raises a refusal, `REFUSALS`, refuses that object only), with
    the request's method (GET or HEAD) on the view's item type
    (`as_detail_route`), and the view's permissions reading a resource's
    contexts from its content, as on the detail route (`reading_content`,
    `checks_read_content`). An object stored in a context reserved once
    normalised is left out (its detail route is a 404). Every object examined
    counts against ``SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]``: past it,
    ``access.ConditionalListTooLarge``."""

    # Each permission reads a resource's contexts from the content
    # (`reading_content`), whatever the view's get_permissions() built, and
    # so do those an object check asks view.get_permissions() for, as on the
    # detail route (`checks_read_content`).
    reading_content(_requirement_leaves(requirement))
    max_rows = conditional_list_max_rows()
    allowed_pks = []
    bounded = queryset.order_by()[: max_rows + 1]
    with as_detail_route(view), checks_read_content(view):
        for count, obj in enumerate(
            bounded.iterator(chunk_size=CANDIDATE_CHUNK_SIZE), start=1
        ):
            if count > max_rows:
                raise access.ConditionalListTooLarge()
            if access.stored_in_reserved_context(obj):
                continue
            check = object_for_checks(obj)
            if meets_object_checks(requirement, request, view, check):
                allowed_pks.append(obj.pk)
    return queryset.filter(pk__in=allowed_pks)


def readable_removal(request, view, resource_type, using=None):
    """The newest `ResourceRemoval` stamp of `resource_type` a list `view`'s
    ``Last-Modified`` takes for `request` (``None`` for none): of the contexts
    in which the requester may read **every** resource (``http.GET`` on the
    item type), so that a stamp only ever tells them that a resource they
    could list left: every context for a superuser, every context not
    plainly reserved for a grant in every context, else the contexts that
    grant it outright. A context that grants it only by a condition is left
    out: the resource that left may be one the condition refuses. Without
    `ResourceRbacFilter` among the view's ``filter_backends``, with an
    ``IsAuthorized`` that overrides ``contexts_for`` (whose contexts are not
    the stamped ones), or with one that overrides ``has_object_permission``
    or ``has_context_permission`` (`object_check_requirement`: it may refuse
    the resource that left), there is none. Read on database `using` (the
    list's)."""
    backends = getattr(view, "filter_backends", ())
    if not any(
        isinstance(backend, type) and issubclass(backend, ResourceRbacFilter)
        for backend in backends
    ):
        return None
    if access.non_default_contexts_for(RbacFilter.contexts_for(view)) is not None:
        return None
    if object_check_requirement(view) is not None:
        return None
    stamps = ResourceRemoval.objects.filter(resource_type=resource_type)
    if using is not None:
        stamps = stamps.using(using)
    user = access.request_user(request)
    if user is None or not user.is_superuser:
        permission = Permission("http.GET", view.resource_type_iri)
        scope = policy_for(request).list_scope(permission, exclude=(SOME_CONTEXT,))
        if scope.unrestricted:
            stamps = stamps.exclude(_plainly_reserved())
        else:
            stamps = stamps.filter(rbac_context__in=_usable(scope.outright))
    return stamps.aggregate(newest=Max("removed_at"))["newest"]


class ResourceRbacFilter(RbacFilter):
    """scoped-rbac's ``RbacFilter`` for the scrud viewsets, whose lists show
    exactly what the detail route shows as far as ``IsAuthorized``
    permissions decide (another ``BasePermission`` is not asked for lists, as
    in DRF): a list (a GET or HEAD outside
    ``get_object()``) of `Resource` is restricted to the resources the
    requester may ``http.GET`` as **items** of the view's type
    (``view.resource_type_iri``) in any of their usable contexts
    (`filter_readable_resources`). The list type's ``http.GET``
    (``view.list_type_iri``) remains the view-level check that opens the list.

    A detail lookup (inside ``get_object()``) and any other method are
    ``RbacFilter``'s. A view whose ``IsAuthorized`` overrides ``contexts_for``
    is listed per object with those contexts
    (`filter_readable_resources_per_object`), with the item type's
    permission. A view whose ``IsAuthorized`` overrides
    ``has_object_permission`` or ``has_context_permission`` is narrowed
    further, after this filter, by its view
    (``ListValidatorsMixin.filter_queryset``, `filter_by_object_checks`)."""

    def filter_queryset_for_get(self, request, queryset, view):
        if self.is_detail_request(request, view) or not issubclass(
            queryset.model, Resource
        ):
            return super().filter_queryset_for_get(request, queryset, view)
        permission = Permission(http_action_iri_for(request), view.resource_type_iri)
        # A permission that keeps scoped-rbac's contexts_for reads a
        # resource's contexts from the content it is given.
        with checks_read_content(view):
            contexts_for = self.contexts_for(view)
        try:
            if access.non_default_contexts_for(contexts_for) is not None:
                return filter_readable_resources_per_object(
                    queryset, request, permission, contexts_for
                )
            return filter_readable_resources(queryset, request, permission)
        except access.ConditionalListTooLarge as exc:
            raise AccessAPIError(exc) from exc


# A query parameter that names an integer: a JSON path reads it as an array
# index, so it cannot name a key of a resource's content (an object).
_INTEGER = re.compile(r"^[+-]?\d+$")


class ResourceContentFilter(BaseFilterBackend):
    """Filter a list of resources on its query parameters: each parameter
    ``key=value`` keeps the resources whose content's top-level ``key`` is the
    string ``value``, a JSON key lookup (``KeyTransform``) that every database
    Django supports evaluates. The key is a key, never a lookup:
    ``name__startswith=x`` keeps the resources whose key
    ``"name__startswith"`` is ``"x"``. All values of a repeated parameter must
    match. The pagination parameters and DRF's format override are not
    filters. A key that is an integer is a 400 (a JSON path reads it as an
    array index), and so is a NUL character in a key or a value (PostgreSQL
    text cannot hold one). Only lists are filtered (``view.action == "list"``)."""

    def ignored_parameters(self, view):
        paginator = getattr(view, "paginator", None)
        ignored = {api_settings.URL_FORMAT_OVERRIDE}
        for name in ("page_query_param", "page_size_query_param"):
            value = getattr(paginator, name, None)
            if value:
                ignored.add(value)
        return ignored

    def filter_queryset(self, request, queryset, view):
        if getattr(view, "action", None) != "list":
            return queryset
        ignored = self.ignored_parameters(view)
        filters = [
            (key, values)
            for key, values in request.query_params.lists()
            if key not in ignored
        ]
        errors = {}
        for key, values in filters:
            if "\x00" in key or any("\x00" in value for value in values):
                # PostgreSQL text cannot hold NUL: a 400 on every database.
                errors[key] = ["A query parameter cannot contain a NUL character."]
            elif _INTEGER.match(key):
                errors[key] = [
                    "An integer is not a content key a list can be filtered on."
                ]
        if errors:
            raise ValidationError(errors, code="invalid")
        for index, (key, values) in enumerate(filters):
            alias = f"_scrud_content_filter_{index}"
            queryset = queryset.alias(**{alias: KeyTransform(key, "content")})
            for value in values:
                queryset = queryset.filter(**{alias: value})
        return queryset
