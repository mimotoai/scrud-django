"""A list of resources without DRF: the resources a requester may list
(`readable_queryset`), filtered on query parameters (`content_filtered`), and
the list's validators (`list_etag`, `list_last_modified`) and the
preconditions of a POST to it (`evaluate_list_preconditions`), as the DRF
routes answer them (`scrud_django.filters.ResourceRbacFilter`,
``ResourceContentFilter``, ``ListValidatorsMixin``).

A requirement's leaves must read a resource's contexts as they are stored
(their ``ResourceContext`` rows, which the SQL filter and the removal stamps
read), as `scrud_django.core.requirements.GrantsLeaf` does: a front end whose
permissions read other contexts keeps a path of its own, as the DRF views
do for an ``IsAuthorized`` that overrides ``contexts_for``.
"""

import hashlib
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from urllib.parse import urlencode

from django.db.models import (
    BooleanField,
    Case,
    Exists,
    Max,
    OuterRef,
    Q,
    QuerySet,
    Value,
    When,
)
from django.db.models.fields.json import KeyTransform
from django.http import QueryDict
from scoped_rbac import access
from scoped_rbac.conf import conditional_list_max_rows
from scoped_rbac.policy import Permission
from scoped_rbac.rbac_contexts import SOME_CONTEXT
from scrudful._django import aware
from scrudful.conditional import (
    ConditionalResult,
    evaluate_preconditions,
    http_timestamp,
    parse_etag_list,
    parse_http_date,
    quote_etag,
)

from scrud_django.core.authz import condition_resource, content_of
from scrud_django.core.requirements import (
    CANDIDATE_CHUNK_SIZE,
    _plainly_reserved,
    _usable,
    as_get,
    filter_by_object_checks,
    object_check_requirement,
    readable_removal,
)
from scrud_django.exceptions import QueryParameterError
from scrud_django.models import Resource, ResourceContext

# The preconditions a POST to a list evaluates (`evaluate_list_preconditions`).
LIST_PRECONDITIONS = ("if-match", "if-none-match", "if-unmodified-since")

# A query parameter that names an integer: a JSON path reads it as an array
# index, so it cannot name a key of a resource's content (an object).
_INTEGER = re.compile(r"^[+-]?\d+$")


def _rows(*conditions, **filters):
    """``Exists`` of a `ResourceContext` row of the outer resource matching
    `conditions` and `filters`."""
    return Exists(
        ResourceContext.objects.filter(*conditions, resource=OuterRef("pk"), **filters)
    )


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
    policy = access.policy_for(request)
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
        content = content_of(obj)
        # Each context's conditions read the content as it is in that context.
        if any(
            policy.should_allow(permission, c, condition_resource(content, c))
            for c in evaluated
        ):
            allowed.append(obj.pk)
    return allowed


def readable_queryset(
    request, resource_type, requirement, *, using: str | None = None
) -> QuerySet[Resource]:
    """The resources of `resource_type` `request`'s user may list, read on
    database `using` (``None``: as the router reads), in today's order: the
    RBAC filter (`filter_readable_resources`, ``http.GET`` on the type's
    ``type_uri``), then, when a leaf of `requirement` overrides its object
    checks (`object_check_requirement`), those checks, object by object
    (`filter_by_object_checks`, given each resource's content). Past
    ``SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]`` it raises
    ``access.ConditionalListTooLarge``.

    First, before it filters, the list's request check
    (`scrud_django.core.resources.check_request` with ``list_route``): the
    list grant (``http.GET`` on the list type, ``collection_type_uri_for``)
    and each entry's ``has_permission`` of the GET, as the DRF route asks
    them before it lists; a refusal is ``access.AccessDenied`` (a 403), and
    a `requirement` that asks no grants ``ImproperlyConfigured``. A list is
    a GET: a request of another method (a POST's list validators) is asked,
    checked included, as its GET copy (`as_get`), so a requester who may not
    list the resources gets no validators describing them."""
    # Imported here: scrud_django.core.resources imports this module.
    from scrud_django.core.resources import check_request

    if request.method not in ("GET", "HEAD"):
        request = as_get(request)
    check_request(request, resource_type, requirement, list_route=True)
    queryset = Resource.objects.filter(resource_type=resource_type)
    if using is not None:
        queryset = queryset.using(using)
    permission = Permission("http.GET", resource_type.type_uri)
    queryset = filter_readable_resources(queryset, request, permission)
    narrowing = object_check_requirement(requirement)
    if narrowing is not None:
        queryset = filter_by_object_checks(queryset, request, narrowing, content_of)
    return queryset


def content_filtered(queryset, params: QueryDict, *, ignored: frozenset[str]):
    """`queryset` (of `Resource`) filtered on the query parameters `params`
    but those `ignored` (the pagination parameters, the format override and
    the UI's, the caller's): each ``key=value`` keeps the resources whose
    content's top-level ``key`` is the string ``value``, a JSON key lookup
    (``KeyTransform``) every database Django supports evaluates. The key is a
    key, never a lookup. All values of a repeated parameter must match. A key
    that is an integer (a JSON path reads it as an array index), or a NUL
    character in a key or a value (PostgreSQL text cannot hold one), is a
    `QueryParameterError` (400) naming every such parameter."""
    filters = [(key, values) for key, values in params.lists() if key not in ignored]
    errors = {}
    for key, values in filters:
        if "\x00" in key or any("\x00" in value for value in values):
            # PostgreSQL text cannot hold NUL: a 400 on every database.
            errors[key] = ["A query parameter cannot contain a NUL character."]
        elif _INTEGER.match(key):
            errors[key] = ["An integer is not a content key a list can be filtered on."]
    if errors:
        raise QueryParameterError(errors)
    for index, (key, values) in enumerate(filters):
        alias = f"_scrud_content_filter_{index}"
        queryset = queryset.alias(**{alias: KeyTransform(key, "content")})
        for value in values:
            queryset = queryset.filter(**{alias: value})
    return queryset


def list_etag(
    resource_type,
    *,
    items: Sequence[Resource],
    count: int,
    query: list[tuple[str, list[str]]],
) -> str:
    """The list's ETag (unquoted): a hash of what the requester gets, the
    page's `items` (key and ETag, in page order), the list's `count`, the
    query parameters `query` (``QueryDict.lists()``, sorted here) and
    `resource_type`'s link targets. Nothing else changes it."""
    parts = [
        resource_type.pk,
        resource_type.revision,
        resource_type.schema_id,
        resource_type.schema_uri,
        resource_type.context_id,
        resource_type.context_uri,
        count,
        urlencode(sorted(query), doseq=True),
        *(f"{obj.pk}:{obj.etag}" for obj in items),
    ]
    digest = hashlib.sha256("\n".join(map(str, parts)).encode("utf-8"))
    return digest.hexdigest()


def latest(*times):
    """The latest of `times` that is not ``None`` (``None`` for none)."""
    return max((t for t in times if t is not None), default=None)


def list_last_modified(request, resource_type, queryset, requirement):
    """The list's Last-Modified: the newest ``modified_at`` of `queryset`
    (`readable_queryset`'s), or the newest time a resource left a context in
    which the requester may list every resource
    (`scrud_django.core.requirements.readable_removal` of `requirement`, read
    on the queryset's database), whichever is later; ``None`` for neither.
    The leaves must read a resource's contexts as stored (the module's
    note): the stamps are of those contexts."""
    newest = queryset.aggregate(newest=Max("modified_at"))["newest"]
    removed = readable_removal(request, requirement, resource_type, queryset.db)
    return latest(newest, removed)


def has_list_preconditions(headers):
    """Whether `headers` (as ``precondition_headers`` gives them) carry a
    precondition a POST to a list evaluates (`LIST_PRECONDITIONS`)."""
    return any(headers.get(name) for name in LIST_PRECONDITIONS)


def list_validators_needed(headers):
    """Whether evaluating the preconditions `headers` (lower-case names, as
    ``scrudful._django.precondition_headers`` gives them) of a POST to a list
    needs the list's validators (RFC 9110 §13.2.2): an ``If-Match`` that names
    entity-tags, an ``If-Unmodified-Since`` with a valid date and no
    ``If-Match``, or an ``If-None-Match`` that names entity-tags. ``*``
    (either field) is decided by the list existing, a malformed field is a
    400 and an invalid date is ignored, without them."""
    if_match = parse_etag_list(headers.get("if-match") or None)
    if_none_match = parse_etag_list(headers.get("if-none-match") or None)
    if if_match is None or if_none_match is None:
        return False
    if if_match and if_match != ["*"]:
        return True
    if_unmodified_since = parse_http_date(headers.get("if-unmodified-since") or None)
    if not if_match and if_unmodified_since is not None:
        return True
    return bool(if_none_match) and if_none_match != ["*"]


def evaluate_list_preconditions(
    method: str,
    headers,
    validators: Callable[[], tuple[str | None, datetime | None]],
) -> ConditionalResult | None:
    """The failed preconditions of a POST to a list, or ``None`` to go on:
    ``If-Match``, else ``If-Unmodified-Since``, then ``If-None-Match`` (``*``
    fails: the list exists), with ``scrudful.conditional.evaluate_preconditions``
    (a 412, or a 400 for a malformed one), against the list's
    ``(etag, last_modified)`` (unquoted ETag) `validators` returns.
    `validators` is called only when a precondition needs them
    (`list_validators_needed`). Rendering the result is the caller's."""
    if not has_list_preconditions(headers):
        return None
    etag = last_modified = None
    if list_validators_needed(headers):
        etag, last_modified = validators()
    return evaluate_preconditions(
        method,
        headers,
        etag=None if etag is None else quote_etag(etag),
        last_modified=http_timestamp(aware(last_modified)),
        require_precondition=False,
        exists=True,
    )
