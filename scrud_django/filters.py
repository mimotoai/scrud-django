"""List filters of the scrud viewsets.

`ResourceRbacFilter` restricts a list of resources to those the requester may
read, as the detail route decides it, using the `ResourceContext` rows to find
them in SQL. `ResourceContentFilter` filters a list on query parameters.
"""

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework.exceptions import (
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    ValidationError,
)
from rest_framework.filters import BaseFilterBackend
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

from scrud_django.core import requirements as core
from scrud_django.core.lists import (  # noqa: F401  (re-exported)
    _INTEGER,
    _conditionally_allowed,
    _not_stored_in_reserved_context,
    _rows,
    content_filtered,
    filter_readable_resources,
)
from scrud_django.core.requirements import (  # noqa: F401  (re-exported)
    CANDIDATE_CHUNK_SIZE,
    _plainly_reserved,
    _usable,
)
from scrud_django.drf_requirements import (  # noqa: F401  (re-exported)
    NotNeutral,
    as_detail_read,
    as_detail_route,
    hook_leaves,
    leaves_of,
    overrides_object_checks,
    permissions_of,
    requirement_of,
)
from scrud_django.exceptions import QueryParameterError
from scrud_django.models import Resource, ResourceContext  # noqa: F401
from scrud_django.permissions import (
    checks_read_content,
    condition_resource,
    content_of,
    may_act_in_context,
    reading_content,
)

# What an object check may raise to refuse one object (DRF's and Django's
# idioms): a list leaves that object out, as its detail route refuses it.
REFUSALS = (
    PermissionDenied,
    NotAuthenticated,
    NotFound,
    Http404,
    DjangoPermissionDenied,
)


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

    def allowed(check, contexts, resources):
        # As scoped-rbac 0.11.2 reads them: a context that is not a str (a
        # lazy string, a number) names no context (only "*" reaches it).
        contexts = [access.context_name(c) for c in access.usable_contexts(contexts)]
        if not contexts:
            return False
        if scope.unrestricted or any(c in scope.outright for c in contexts):
            return True
        conditional = [
            c for c in contexts if scope.conditional_anywhere or c in scope.conditional
        ]
        if not conditional:
            return False

        def resource(context):
            # The content as it is in `context` (condition_resource), built
            # once for each context.
            if context not in resources:
                resources[context] = condition_resource(check, context)
            return resources[context]

        return any(policy.should_allow(permission, c, resource(c)) for c in conditional)

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
        resources = {}  # the CEL resource in each context, built once if needed

        def allows(contexts_for, check=check, resources=resources):
            if isinstance(contexts_for, access.AnyOf):
                return any(
                    all(map(allows, option))
                    if isinstance(option, (list, tuple))
                    else allows(option)
                    for option in contexts_for.options
                )
            return allowed(check, contexts_for(check), resources)

        if all(map(allows, contexts_fors)):
            allowed_pks.append(obj.pk)
    return queryset.filter(pk__in=allowed_pks)


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
    permissions. `scrud_django.core.requirements.object_check_requirement`
    of its requirement (`requirement_of`), as the view's permissions."""
    try:
        requirement = requirement_of(view)
    except NotNeutral:
        return _view_object_check_requirement(view)
    narrowing = core.object_check_requirement(requirement)
    return None if narrowing is None else permissions_of(narrowing)


def _view_object_check_requirement(view):
    """`object_check_requirement` of a view with no neutral requirement
    (`NotNeutral`), with the hooks registered for its type beside it
    (`hook_leaves`)."""
    rbac_requirement = getattr(view, "rbac_requirement", None)
    if callable(rbac_requirement):
        requirement = list(rbac_requirement())
    else:
        permissions = getattr(view, "get_permissions", None)
        permissions = permissions() if callable(permissions) else []
        requirement = [p for p in permissions if isinstance(p, IsAuthorized)]
    hooks = hook_leaves(view)
    if any(map(overrides_object_checks, _requirement_leaves(requirement))):
        return requirement + hooks
    return hooks or None


def meets_object_checks(requirement, request, view, check):
    """Whether every entry of `requirement` (`object_check_requirement`; any
    option of an ``access.AnyOf`` suffices) allows
    ``has_object_permission(request, view, check)``; one that raises a
    refusal (`REFUSALS`) refuses `check` only: the neutral
    `scrud_django.core.requirements.meets_object_checks` of the permissions
    as `view`'s leaves (`scrud_django.drf_requirements.ViewLeaf`)."""
    return core.meets_object_checks(leaves_of(requirement, view), request, check)


def grants_allow(view, request, obj, permission):
    """Whether the grants allow `permission` on `obj` (the object as its
    object check gets it) as `view`'s ``IsAuthorized`` permissions read its
    contexts: scoped-rbac's ``AccessControlledAPIView.is_readable`` (in the
    contexts each ``IsAuthorized`` of the view's requirement reads,
    ``access.AnyOf`` alternatives included) for `permission`, except that
    each context is read with the object as it is in that context
    (`condition_resource`), as the object check reads it. Call it with the
    view's permissions reading the content (`checks_read_content`). The
    neutral `scrud_django.core.requirements.grants_allow` of its requirement
    (`requirement_of`), except for a view with none (`NotNeutral`)."""
    try:
        requirement = requirement_of(view)
    except NotNeutral:
        return _view_grants_allow(view, request, obj, permission)
    return core.grants_allow(requirement, request, obj, permission)


def _view_grants_allow(view, request, obj, permission):
    """`grants_allow` of a view with no neutral requirement (`NotNeutral`):
    without a requirement (only ``~IsAuthorized``), any ``IsAuthorized``
    reading the object allows it, as ``is_readable`` has it."""

    def reads(leaf):
        return may_act_in_context(request, permission, obj, leaf.contexts_for(obj))

    def meets(entry):
        if isinstance(entry, access.AnyOf):
            return any(all(map(meets, option)) for option in entry.options)
        return reads(entry)

    requirement = view.rbac_requirement()
    if requirement:
        return all(map(meets, requirement))
    leaves = view._rbac_permissions()
    return not leaves or any(map(reads, leaves))


def readable_by_grants(view, request, obj):
    """`grants_allow` for ``http.GET`` on the view's type: which tells a 403
    from a 404. Call it as a GET of the detail route has it
    (`as_detail_read`), with the view's permissions reading the content
    (`checks_read_content`)."""
    permission = Permission("http.GET", view.resource_type_iri_for(request))
    return grants_allow(view, request, obj, permission)


def readable_by_object_checks(view, request, check):
    """Whether `view`'s object checks allow a read of `check` (the object as
    its object check gets it), as its list asks them
    (`filter_by_object_checks`), whatever `request`'s method: with
    `object_check_requirement` built for a GET of the detail route
    (`as_detail_read`), each ``IsAuthorized`` of it reading a resource's
    contexts from its content. True when the view has no such requirement
    (its grants decide alone). They can only refuse: the grants are asked
    apart (``is_readable``). The neutral
    `scrud_django.core.requirements.readable_by_object_checks` of its
    requirement (`requirement_of`), as a GET of the detail route builds it."""
    with as_detail_read(view, request) as read, checks_read_content(view):
        try:
            requirement = requirement_of(view)
        except NotNeutral:
            requirement = _view_object_check_requirement(view)
            if requirement is None:
                return True
            reading_content(_requirement_leaves(requirement))
            return meets_object_checks(requirement, read, view, check)
        return core.readable_by_object_checks(requirement, read, check)


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
    ``access.ConditionalListTooLarge``. The neutral
    `scrud_django.core.requirements.filter_by_object_checks` of the
    permissions as `view`'s leaves."""

    # Each permission reads a resource's contexts from the content
    # (`reading_content`), whatever the view's get_permissions() built, and
    # so do those an object check asks view.get_permissions() for, as on the
    # detail route (`checks_read_content`).
    reading_content(_requirement_leaves(requirement))
    with as_detail_route(view), checks_read_content(view):
        return core.filter_by_object_checks(
            queryset, request, leaves_of(requirement, view), object_for_checks
        )


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
    list's). The neutral `scrud_django.core.requirements.readable_removal`
    of its requirement (`requirement_of`) once the view's filters and
    ``contexts_for`` are its stamped contexts'."""
    backends = getattr(view, "filter_backends", ())
    if not any(
        isinstance(backend, type) and issubclass(backend, ResourceRbacFilter)
        for backend in backends
    ):
        return None
    if access.non_default_contexts_for(RbacFilter.contexts_for(view)) is not None:
        return None
    try:
        requirement = requirement_of(view)
    except NotNeutral:
        if _view_object_check_requirement(view) is not None:
            return None
        requirement = []
    return core.readable_removal(
        request, requirement, resource_type, using, type_iri=view.resource_type_iri
    )


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


def ui_parameters() -> frozenset[str]:
    """The query parameters that select the UI's presentation, never content,
    when ``scrud_django.ui`` is installed: the branch and its confirmation,
    the scope chain, the mode and safe mode. Empty without the UI."""
    from scrud_django.ui import is_enabled

    if not is_enabled():
        return frozenset()
    from scrud_django.ui.branches.effective import BRANCH_PARAMETER, SAFE_PARAMETER
    from scrud_django.ui.branches.selection import CONFIRM_PARAMETER
    from scrud_django.ui.renderer import MODE_PARAMETER, SCOPE_PARAMETER

    return frozenset(
        (
            BRANCH_PARAMETER,
            CONFIRM_PARAMETER,
            SCOPE_PARAMETER,
            MODE_PARAMETER,
            SAFE_PARAMETER,
        )
    )


class ResourceContentFilter(BaseFilterBackend):
    """Filter a list of resources on its query parameters: each parameter
    ``key=value`` keeps the resources whose content's top-level ``key`` is the
    string ``value``, a JSON key lookup (``KeyTransform``) that every database
    Django supports evaluates. The key is a key, never a lookup:
    ``name__startswith=x`` keeps the resources whose key
    ``"name__startswith"`` is ``"x"``. All values of a repeated parameter must
    match. The pagination parameters and DRF's format override are not
    filters, and neither are the UI's URL parameters when ``scrud_django.ui``
    is installed (`ui_parameters`). A key that is an integer is a 400 (a JSON path reads it as an
    array index), and so is a NUL character in a key or a value (PostgreSQL
    text cannot hold one). Only lists are filtered (``view.action == "list"``)."""

    def ignored_parameters(self, view):
        paginator = getattr(view, "paginator", None)
        ignored = {api_settings.URL_FORMAT_OVERRIDE}
        for name in ("page_query_param", "page_size_query_param"):
            value = getattr(paginator, name, None)
            if value:
                ignored.add(value)
        return ignored | ui_parameters()

    def filter_queryset(self, request, queryset, view):
        if getattr(view, "action", None) != "list":
            return queryset
        try:
            return content_filtered(
                queryset,
                request.query_params,
                ignored=frozenset(self.ignored_parameters(view)),
            )
        except QueryParameterError as exc:
            raise ValidationError(exc.errors, code="invalid") from exc
