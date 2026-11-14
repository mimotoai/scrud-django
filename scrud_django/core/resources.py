"""A scrud resource without DRF: what a front end calls to serve a type's
list and items, with a Django request, the `ResourceType` and a neutral
requirement (`scrud_django.core.requirements`), as the DRF routes answer
(``ResourceViewMixin``): an item's lookup and object check (`load_for_read`),
whether it is readable (`object_readable`), its validators and Links, and
the ``Allow`` of an OPTIONS (`allowed_methods`). The list's and the writes'
functions are re-exported from `scrud_django.core.lists` and
`scrud_django.core.writes`.

A refusal of an object the requester cannot read is a 404 (``Http404``),
for every method: another answer would reveal that it exists. A refusal of
one they can read is ``access.AccessDenied`` (403), for the front end to
answer as it answers access errors.

The request check of a route (`check_request`: the list grant for a list's
GET, else ``has_permission`` of each entry of the requirement, a
`HookLeaf`'s included, after the others and outside the write's checks, with
the body for a write) runs before anything is looked up, as DRF's
``initial()`` asks the permissions before ``get_object()``: `load_for_read`,
`readable_queryset` (of a GET or HEAD) and `validate_write` ask it, so a
requester refused there gets a 403 whether the item exists or not. A front
end runs `scrud_django.core.bodies.check_body` on a write's body before any
of them, as DRF answers a malformed body 400 before any 403. Beyond that,
a requirement's `AnyOf` is DRF's ``|``: an option allows an object
only when each of its entries allows the request (``has_permission``) and
then the object, as DRF's ``OR.has_object_permission`` asks them
(`_object_permitted`; and the object checks of `readable_content` and
`readable_queryset`). A DRF view keeps its own checks
(``check_permissions``, ``check_object_permissions``), and its list and
readability ask an option's object checks alone.
"""

import copy
from collections.abc import Mapping, Sequence
from datetime import datetime

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from scoped_rbac import access
from scoped_rbac.policy import Permission

from scrud_django import collection_type_uri_for
from scrud_django.core.authz import _not_checking_write, content_of
from scrud_django.core.lists import (  # noqa: F401  (re-exported)
    content_filtered,
    evaluate_list_preconditions,
    list_etag,
    list_last_modified,
    readable_queryset,
)
from scrud_django.core.requirements import (
    AnyOf,
    GrantsLeaf,
    Requirement,
    _own_request_check,
    _trusted,
    as_get,
    asks_grants_itself,
    grants_allow,
    is_hook,
    option_allows_request,
    readable_by_grants,
    readable_by_object_checks,
    requirement_leaves,
)
from scrud_django.core.types import BODY_METHODS, HookLeaf, type_options
from scrud_django.core.workflow import authorizing_write
from scrud_django.core.writes import (  # noqa: F401  (re-exported)
    create,
    destroy,
    update,
    validate_write,
)
from scrud_django.headers import get_link_values_for
from scrud_django.models import Resource

# The method of an item whose object check the workflow check covers: the
# PUT of its ``update`` action (``ResourceViewMixin.checks_workflow``, whose
# ``WORKFLOW_VIEW_ACTIONS`` also covers the list's POST, ``create``, which
# loads no object). A PATCH (``partial_update``) and a DELETE (``destroy``)
# are not covered, locked writes (``LOCKED_WRITE_METHODS``) though they are.
COVERED_ITEM_METHOD = "PUT"

# The method of the list the workflow check covers: the POST of ``create``.
COVERED_LIST_METHOD = "POST"

# The methods an item's and a list's routes serve whose use OPTIONS checks
# (`allowed_methods`), in DRF's order (``APIView.allowed_methods``, from
# ``http_method_names``): ``scrud_django.registration.resource_type_urls``.
# HEAD is GET's answer; OPTIONS is always allowed.
ITEM_METHODS = ("GET", "PUT", "DELETE")
LIST_METHODS = ("GET", "POST")


def served_methods(resource_type, *, item: bool) -> tuple[str, ...]:
    """The methods the route of `resource_type` serves (`item`: its detail
    route, else its list): a computed type (``TypeOptions.compute``, by its
    IRI) serves ``GET`` only; any other `ITEM_METHODS` or `LIST_METHODS`."""
    if type_options(resource_type.type_uri).compute is not None:
        return ("GET",)
    return ITEM_METHODS if item else LIST_METHODS


# The order of the methods ``Allow`` lists, as scrudful's ``allow_value``
# orders them (its ``METHOD_KEYS``).
ALLOW_ORDER = ("GET", "PUT", "POST", "DELETE", "OPTIONS", "HEAD", "PATCH", "TRACE")


def _as_method(request, method):
    """`request` itself when its method is `method`, else a shallow copy of
    it whose method is (as `as_get` copies it)."""
    if request.method == method:
        return request
    clone = copy.copy(request)
    clone.method = method
    return clone


def _options_copy(request, method):
    """A shallow copy of `request` whose method is `method` and whose
    ``META["REQUEST_METHOD"]`` is ``"OPTIONS"``: the copy on which scrudful
    checks one method of an OPTIONS for ``Allow`` (``access.checks_allow``),
    whose PUT or POST is checked without a body (`allowed_methods`; and
    `load_for_read` given no body)."""
    clone = copy.copy(request)
    clone.method = method
    clone.META = {**request.META, "REQUEST_METHOD": "OPTIONS"}
    return clone


def readable_content(
    read, content, requirement: Requirement | Sequence, *, permission=None
):
    """Whether the resource's `content` is readable by `read` (a GET of the
    detail route): the grants (`readable_by_grants`, ``http.GET`` on each
    leaf's type; with `permission`, `grants_allow` for it instead) **and**
    the object checks a list asks (`readable_by_object_checks`, each
    ``AnyOf`` option asking its request checks of `read` first, as DRF's
    ``|`` answers a GET), with ``scrud_workflow`` refusing
    (`_not_checking_write`): it never makes an object readable.

    `requirement` is taken through `Requirement.of`: one that asks no
    grants (empty, or hooks only) is ``ImproperlyConfigured``: it would read
    as readable by anyone."""
    requirement = Requirement.of(requirement, caller="readable_content")
    return _readable_content(
        read,
        content,
        requirement,
        permission=permission,
        request_checks=True,
    )


def _readable_content(
    read, content, requirement, *, grants=True, permission=None, request_checks=False
):
    """`readable_content` without its guard: the DRF views'
    (``ListValidatorsMixin.object_readable``), whose requirement is empty or
    hooks only for a view with no ``IsAuthorized``, which then reads as
    readable, as scoped-rbac's ``is_readable`` has it (a refusal by its
    other permissions stays a 403). Their object checks ask no request
    check (`request_checks`, the facade's)."""
    with _not_checking_write():
        if grants:
            if permission is None:
                allowed = readable_by_grants(requirement, read, content)
            else:
                allowed = grants_allow(requirement, read, content, permission)
            if not allowed:
                return False
        return readable_by_object_checks(
            requirement, read, content, request_checks=request_checks
        )


def object_readable(
    request, resource_type, obj: Resource, requirement: Requirement | Sequence
) -> bool:
    """Whether `obj` (a `Resource` of `resource_type`) is readable by
    `request`'s user, as a GET of its detail route has it, whatever the
    request's method (`as_get`): `readable_content` of its content. Tells a
    404 from a 403 (`load_for_read`)."""
    return readable_content(as_get(request), content_of(obj), requirement)


def found_item(resource_type, obj: Resource) -> Resource:
    """`obj`, the item an item's lookup found, with `resource_type` (the
    type the front end read) cached on it when it is of it, so it is not
    read again; ``Http404`` for one stored in a context reserved once
    normalised (``access.stored_in_reserved_context``), as scoped-rbac
    answers it, before any check."""
    if obj.resource_type_id == resource_type.pk:
        Resource.resource_type.field.set_cached_value(obj, resource_type)
    if access.stored_in_reserved_context(obj):
        raise Http404
    return obj


def check_grants_in_each_context(request, content, requirement, permission) -> None:
    """Refuse (``access.AccessDenied``) the resource's `content` unless the
    grants allow `permission` on it, each context read as the resource is
    there (`grants_allow`), unless every leaf of `requirement` asks them so
    itself (`asks_grants_itself`: a leaf of a trusted class whose
    ``reads_each_context`` is true) or is a type's hooks (`is_hook`). Object
    checks only narrow; a leaf of any other class gets the grants asked
    beside it, whatever it says."""
    if all(
        asks_grants_itself(leaf) or is_hook(leaf)
        for leaf in requirement_leaves(requirement)
    ):
        return
    if not grants_allow(requirement, request, content, permission):
        raise access.AccessDenied()


def _object_permitted(request, resource_type, content, requirement, *, body=None):
    """Whether `requirement` allows `request` on the resource's `content`:
    every entry's ``has_object_permission``, an ``AnyOf`` allowing through
    any option each of whose entries allows the request first
    (``has_permission``, `option_allows_request`), as DRF's ``|`` asks
    them; then the grants in each context (`check_grants_in_each_context`).
    An option's request check reads `body` (the parsed body of a PUT or
    POST, as the route's request check, `check_request`, reads it); without
    one, a PUT or POST is checked as ``Allow`` checks it (`_options_copy`),
    as `load_for_read` checks it given none: whether the requester may write
    in some context, which does not pair an option's request check of the
    body with its object check as DRF's ``|`` does, so a write loaded
    without its body is paired again by `validate_write` (`update`), given
    the body. A leaf's ``PermissionDenied``
    refuses; its ``Http404`` is raised, as is any other access error of a
    request check (a body naming a reserved context, 400)."""
    permission = Permission(
        access.action_iri_for(request.method), resource_type.type_uri
    )
    if body is None and request.method in BODY_METHODS:
        asked = _options_copy(request, request.method)
    else:
        asked = request

    def meets(entry):
        if isinstance(entry, AnyOf):
            return any(
                option_allows_request(option, asked, body)
                and all(meets(e) for e in option)
                for option in entry.options
            )
        return entry.has_object_permission(request, content)

    try:
        if not all(meets(entry) for entry in requirement):
            return False
        check_grants_in_each_context(request, content, requirement, permission)
    except (PermissionDenied, access.AccessDenied):
        return False
    return True


def check_object(
    request,
    resource_type,
    obj: Resource,
    requirement: Requirement | Sequence,
    *,
    covered,
    body: Mapping | None = None,
):
    """The object check of `request` on `obj`, as the detail route asks it
    (``check_object_permissions``): run with ``scrud_workflow`` allowing when
    the workflow check covers the write (`covered`, `authorizing_write`). An
    ``AnyOf`` option asks its request checks first, with `body`
    (`_object_permitted`). A refusal of an object `object_readable` refuses
    is ``Http404``; of a readable one, ``access.AccessDenied``.
    `requirement` is taken through `Requirement.of`: one that asks no
    grants (empty, or hooks only) is ``ImproperlyConfigured``: it would
    allow anyone.

    It asks no top-level request check (DRF's ``check_permissions``): a
    caller runs `check_request` first, as `load_for_read` and
    `validate_write` do."""
    requirement = Requirement.of(requirement, caller="check_object")
    with authorizing_write(request, covered=covered):
        permitted = _object_permitted(
            request, resource_type, content_of(obj), requirement, body=body
        )
    if permitted:
        return
    if not object_readable(request, resource_type, obj, requirement):
        raise Http404
    raise access.AccessDenied()


def load_for_read(
    request,
    resource_type,
    pk,
    *,
    requirement: Requirement | Sequence,
    method: str,
    lock_using: str | None = None,
    body: Mapping | None = None,
) -> Resource:
    """The resource `pk` of `resource_type`, looked up and checked for a
    `method` request (every method's lookup, not only a read's), as the
    detail route answers it (``initial()``, then ``get_object()``). A front
    end given a body runs `scrud_django.core.bodies.check_body` on it
    first, as DRF answers a malformed body 400 before any 403:

    - first, before any lookup, the request check of the item's route for
      `method` (`check_request`), with `body`, the parsed body of a PUT (or
      of any method `BODY_METHODS` names): its refusal is
      ``access.AccessDenied`` whether `pk` exists or not, so that it tells
      nothing of it. Without a `body` a PUT is checked as ``Allow`` checks
      it (`allowed_methods`, on a body-less copy, `_options_copy`): whether
      the requester may PUT in some context; the body's contexts are then
      the caller's to check (`validate_write` asks them, then the post-save
      check), and so is pairing an ``AnyOf`` option's request check of the
      body with its object check, as DRF's ``|`` does (`validate_write`
      asks the object check again with the body, `check_object`);
    - missing, or a `pk` that is no key, it is ``Http404``; so is one stored
      in a context reserved once normalised (`found_item`, which caches the
      type on it). The lookup is not narrowed by readability, as the DRF
      route's is not (a resource's contexts are rows, not a column, so
      scoped-rbac's detail scope keeps it): the object check decides, which
      no ``SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]`` caps;
    - the object check for `method` (`check_object`) runs with
      ``scrud_workflow`` allowing for the PUT of the update action, which
      the workflow check covers (`COVERED_ITEM_METHOD`), and refusing for
      any other method;
    - a refusal of an unreadable object is ``Http404``, of a readable one
      ``access.AccessDenied``.

    With `lock_using`, the row is read on that database with
    ``select_for_update()`` (call it inside a transaction there); else as
    the router reads.

    `requirement` is taken through `Requirement.of`: one that asks no
    grants (empty, or hooks only) is ``ImproperlyConfigured``, before any
    lookup: it would let anyone load any resource."""
    requirement = Requirement.of(requirement, caller="load_for_read")
    if body is None and method in BODY_METHODS:
        checked = _options_copy(request, method)
    else:
        checked = _as_method(request, method)
    check_request(checked, resource_type, requirement, list_route=False, body=body)
    queryset = Resource.objects.filter(resource_type=resource_type)
    if lock_using is not None:
        queryset = queryset.using(lock_using).select_for_update()
    try:
        obj = queryset.get(pk=pk)
    except (Resource.DoesNotExist, TypeError, ValueError, ValidationError):
        # DRF's get_object_or_404: a pk that is no key is missing too.
        raise Http404 from None
    found_item(resource_type, obj)
    check_object(
        _as_method(request, method),
        resource_type,
        obj,
        requirement,
        covered=method == COVERED_ITEM_METHOD,
        body=body,
    )
    return obj


def item_validators(obj: Resource) -> tuple[str, datetime]:
    """`obj`'s ``(etag, last_modified)``: its ``etag`` (unquoted) and
    ``modified_at``, as ``ScrudfulMeta`` gives scrudful."""
    return (obj.etag, obj.modified_at)


def item_links(request, resource_type) -> list[str]:
    """The Link values of a resource of `resource_type` that ``ScrudfulMeta``
    produces (`scrud_django.headers.get_link_values_for`): its schema
    (``rel="describedby"``) and its JSON-LD context, each absolute against
    `request`, either left out when the type has none."""
    return get_link_values_for(resource_type, request)


def _request_checks_pass(request, requirement, body=None):
    """Whether every entry of `requirement` (any option of an ``AnyOf``)
    allows ``has_permission(request, body)`` (`body` ``None``: a request
    check without a body). ``PermissionDenied`` refuses; any other error (an
    access error of the body, a 400) is raised.

    A type's hooks (a `HookLeaf`, of any subclass) are asked after the other
    entries, and outside the block the caller runs it in
    (`_not_checking_write`: ``scrud_workflow`` refuses there), as the DRF
    route asks them (``check_request_hooks``) once its own request check,
    run in ``write_checks``, is over."""

    def meets(entry):
        if isinstance(entry, AnyOf):
            return any(all(meets(e) for e in option) for option in entry.options)
        if isinstance(entry, HookLeaf):
            with _not_checking_write():
                return entry.has_permission(request, body)
        return entry.has_permission(request, body)

    hooks = [entry for entry in requirement if isinstance(entry, HookLeaf)]
    others = [entry for entry in requirement if not isinstance(entry, HookLeaf)]
    try:
        return all(meets(entry) for entry in others + hooks)
    except PermissionDenied:
        return False


def _as_list_requirement(requirement):
    """`requirement` as a list's GET asks its request checks: each leaf
    trusted to ask the grants (`_trusted`) as a `GrantsLeaf` of its type's
    list type (``collection_type_uri_for``), as DRF's ``IsAuthorized``
    names the collection type on the ``list`` action, and not its item
    type; any other entry (a type's hooks) as it is, ``AnyOf`` options
    included. A trusted leaf with a request check of its own
    (`_own_request_check`) is asked as it is too, beside the list grant:
    what it refuses beyond the grants stays refused."""

    def as_list(entry):
        if isinstance(entry, AnyOf):
            return [
                AnyOf(
                    tuple(
                        tuple(e for item in option for e in as_list(item))
                        for option in entry.options
                    )
                )
            ]
        if not _trusted(entry):
            return [entry]
        listed = GrantsLeaf(collection_type_uri_for(entry.type_iri))
        return [listed, entry] if _own_request_check(entry) else [listed]

    # Derived from a checked requirement: each trusted leaf stays one.
    return Requirement.unchecked(
        [e for entry in Requirement.unchecked(requirement) for e in as_list(entry)]
    )


def check_request(
    request,
    resource_type,
    requirement: Requirement | Sequence,
    *,
    list_route: bool,
    body: Mapping | None = None,
) -> None:
    """The request check of `request` on `resource_type`'s list
    (`list_route`) or item route, as the DRF route asks it before it looks
    anything up (``check_permissions``): ``access.AccessDenied`` unless

    - for a GET or HEAD of the list, every entry of `requirement` (any
      option of an ``AnyOf``) as the list asks it (`_as_list_requirement`:
      each grants leaf as a `GrantsLeaf` of its list type,
      ``collection_type_uri_for``, not its item type, as DRF names the
      collection type on the ``list`` action) allows ``has_permission`` of
      a GET copy (`as_get`) without a body: the list grant. A requester
      with the list grant and no item grant lists, an empty list once the
      item grants filter it, as on DRF;
    - for any other request (an item's, whatever its method; a POST to the
      list, a create), every entry of `requirement` allows
      ``has_permission(request, body)``: `body` is the parsed body, whose
      contexts a PUT or POST is checked in (``None`` is checked as a body
      that writes none, in the default context ``""``, unless `request` is
      the body-less copy ``Allow`` checks, ``access.checks_allow``).

    The front end runs `scrud_django.core.bodies.check_body` on a write's
    body first: DRF answers a malformed body 400 before any 403.

    It runs with ``scrud_workflow`` allowing for the writes the workflow
    check covers (a POST to the list, `COVERED_LIST_METHOD`, and an item's
    PUT, `COVERED_ITEM_METHOD`) and refusing for any other, as the DRF
    route's ``write_checks``; but a type's hooks (each `HookLeaf`) are asked
    after the other entries and outside that block, where it refuses, as
    the DRF route asks them (``check_request_hooks``) once its
    ``write_checks`` is over (`_request_checks_pass`). A leaf's
    ``PermissionDenied`` refuses; another access error (a body naming a
    reserved context, 400) is raised.

    `requirement` is taken through `Requirement.of`: one that asks no
    grants (empty, or hooks only) is ``ImproperlyConfigured``: it would
    allow anyone."""
    requirement = Requirement.of(requirement, caller="check_request")
    if list_route and request.method in ("GET", "HEAD"):
        read = as_get(request)
        with authorizing_write(read, covered=False):
            allowed = _request_checks_pass(read, _as_list_requirement(requirement))
    else:
        covered = COVERED_LIST_METHOD if list_route else COVERED_ITEM_METHOD
        with authorizing_write(request, covered=request.method == covered):
            allowed = _request_checks_pass(request, requirement, body)
    if not allowed:
        raise access.AccessDenied()


def allowed_methods(
    request,
    resource_type,
    obj: Resource | None,
    requirement: Requirement | Sequence,
    *,
    list_requirement: Requirement | Sequence | None = None,
    methods: Sequence[str] | None = None,
) -> list[str]:
    """The methods the ``Allow`` of the OPTIONS `request` lists, as the DRF
    route's (scrudful's ``_allowed_methods`` over ``ResourceViewMixin``)
    lists them for its requester, in its order (`ALLOW_ORDER`): of the item
    `obj` (a `Resource` of `resource_type`, as its lookup found it), or of
    the list for ``None``.

    1. Each method the route serves (`methods`, by default
       `served_methods`: GET alone for a computed type) is
       checked with the request checks (``has_permission``) of `requirement`
       on a body-less copy of `request` with that method (`_options_copy`),
       ``scrud_workflow`` allowing for the PUT of an item and the POST of
       the list (the workflow check covers them). They name the item's type,
       as the DRF route's do, but for the list's GET, whose are those of
       `list_requirement` (by default a `GrantsLeaf` of the type's list
       type, ``collection_type_uri_for``): the DRF route names the list's
       type for its ``list`` action only.
    2. Only when one passed, for an item: the object is as the first method
       that passed loads it (``Http404`` for one stored in a reserved
       context, `found_item`), and each method that passed is checked
       against it (`check_object`, covered as in `load_for_read`). The first
       one's ``Http404`` (an object it cannot read) is raised: it is the
       OPTIONS's answer, as a GET's would be. Another's refuses only it, as
       does any ``access.AccessDenied``.

    `methods` are upper-cased and checked in `ALLOW_ORDER` (a method not
    in it after those, in the order given), so the answer does not depend on
    the caller's order or case.

    OPTIONS is always listed, and HEAD with GET. The item is given already
    loaded: on the DRF route a requester refused every method gets ``Allow:
    OPTIONS`` whether the object exists or not (nothing is loaded then),
    which a caller that answers a missing item with a 404 before calling
    this does not reproduce.

    `requirement` (and a given `list_requirement`) is taken through
    `Requirement.of`: one that asks no grants (empty, or hooks only) is
    ``ImproperlyConfigured``: it would list every method for anyone."""
    requirement = Requirement.of(requirement, caller="allowed_methods")
    if list_requirement is not None:
        list_requirement = Requirement.of(list_requirement, caller="allowed_methods")
    given = (
        methods
        if methods is not None
        else served_methods(resource_type, item=obj is not None)
    )
    upper = dict.fromkeys(method.upper() for method in given)
    checked = tuple(
        [method for method in ALLOW_ORDER if method in upper]
        + [method for method in upper if method not in ALLOW_ORDER]
    )
    if obj is None:
        covered = COVERED_LIST_METHOD
        if list_requirement is None:
            list_requirement = Requirement.of(
                [GrantsLeaf(collection_type_uri_for(resource_type.type_uri))],
                caller="allowed_methods",
            )
        requirements = {"GET": list_requirement}
    else:
        covered = COVERED_ITEM_METHOD
        requirements = {}
    copies = {method: _options_copy(request, method) for method in checked}
    allowed = {}
    for method in checked:
        with authorizing_write(copies[method], covered=method == covered):
            allowed[method] = _request_checks_pass(
                copies[method], requirements.get(method, requirement)
            )
    loader = next((method for method in checked if allowed[method]), None)
    if obj is not None and loader is not None:
        found_item(resource_type, obj)
        for method in checked:
            if not allowed[method]:
                continue
            try:
                check_object(
                    copies[method],
                    resource_type,
                    obj,
                    requirement,
                    covered=method == COVERED_ITEM_METHOD,
                )
            except Http404:
                if method == loader:
                    raise
                allowed[method] = False
            except access.AccessDenied:
                allowed[method] = False
    allowed["HEAD"] = allowed.get("GET", False)
    allowed["OPTIONS"] = True
    return [method for method in ALLOW_ORDER if allowed.get(method)]
