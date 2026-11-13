"""A list of resources without DRF: the resources a requester may list
(`readable_queryset`), filtered on query parameters (`content_filtered`),
both in the list's order (`list_queryset`), a page of them read key first
on MySQL (`page_of`, `rows_by_keys`), and the list's validators
(`list_etag`, `list_last_modified`) and the preconditions of a POST to it
(`evaluate_list_preconditions`), as the DRF routes answer them
(`scrud_django.filters.ResourceRbacFilter`, ``ResourceContentFilter``,
``StandardResultsSetPagination``, ``ListValidatorsMixin``).

A requirement's leaves must read a resource's contexts as they are stored
(their ``ResourceContext`` rows, which the SQL filter and the removal stamps
read), as `scrud_django.core.requirements.GrantsLeaf` does:
`readable_queryset` and `list_last_modified` refuse (``ImproperlyConfigured``,
`scrud_django.core.requirements.require_stored_contexts`) a requirement with
a leaf that may read others (a ``contexts_for`` of its own, or a class not
registered with ``register_stored_contexts_leaf``). A front end whose
permissions read other contexts keeps a path of its own, as the DRF views
do for an ``IsAuthorized`` that overrides ``contexts_for``.
"""

import hashlib
import json
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from django.core.paginator import Paginator
from django.db import connections
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
from django.utils.encoding import force_str
from django.utils.http import quote_etag as quote_django_etag
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
    Requirement,
    _plainly_reserved,
    _usable,
    as_get,
    filter_by_object_checks,
    object_check_requirement,
    readable_removal,
    require_stored_contexts,
)
from scrud_django.core.types import type_options
from scrud_django.core.urls import absolute_reverse
from scrud_django.exceptions import ContentFilterError
from scrud_django.headers import get_link_header_for
from scrud_django.models import Resource, ResourceContext
from scrud_django.rows import unsorted
from scrud_django.utils import http_date_for

# The preconditions a POST to a list evaluates (`evaluate_list_preconditions`).
LIST_PRECONDITIONS = ("if-match", "if-none-match", "if-unmodified-since")

# The paging rules of a list, as the DRF paginator
# (`scrud_django.paginations.StandardResultsSetPagination`) has them: the
# query parameters naming the page and its size, the size of a page that
# names none (or not a positive integer) and the largest size, and the page
# names standing for the last page.
PAGE_PARAMETER = "page"
PAGE_SIZE_PARAMETER = "page_size"
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
LAST_PAGE_STRINGS = ("last",)

# DRF's format override (``URL_FORMAT_OVERRIDE``'s default), which a list is
# not filtered on.
FORMAT_PARAMETER = "format"

# The query parameters that select the UI's presentation (``scrud_django.ui``:
# the branch and its confirmation, the scope chain, the mode and safe mode),
# never content (`ui_parameters`).
UI_PARAMETERS = frozenset(
    {"ui-branch", "ui-branch-confirm", "ui-scope", "ui-mode", "ui-safe"}
)

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
    request,
    resource_type,
    requirement: Requirement | Sequence,
    *,
    using: str | None = None,
) -> QuerySet[Resource]:
    """The resources of `resource_type` `request`'s user may list, read on
    database `using` (``None``: as the router reads), in today's order: the
    RBAC filter (`filter_readable_resources`, ``http.GET`` on the type's
    ``type_uri``), then, when a leaf of `requirement` overrides its object
    checks (`object_check_requirement`), those checks, object by object
    (`filter_by_object_checks`, given each resource's content). Past
    ``SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]`` it raises
    ``access.ConditionalListTooLarge``.

    First, for a GET or HEAD, before it filters, the list's request check
    (`scrud_django.core.resources.check_request` with ``list_route``): the
    list grant (``http.GET`` on the list type, ``collection_type_uri_for``)
    and the other entries' ``has_permission`` of the GET (a type's hooks),
    as the DRF route asks them before it lists; a refusal is
    ``access.AccessDenied`` (a 403). A request of another method (a write's
    list validators, a conditional create's) is not checked here: the
    write's own request check governs it (`validate_write`'s), as DRF
    evaluates a conditional create's list validators without the list
    grant; its list is that of its GET copy (`as_get`), filtered by the
    item grants. A `requirement` (taken through
    `scrud_django.core.requirements.Requirement.of`) that asks no grants,
    or with a leaf that may read contexts other than a resource's stored
    ones (`scrud_django.core.requirements.require_stored_contexts`), is
    ``ImproperlyConfigured`` whatever the method or the requester.

    The object checks ask, for each ``AnyOf`` option, its request checks
    first (``request_checks``), as DRF's ``|`` answers the detail route."""
    # Imported here: scrud_django.core.resources imports this module.
    from scrud_django.core.resources import check_request

    requirement = Requirement.of(requirement, caller="readable_queryset")
    require_stored_contexts(requirement, "readable_queryset")
    if request.method in ("GET", "HEAD"):
        check_request(request, resource_type, requirement, list_route=True)
    else:
        request = as_get(request)
    queryset = Resource.objects.filter(resource_type=resource_type)
    if using is not None:
        queryset = queryset.using(using)
    permission = Permission("http.GET", resource_type.type_uri)
    queryset = filter_readable_resources(queryset, request, permission)
    narrowing = object_check_requirement(requirement)
    if narrowing is not None:
        queryset = filter_by_object_checks(
            queryset, request, narrowing, content_of, request_checks=True
        )
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
    `ContentFilterError` (a ``QueryParameterError``, 400, answered as the DRF
    list route answers it) naming every such parameter."""
    filters = [(key, values) for key, values in params.lists() if key not in ignored]
    errors = {}
    for key, values in filters:
        if "\x00" in key or any("\x00" in value for value in values):
            # PostgreSQL text cannot hold NUL: a 400 on every database.
            errors[key] = ["A query parameter cannot contain a NUL character."]
        elif _INTEGER.match(key):
            errors[key] = ["An integer is not a content key a list can be filtered on."]
    if errors:
        raise ContentFilterError(errors)
    for index, (key, values) in enumerate(filters):
        alias = f"_scrud_content_filter_{index}"
        queryset = queryset.alias(**{alias: KeyTransform(key, "content")})
        for value in values:
            queryset = queryset.filter(**{alias: value})
    return queryset


def list_queryset(
    request,
    resource_type,
    params: QueryDict,
    *,
    requirement: Requirement | Sequence,
    ignored: frozenset[str] = frozenset(),
    using: str | None = None,
) -> QuerySet[Resource]:
    """The list of `resource_type` `request`'s user gets, as the DRF list
    route lists it: `readable_queryset` (its request check, its refusals,
    `requirement` and `using` as it takes them), then, when the type's
    ``TypeOptions.content_filter`` is set (`scrud_django.core.types.type_options`
    of its ``type_uri``), filtered on the query parameters `params` but those
    `ignored` (`content_filtered`, the pagination parameters, the format
    override and the UI's, the caller's; a `QueryParameterError` for a
    parameter that cannot be a content key), ordered by
    ``Resource.Meta.ordering``. Page it with `page_of`.

    It lists for a GET or HEAD only: any other method of `request` is a
    ``ValueError`` (`readable_queryset` asks the list grant for those two
    alone, so a list served for another method would skip it)."""
    if request.method not in ("GET", "HEAD"):
        raise ValueError(
            f"list_queryset lists for a GET or HEAD, not a {request.method}"
        )
    queryset = readable_queryset(request, resource_type, requirement, using=using)
    return _listed(queryset, resource_type, params, ignored)


def _listed(queryset, resource_type, params, ignored):
    """`queryset` filtered on `params` but those `ignored` when
    `resource_type`'s ``TypeOptions.content_filter`` is set, in the list's
    order."""
    if type_options(resource_type.type_uri).content_filter:
        queryset = content_filtered(queryset, params, ignored=ignored)
    return queryset.order_by(*Resource._meta.ordering)


def ui_parameters() -> frozenset[str]:
    """The UI's presentation parameters (`UI_PARAMETERS`) when
    ``scrud_django.ui`` is installed, which a list is not filtered on; empty
    without it."""
    from scrud_django.ui import is_enabled

    return UI_PARAMETERS if is_enabled() else frozenset()


def ignored_list_parameters() -> frozenset[str]:
    """The query parameters a list is not filtered on (`list_queryset`'s
    ``ignored``), as the DRF list route ignores them: its paging, DRF's format
    override and the UI's (`ui_parameters`)."""
    return (
        frozenset({PAGE_PARAMETER, PAGE_SIZE_PARAMETER, FORMAT_PARAMETER})
        | ui_parameters()
    )


def rows_by_keys(queryset, keys: Sequence) -> list[Resource]:
    """The rows of `queryset` whose keys are `keys`, in `keys`' order (a key
    `queryset` does not hold is left out), read by key with no ``ORDER BY``
    (`scrud_django.rows.unsorted`): sorting rows whose JSON content is over
    MySQL's ``sort_buffer_size`` fails there (error 1038,
    `scrud_django.rows`)."""
    rows = {row.pk: row for row in unsorted(queryset.filter(pk__in=keys))}
    return [rows[pk] for pk in keys if pk in rows]


def page_of(queryset, *, number: int, size: int) -> tuple[list[Resource], int]:
    """Page `number` (from 1) of `queryset` (`list_queryset`'s, ordered), of
    `size` rows, and the count of `queryset`'s rows, as Django's
    ``Paginator`` pages it (an empty list has one, empty, page). A `number`
    past the last page (or below 1) raises
    ``django.core.paginator.EmptyPage``; a `size` below 1 is a
    ``ValueError``. On MySQL the page is found among the keys alone
    (``values_list("pk", flat=True)``), then its rows are read by key
    (`rows_by_keys`): MySQL sorts with every column it selects, so a page
    holding a row whose content is over its ``sort_buffer_size`` would fail
    (error 1038); elsewhere the queryset is sliced."""
    if size < 1:
        raise ValueError(f"A page holds at least one row, not {size}.")
    rows, page = _paged(queryset, size, lambda paginator: number)
    return rows, page.paginator.count


def _paged(queryset, size, number_of):
    """The rows of the page of `queryset` numbered ``number_of(paginator)``
    (the Django ``Paginator`` of `size` rows), and that ``Page`` (`page_of`)."""
    if connections[queryset.db].vendor != "mysql":
        paginator = Paginator(queryset, size)
        page = paginator.page(number_of(paginator))
        return list(page.object_list), page
    paginator = Paginator(queryset.values_list("pk", flat=True), size)
    page = paginator.page(number_of(paginator))
    return rows_by_keys(queryset, list(page.object_list)), page


def page_size_of(
    params: QueryDict,
    *,
    param: str | None = PAGE_SIZE_PARAMETER,
    default: int | None = DEFAULT_PAGE_SIZE,
    maximum: int | None = MAX_PAGE_SIZE,
) -> int | None:
    """The size of the page the query parameters `params` ask for, as DRF's
    ``PageNumberPagination.get_page_size`` reads it (by default with the
    list's rules; a paginator passes its own ``page_size_query_param``,
    ``page_size`` and ``max_page_size``): `param` (its last value) when it
    is a positive integer, at most `maximum` (unless ``None``); else
    `default`, as for no `param`."""
    if not param:
        return default
    try:
        size = int(params[param])
    except (KeyError, ValueError):
        return default
    if size < 1:
        return default
    return min(size, maximum) if maximum else size


def page_number_of(
    params: QueryDict,
    paginator: Paginator,
    *,
    param: str = PAGE_PARAMETER,
    last: Sequence[str] = LAST_PAGE_STRINGS,
):
    """The page the query parameters `params` ask for, as DRF's
    ``PageNumberPagination.get_page_number`` reads it (by default with the
    list's names; a paginator passes its own ``page_query_param`` and
    ``last_page_strings``): `param` (its last value; 1 when absent or
    empty), the number of `paginator`'s last page for one of `last`. The
    ``Paginator`` validates it (a page that is not a number is
    ``PageNotAnInteger``, one out of range ``EmptyPage``)."""
    number = params.get(param) or 1
    if number in last:
        return paginator.num_pages
    return number


@dataclass(frozen=True)
class ListPage:
    """A page of a list (`requested_page`): its `items`, the `count` of the
    list's rows, its `number` (from 1) and its `size`."""

    items: list[Resource]
    count: int
    number: int
    size: int

    @property
    def page_count(self) -> int:
        """The list's number of pages of `size` rows: 0 for an empty list."""
        return math.ceil(self.count / self.size)

    @property
    def has_next(self) -> bool:
        return self.number < max(self.page_count, 1)

    @property
    def has_previous(self) -> bool:
        return self.number > 1


def requested_page(queryset, params: QueryDict) -> ListPage:
    """The page of `queryset` (`list_queryset`'s, ordered) the query
    parameters `params` ask for, as the DRF list route pages it
    (`page_size_of`, `page_number_of`, read as `page_of` reads a page). A
    page that is not one of the list's (not a number, below 1 or past the
    last; an empty list has one, empty, page) raises
    ``django.core.paginator.InvalidPage``, which the DRF route answers 404
    ``"Invalid page."``."""
    size = page_size_of(params)
    rows, page = _paged(
        queryset, size, lambda paginator: page_number_of(params, paginator)
    )
    return ListPage(
        items=rows, count=page.paginator.count, number=page.number, size=size
    )


def page_url(request, number: int, *, param: str = PAGE_PARAMETER) -> str:
    """The URL of page `number` of the list `request` asked for, as DRF's
    ``PageNumberPagination`` links the next and previous pages: `request`'s
    absolute URL with the page parameter `param` (by default the list's; a
    paginator passes its ``page_query_param``) set to `number` (left out for
    page 1), the query parameters sorted; under a published branch's prefix
    (``request.scrud_ui_mount``, ``scrud_django.ui``'s ``Mount``), the API's
    URL."""
    scheme, netloc, path, query, fragment = urlsplit(
        force_str(request.build_absolute_uri())
    )
    params = parse_qs(query, keep_blank_values=True)
    if number == 1:
        params.pop(param, None)
    else:
        params[force_str(param)] = [force_str(number)]
    query = urlencode(sorted(params.items()), doseq=True)
    url = urlunsplit((scheme, netloc, path, query, fragment))
    mount = getattr(request, "scrud_ui_mount", None)
    return mount.canonical(url) if mount is not None else url


def resource_envelope(request, resource_type, obj: Resource) -> dict:
    """The envelope of `obj` (of `resource_type`) in a list's ``content``, as
    the DRF list serializes it: its absolute URL (``href``, its detail route
    within the URL namespace `request` was resolved under), ``last_modified``
    (an HTTP date), its quoted ``etag``, its ``link`` (the Link field value
    of its schema and JSON-LD context) and its ``content``."""
    content = obj.content
    if isinstance(content, str):
        content = json.loads(content)
    return {
        "href": absolute_reverse(
            resource_type.route_name_detail(), args=[obj.pk], request=request
        ),
        "last_modified": http_date_for(obj.modified_at),
        "etag": quote_django_etag(obj.etag),
        "link": get_link_header_for(resource_type, request),
        "content": content,
    }


def list_body(request, resource_type, page: ListPage) -> dict:
    """The body of the list `request` asked for whose page is `page`, as the
    DRF list route answers it: ``count``, ``page_count``, the ``next`` and
    ``previous`` pages' URLs (`page_url`, ``None`` at either end) and the
    page's envelopes (`resource_envelope`) as ``content``."""
    return {
        "count": page.count,
        "page_count": page.page_count,
        "next": page_url(request, page.number + 1) if page.has_next else None,
        "previous": page_url(request, page.number - 1) if page.has_previous else None,
        "content": [
            resource_envelope(request, resource_type, obj) for obj in page.items
        ],
    }


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


def list_last_modified(
    request, resource_type, queryset, requirement: Requirement | Sequence
):
    """The list's Last-Modified: the newest ``modified_at`` of `queryset`
    (`readable_queryset`'s), or the newest time a resource left a context in
    which the requester may list every resource
    (`scrud_django.core.requirements.readable_removal` of `requirement`, read
    on the queryset's database), whichever is later; ``None`` for neither.
    The leaves must read a resource's contexts as stored (the module's
    note): the stamps are of those contexts. `requirement` is taken through
    `scrud_django.core.requirements.Requirement.of`: one that asks no
    grants, or with a leaf that may read others
    (`scrud_django.core.requirements.require_stored_contexts`), is
    ``ImproperlyConfigured``."""
    requirement = Requirement.of(requirement, caller="list_last_modified")
    require_stored_contexts(requirement, "list_last_modified")
    newest = queryset.aggregate(newest=Max("modified_at"))["newest"]
    removed = readable_removal(request, requirement, resource_type, queryset.db)
    return latest(newest, removed)


def list_validators(
    request, resource_type, queryset, page: ListPage, requirement
) -> tuple[str, datetime | None]:
    """The ``(etag, last_modified)`` of the list `request` (a GET or HEAD, or
    the GET copy of a POST, `as_get`) gets: `list_etag` of `page` (of
    `queryset`, the list) and `request`'s query parameters, and
    `list_last_modified` of `queryset` with `requirement`."""
    etag = list_etag(
        resource_type,
        items=page.items,
        count=page.count,
        query=sorted(request.GET.lists()),
    )
    return etag, list_last_modified(request, resource_type, queryset, requirement)


def create_list_validators(
    request,
    resource_type,
    *,
    requirement: Requirement | Sequence,
    ignored: frozenset[str] = frozenset(),
    using: str | None = None,
) -> tuple[str, datetime | None]:
    """The list's validators (`list_validators`) against which the
    preconditions of POST `request` to the list are evaluated
    (`evaluate_list_preconditions`): of the list as the requester would GET
    it from the POST's URL (its query parameters filtering and paging it,
    `ignored` as `list_queryset` takes it), read on database `using` (the
    create's, under its `scrud_django.core.writes.conditional_create_lock`),
    as the DRF route reads them (``ListValidatorsMixin.create_list_validators``):
    `readable_queryset` of the POST, which lists as its GET copy without the
    list grant (the create's own request check governs it). A page that is
    not one of the list's raises ``django.core.paginator.InvalidPage`` (404
    on DRF), as the GET would."""
    queryset = readable_queryset(request, resource_type, requirement, using=using)
    queryset = _listed(queryset, resource_type, request.GET, ignored)
    page = requested_page(queryset, request.GET)
    return list_validators(as_get(request), resource_type, queryset, page, requirement)


def evaluate_list_read_preconditions(
    method: str,
    headers,
    *,
    etag: str,
    last_modified: datetime | None,
) -> ConditionalResult | None:
    """The failed preconditions of a GET or HEAD of a list, or ``None`` to
    go on, as scrudful evaluates them on the DRF list route: every
    precondition of RFC 9110 §13.2.2 (``If-Match``, ``If-Unmodified-Since``,
    ``If-None-Match``, ``If-Modified-Since``; a 304, a 412, or a 400 for a
    malformed one) with ``scrudful.conditional.evaluate_preconditions``,
    against the list's `etag` (unquoted) and `last_modified`
    (`list_validators`). Rendering the result is the caller's."""
    return evaluate_preconditions(
        method,
        headers,
        etag=quote_etag(etag),
        last_modified=http_timestamp(aware(last_modified)),
        require_precondition=True,
    )


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
