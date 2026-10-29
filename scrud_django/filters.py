"""List filters of the scrud viewsets.

`ResourceRbacFilter` restricts a list of resources to those the requester may
read, as the detail route decides it, using the `ResourceContext` rows to find
them in SQL. `ResourceContentFilter` filters a list on query parameters.
"""

import re

from django.db.models import BooleanField, Case, Exists, OuterRef, Q, Value, When
from django.db.models.fields.json import KeyTransform
from rest_framework.exceptions import ValidationError
from rest_framework.filters import BaseFilterBackend
from rest_framework.settings import api_settings
from scoped_rbac import access
from scoped_rbac.conf import conditional_list_max_rows
from scoped_rbac.filters import RbacFilter
from scoped_rbac.permissions import AccessAPIError, http_action_iri_for, policy_for
from scoped_rbac.policy import Permission
from scoped_rbac.rbac_contexts import SOME_CONTEXT

from scrud_django.models import Resource, ResourceContext

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
      the condition's resource (``access.cel_resource``), as
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
        resource = access.cel_resource(request, obj)
        if any(policy.should_allow(permission, c, resource) for c in evaluated):
            allowed.append(obj.pk)
    return allowed


class ResourceRbacFilter(RbacFilter):
    """scoped-rbac's ``RbacFilter`` for the scrud viewsets, whose lists show
    exactly what the detail route shows: a list (a GET or HEAD outside
    ``get_object()``) of `Resource` is restricted to the resources the
    requester may ``http.GET`` as **items** of the view's type
    (``view.resource_type_iri``) in any of their usable contexts
    (`filter_readable_resources`). The list type's ``http.GET``
    (``view.list_type_iri``) remains the view-level check that opens the list.

    A detail lookup (inside ``get_object()``) and any other method are
    ``RbacFilter``'s. A view whose ``IsAuthorized`` overrides ``contexts_for``
    is listed per object with those contexts (``access.filter_authorized``),
    with the item type's permission."""

    def filter_queryset_for_get(self, request, queryset, view):
        if self.is_detail_request(request, view) or not issubclass(
            queryset.model, Resource
        ):
            return super().filter_queryset_for_get(request, queryset, view)
        permission = Permission(http_action_iri_for(request), view.resource_type_iri)
        contexts_for = self.contexts_for(view)
        try:
            if contexts_for is not None:
                return access.filter_authorized(
                    queryset,
                    policy_for(request),
                    permission,
                    request=request,
                    contexts_for=contexts_for,
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
