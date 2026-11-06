import hashlib
import inspect
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from urllib.parse import urlencode

import reversion
from django.core.exceptions import FieldError, ImproperlyConfigured, ValidationError
from django.db import router, transaction
from django.db.models import Max, ProtectedError
from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import NotAuthenticated, PermissionDenied
from rest_framework.generics import get_object_or_404
from rest_framework.request import clone_request
from rest_framework.response import Response
from rest_framework.reverse import reverse
from scoped_rbac import access, problems
from scoped_rbac.conditional import lock_collection
from scoped_rbac.filters import RbacFilter
from scoped_rbac.permissions import AccessAPIError, drf_error, view_permission
from scoped_rbac.rest import AccessControlledAPIView
from scrudful._django import aware, conditional_response, precondition_headers
from scrudful.conditional import (
    evaluate_preconditions,
    http_timestamp,
    parse_etag_list,
    parse_http_date,
    quote_etag,
)
from scrudful.rest_framework import ScrudfulViewFunc

from scrud_django.exceptions import (
    RbacContextInvalidError,
    RequestBodyMissingError,
    RequestBodyNulError,
    RequestBodyTooDeepError,
    ResourceInUseError,
)
from scrud_django.filters import (
    as_detail_read,
    filter_by_object_checks,
    grants_allow,
    object_check_requirement,
    readable_by_grants,
    readable_by_object_checks,
    readable_removal,
)
from scrud_django.headers import get_context_uri_for, get_schema_uri_for
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models import Resource
from scrud_django.models.resource import MAX_CONTEXT_BYTES, context_fits
from scrud_django.paginations import StandardResultsSetPagination
from scrud_django.permissions import (
    _checking_write,
    _not_checking_write,
    _workflow_authorized,
    checks_read_content,
    content_of,
    may_act_in_context,
    reading_content,
    reads_each_context,
)
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    lock_if_workflow_policy,
    type_db,
    type_locked,
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


# The preconditions a POST to a list evaluates (`list_precondition_response`).
LIST_PRECONDITIONS = ("if-match", "if-none-match", "if-unmodified-since")

# What a view records of the conditional POST it serves (`LockedWriteMixin`),
# forgotten when it serves another: the list's validators read
# (`create_list_validators`), whether the conditional create's lock is held,
# and whether its preconditions were evaluated.
_CREATE_STATE = (
    "_scrud_create_validators",
    "_scrud_create_locked",
    "_scrud_list_preconditions_evaluated",
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


# The methods whose body is the resource to store.
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

# The methods of a resource view whose write the workflow check
# (``WorkflowValidator``) covers.
WORKFLOW_METHODS = frozenset({"POST", "PUT"})


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


# The deepest a POST, PUT or PATCH body may be nested (objects and arrays,
# the outermost one counted): far deeper than resources need, far below what
# Python code walking it recursively can follow (about 800 levels), and as
# deep as MySQL's JSON type stores.
MAX_BODY_DEPTH = 100

# The deepest MariaDB stores a resource's content: its JSON column's
# ``JSON_VALID`` check refuses a document nested 32 levels or more.
MARIADB_MAX_BODY_DEPTH = 31


def max_body_depth():
    """The deepest a body may be nested: `MAX_BODY_DEPTH`, or
    `MARIADB_MAX_BODY_DEPTH` when resources are written to MariaDB
    (``router.db_for_write(Resource)``), which could not store a deeper one
    (a 500)."""
    from django.db import connections

    connection = connections[router.db_for_write(Resource)]
    if connection.vendor == "mysql" and connection.mysql_is_mariadb:
        return MARIADB_MAX_BODY_DEPTH
    return MAX_BODY_DEPTH


def body_depth(data, limit=None):
    """How deeply `data` (a parsed JSON body) nests objects and arrays (0 for
    a scalar), counted without recursion; past `limit`, ``limit + 1``."""
    deepest, stack = 0, [(data, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, Mapping):
            items = node.values()
        elif isinstance(node, (list, tuple)):
            items = node
        else:
            continue
        deepest = max(deepest, depth)
        if limit is not None and deepest > limit:
            return deepest
        stack.extend((item, depth + 1) for item in items)
    return deepest


def check_body_depth(request):
    """Raise `RequestBodyTooDeepError` (400) for a POST, PUT or PATCH body
    nested deeper than `max_body_depth`."""
    if request.method not in BODY_METHODS:
        return
    limit = max_body_depth()
    if body_depth(request.data, limit) > limit:
        raise RequestBodyTooDeepError(limit)


def body_has_nul(data):
    """Whether a string of `data` (a parsed JSON body), a key or a value at
    any depth, holds a NUL character; walked without recursion."""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            if "\x00" in node:
                return True
        elif isinstance(node, Mapping):
            stack.extend(node.keys())
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return False


def check_body_nul(request):
    """Raise `RequestBodyNulError` (400) for a POST, PUT or PATCH body with a
    NUL character in any string (`body_has_nul`): PostgreSQL's ``jsonb``
    cannot hold one, so it is refused on every database alike."""
    if request.method not in BODY_METHODS:
        return
    if body_has_nul(request.data):
        raise RequestBodyNulError()


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
def _collection_locked(model, using):
    """scoped-rbac's ``lock_collection(model, using)`` as a context manager."""
    lock_collection(model, using)
    yield


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

    # Link callbacks also run for create, where there is no slug, and when
    # scrudful builds the HSR, with ``request`` None (they then give paths,
    # which scrudful resolves against ``SCRUDFUL["BASE_URL"]``; on a response
    # they are absolute, from the request): they take ``**kwargs``.
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
    # write to an object they cannot read changes neither. scrudful (0.9)
    # also evaluates a create's preconditions against them: for a POST they
    # are the list as the requester would GET it
    # (`ListValidatorsMixin.create_list_validators`).
    def list_etag_func(view_instance, request, **kwargs):
        if request.method == "POST":
            return view_instance.create_list_validators(request)[0]
        return view_instance.list_etag(request)

    def list_last_modified_func(view_instance, request, **kwargs):
        if request.method == "POST":
            return view_instance.create_list_validators(request)[1]
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
    """PUT, PATCH and DELETE in one transaction with the row locked, and a
    conditional POST to the list in one transaction too: for
    `ResourceViewMixin` and `GenericViewMixin` views.

    `dispatch` runs the whole request in one transaction on
    `locked_write_database`: DRF's permission check, the lookup with the row
    locked (`locked`, which the view applies to the queryset it looks the
    object up in), scrudful's precondition evaluation (If-Match,
    If-Unmodified-Since) against that row, the object check and the write of
    that same instance. A concurrent write of the row waits for it to
    commit, and then evaluates its own preconditions against what it
    committed. A response that is not a success (an error, a 412) rolls the
    transaction back.

    A POST to the list (`is_list_post`: ``create`` or an
    ``@action(detail=False)``) carrying ``If-Match``, ``If-None-Match`` or
    ``If-Unmodified-Since`` runs in one transaction on
    `locked_write_database` too. When its preconditions need the list's
    validators (`is_conditional_create`), conditional creates run one at a
    time: once DRF's ``initial`` has authenticated, authorized and throttled
    it, `conditional_create_lock` is taken, and held until the response;
    the preconditions are then evaluated against the list
    (`ListValidatorsMixin.create_list_validators`: by scrudful before the
    viewset's ``create``, or by `ListValidatorsMixin.list_precondition_response`,
    which a POST ``@action`` calls first) and the create runs. A response
    that is not a success rolls it back; so does one whose preconditions
    nothing evaluated (``ImproperlyConfigured``), as from an ``@action``
    that writes without calling `list_precondition_response` (or a
    ``create`` that calls it).

    Other requests are not wrapped. A subclass overriding ``dispatch`` or
    ``initial`` must call ``super()``: without it, a conditional create's
    preconditions are refused (``ImproperlyConfigured``) rather than
    evaluated unlocked."""

    # The database of the request's locked write transaction (`dispatch`), or
    # ``None`` outside one.
    locked_write_db: str | None = None

    # The database of the transaction of a POST to the list carrying a
    # precondition (`dispatch`), or ``None`` outside one.
    conditional_create_db: str | None = None

    def locked_write_database(self):
        """The database a locked write's transaction opens on."""
        raise NotImplementedError()

    def is_list_post(self, request):
        """Whether `request` is a POST this viewset answers on its list route:
        with ``create``, or with an ``@action(detail=False)`` (its own
        handler, or one ``@<action>.mapping.post`` adds). The router tells
        (``detail``, which it gives each route's view); in a view built
        without one (``as_view`` with its actions), the handler the POST is
        mapped to: ``create``, or that of an extra action of the viewset
        (``get_extra_actions``) whose ``mapping`` maps its POST there."""
        if request.method != "POST":
            return False
        detail = getattr(self, "detail", None)
        if detail is not None:
            return detail is False
        handler = (getattr(self, "action_map", None) or {}).get("post")
        if handler == "create":
            return True
        extra_actions = getattr(type(self), "get_extra_actions", None)
        for extra in extra_actions() if handler and extra_actions else ():
            # MethodMapper's own get is the @<action>.mapping.get decorator.
            if dict.get(extra.mapping, "post") == handler:
                return extra.detail is False
        return False

    def is_conditional_create(self, request):
        """Whether `request` is a POST to the list (`is_list_post`) carrying
        a precondition that needs the list's validators
        (`list_validators_needed`; ``If-Match: *`` and ``If-None-Match: *``
        alone do not)."""
        return self.is_list_post(request) and list_validators_needed(
            precondition_headers(request)
        )

    def conditional_create_lock(self, using):
        """A context manager taking the lock every conditional create of this
        view's collection takes, on database `using`, inside the create's
        transaction."""
        raise NotImplementedError()

    def dispatch(self, request, *args, **kwargs):
        # A view instance serves one request: forget what a reused one
        # recorded of its last (`create_list_validators`).
        for name in _CREATE_STATE:
            self.__dict__.pop(name, None)
        if request.method in LOCKED_WRITE_METHODS:
            attribute = "locked_write_db"
        elif self.is_list_post(request) and has_list_preconditions(
            precondition_headers(request)
        ):
            attribute = "conditional_create_db"
        else:
            return super().dispatch(request, *args, **kwargs)
        db = self.locked_write_database()
        with transaction.atomic(using=db), ExitStack() as locks:
            setattr(self, attribute, db)
            self._scrud_create_locks = locks
            try:
                response = super().dispatch(request, *args, **kwargs)
            finally:
                setattr(self, attribute, None)
                self._scrud_create_locks = None
            if not 200 <= response.status_code < 300:
                transaction.set_rollback(True, using=db)
            elif attribute == "conditional_create_db" and not self.__dict__.get(
                "_scrud_list_preconditions_evaluated"
            ):
                # Raised inside the transaction: what the handler wrote is
                # rolled back.
                raise ImproperlyConfigured(
                    f"{type(self).__name__} answered a POST to its list with "
                    f"{response.status_code} without evaluating its "
                    "preconditions: an @action(detail=False) serving a POST "
                    "must first call list_precondition_response (or a create "
                    "that does)."
                )
        return response

    def initial(self, request, *args, **kwargs):
        """Refuse a request rewritten from an OPTIONS
        (``scoped_rbac.access.refuse_rewritten_options``: 403, 401 for the
        anonymous; middleware rewriting an OPTIONS's ``request.method``, say
        from ``X-HTTP-Method-Override``), which the body checks would take
        for scrudful's copy of an OPTIONS and skip
        (``ResourceViewMixin.check_permissions``): here, and not only in
        scoped-rbac's ``AccessControlledAPIView.initial``, so that it is
        refused whatever the order of the view's bases. scrudful checks
        ``Allow`` on its copies without calling ``initial``. Then DRF's
        (authentication, permissions, throttles), then, for a conditional
        create (`is_conditional_create`), its lock
        (`conditional_create_lock`), held until `dispatch`'s transaction
        ends: after the request is authorized, so a refused one never waits
        for it or holds it."""
        try:
            access.refuse_rewritten_options(request)
        except access.AccessError as exc:
            raise drf_error(exc, request) from exc
        super().initial(request, *args, **kwargs)
        if self.conditional_create_db is not None and self.is_conditional_create(
            request
        ):
            self._scrud_create_locks.enter_context(
                self.conditional_create_lock(self.conditional_create_db)
            )
            self._scrud_create_locked = True

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
    `object_for_checks`), so that it shows what the detail route shows as far
    as ``IsAuthorized`` permissions decide. Another ``BasePermission`` of the
    view is not asked for lists, as in DRF: to make its rule narrow a list,
    move it into an ``IsAuthorized`` subclass or apply it in a filter
    backend (or ``get_queryset``)."""

    # True while ``get_object()`` runs: its lookup is not a list.
    _scrud_in_get_object = False

    # The database the list's validators are read on (`as_list_read`), or
    # ``None`` to read as the router reads.
    list_read_db: str | None = None

    def object_readable(self, request, obj):
        """Whether `obj` (the object as the object check gets it) is
        readable by `request`'s user, which tells a 404 from a 403 on the
        detail route (`check_object_permissions`) and decides whether a
        workflow error names a resource's stored state: as a GET of the
        detail route has it (`scrud_django.filters.as_detail_read`), whatever
        the request's method, the grants (`readable_by_grants`, scoped-rbac's
        ``is_readable``: ``http.GET`` in the object's contexts, as each
        ``IsAuthorized`` reads them, each with the object as it is in that
        context) **and**
        the object checks a list asks (`readable_by_object_checks`), so that
        the detail route hides what the list leaves out. The object checks
        only narrow. A grant conditioned on ``scrud_workflow`` never makes
        an object readable."""
        with _not_checking_write(), checks_read_content(self):
            if isinstance(self, AccessControlledAPIView):
                with as_detail_read(self, request) as read:
                    if not readable_by_grants(self, read, obj):
                        return False
            return readable_by_object_checks(self, request, obj)

    def is_readable(self, request, obj):
        """`object_readable`. scoped-rbac's ``AccessControlledAPIView``
        defines its own, which reads a resource's stored ``rbac_context``
        whole: a view composing it before this mixin puts `ReadableMixin`
        first, as the scrud viewsets do, so that its
        ``check_object_permissions`` asks this one."""
        return self.object_readable(request, obj)

    def check_object_permissions(self, request, obj):
        """DRF's object check, except that a refusal (403, or 401 for the
        anonymous user) of an object `object_readable` refuses is a 404:
        another answer would reveal that it exists. scoped-rbac's
        ``AccessControlledAPIView.check_object_permissions`` (which reads
        only the grants) then lets the 404 through."""
        try:
            super().check_object_permissions(request, obj)
            self.check_grants_in_each_context(request, obj)
        except (PermissionDenied, NotAuthenticated):
            if not self.object_readable(request, obj):
                raise Http404 from None
            raise

    def check_grants_in_each_context(self, request, obj):
        """Refuse (``permission_denied``) the resource `obj` (its content)
        unless the grants allow the request's action on it, each context read
        as the resource is there (`grants_allow`), when an ``IsAuthorized``
        of the view has a ``has_context_permission`` of its own
        (`reads_each_context` is false): that code, and its ``super()``
        call, read the content as stored, so the grants are asked here too,
        as a list asks them before its object checks. Object checks only
        narrow. A view whose ``IsAuthorized`` permissions keep scoped-rbac's
        ``has_context_permission`` asks the grants so already; a model's
        instance (`GenericViewMixin`) is left to its object checks."""
        get_rbac_permissions = getattr(self, "get_rbac_permissions", None)
        if (
            not isinstance(obj, Mapping)
            or not callable(get_rbac_permissions)
            or not isinstance(self, AccessControlledAPIView)
        ):
            return
        with checks_read_content(self):
            if all(map(reads_each_context, reading_content(get_rbac_permissions()))):
                return
            if not grants_allow(self, request, obj, view_permission(request, self)):
                self.permission_denied(request)

    def object_for_checks(self, obj):
        """`obj` as the detail route gives it to the object check: itself
        (`ResourceViewMixin` gives the resource's content)."""
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
            queryset = self.get_queryset()
            if self.list_read_db is not None:
                queryset = queryset.using(self.list_read_db)
            queryset = self.filter_queryset(queryset)
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

    @contextmanager
    def as_list_read(self, request, using=None):
        """The view as a GET of its list route has it while the block runs:
        its ``request`` a copy of `request` whose method is GET (which the
        block gets) and its ``action`` ``"list"``, with nothing of the list
        computed yet (`list_queryset`, `list_page`), read on database
        `using` (`list_read_db`; ``None``: as the router reads), without
        locking anything. All restored, and what the block computed
        forgotten, when it ends."""
        cached = ("_scrud_list_queryset", "_scrud_list_page")
        missing = object()
        saved = {
            name: self.__dict__.get(name, missing)
            for name in ("request", "action", "list_read_db", *cached)
        }
        for name in cached:
            self.__dict__.pop(name, None)
        read = clone_request(request, "GET")
        self.request, self.action, self.list_read_db = read, "list", using
        try:
            yield read
        finally:
            for name, value in saved.items():
                if value is missing:
                    self.__dict__.pop(name, None)
                else:
                    setattr(self, name, value)

    def create_list_validators(self, request):
        """The list's ``(etag, last_modified)`` against which scrudful (0.9)
        evaluates the preconditions of a POST to it (``Meta.list_etag_func``
        and ``Meta.list_last_modified_func`` call it for a POST): RFC 9110
        evaluates them against the target, the list, as the requester would
        GET it from this URL (`list_etag` and `list_last_modified` under
        `as_list_read`, read once per request). They are computed only when
        a precondition needs them (`list_validators_needed`), else ``(None,
        None)``: ``If-Match: *`` passes and ``If-None-Match: *`` fails on the
        list existing, which it always does. Computing them is a GET of the
        list: past ``SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]`` it is that
        GET's 400 (``conditional_list_too_large``). They are read on the
        conditional create's database, under its lock
        (`LockedWriteMixin.dispatch`), so the create is evaluated against the
        list the previous one left: needed without that lock (a POST that is
        not a conditional create, `LockedWriteMixin.is_conditional_create`,
        such as one to a view that is no viewset, or a ``dispatch`` or
        ``initial`` that skips ``super()``), they are refused
        (``ImproperlyConfigured``) rather than evaluated unlocked. Calling it
        records that the request's preconditions were evaluated
        (`LockedWriteMixin.dispatch` refuses a conditional POST's success
        without)."""
        self._scrud_list_preconditions_evaluated = True
        validators = self.__dict__.get("_scrud_create_validators")
        if validators is None:
            validators = (None, None)
            if list_validators_needed(precondition_headers(request)):
                if self.conditional_create_db is None or not self.__dict__.get(
                    "_scrud_create_locked"
                ):
                    raise ImproperlyConfigured(
                        f"{type(self).__name__} evaluates a conditional POST "
                        "against its list outside a conditional create's "
                        "locked transaction: serve the POST with a viewset's "
                        "create or an @action(detail=False) "
                        "(LockedWriteMixin.is_conditional_create), and call "
                        "super() in dispatch and initial."
                    )
                with self.as_list_read(request, self.conditional_create_db) as read:
                    validators = (self.list_etag(read), self.list_last_modified())
            self._scrud_create_validators = validators
        return validators

    def list_precondition_response(self, request, using=None):
        """The response to a POST to the list whose preconditions fail, or
        ``None`` to go on: ``If-Match``, else ``If-Unmodified-Since``, then
        ``If-None-Match`` (``*`` fails: the list exists), with
        ``scrudful.conditional.evaluate_preconditions``, against the list as
        the requester would GET it from this URL. A failed one is scrudful's
        412, a malformed one its 400, rendered as the view's ``create`` (else
        ``list``) handler renders its own (problem details while
        ``SCOPED_RBAC["UNIFORM_ERRORS"]`` is on). The validators are only
        computed when a precondition needs them (`list_validators_needed`);
        past ``SCOPED_RBAC["CONDITIONAL_LIST_MAX_ROWS"]`` it is that GET's
        400 (``conditional_list_too_large``).

        scrudful evaluates the same preconditions before a viewset's
        ``create`` decorated by ``scrudful_viewset`` runs, under the
        conditional create's lock (`create_list_validators`), and calling it
        there again gives ``None`` (the validators are read once). It does
        not evaluate them for a POST ``@action(detail=False)``, nor for the
        ``create`` such an action calls (scrudful evaluates only the outer
        of two nested handlers): call it first there, or in that ``create``,
        as ``ResourceViewMixin.create`` does. The action is a conditional
        create too (`LockedWriteMixin`): it reads the list under that
        create's lock, and a success whose preconditions nothing evaluated
        is refused (``ImproperlyConfigured``). Elsewhere it reads the list on
        database `using` (``None``: as the router reads) and does not hold
        it: to hold it, call it with `using` inside a transaction there that
        first locks a row every such create locks, and create in that
        transaction."""
        headers = precondition_headers(request)
        if not has_list_preconditions(headers):
            return None
        self._scrud_list_preconditions_evaluated = True
        if self.conditional_create_db is not None and using in (
            None,
            self.conditional_create_db,
        ):
            etag, last_modified = self.create_list_validators(request)
        elif list_validators_needed(headers):
            with self.as_list_read(request, using) as read:
                etag, last_modified = self.list_etag(read), self.list_last_modified()
        else:
            etag = last_modified = None
        result = evaluate_preconditions(
            request.method,
            headers,
            etag=None if etag is None else quote_etag(etag),
            last_modified=http_timestamp(aware(last_modified)),
            require_precondition=False,
            exists=True,
        )
        if result is None:
            return None
        # Rendered as scrudful renders a create's own 400 and 412 (the
        # handler's error entity and body function).
        for name in ("create", "list"):
            handler = inspect.getattr_static(type(self), name, None)
            if isinstance(handler, ScrudfulViewFunc):
                return handler._early_response(
                    result, request, (self, request, *self.args), dict(self.kwargs)
                )
        return conditional_response(result)


class ReadableMixin:
    """Put first in a view composing scoped-rbac's ``AccessControlledAPIView``
    with `ResourceViewMixin` (or `GenericViewMixin`), as the scrud viewsets
    do: ``AccessControlledAPIView.check_object_permissions`` then asks
    `ListValidatorsMixin.object_readable` whether a refused object is a 403
    or a 404, which reads each of a resource's contexts with the resource as
    it is there, rather than its own ``is_readable``."""

    def is_readable(self, request, obj):
        return self.object_readable(request, obj)


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

    def checks_workflow(self, request):
        """Whether `request` is a write the workflow check
        (``WorkflowValidator``) covers: a POST or PUT the view serves."""
        return request.method in WORKFLOW_METHODS and (
            request.method in self.allowed_methods
        )

    def write_checks(self, request, authorized=False):
        """The block in which a check of `request` runs: for a write the
        workflow check covers (`checks_workflow`), with the ``scrud_workflow``
        operator allowing (the workflow check decides the transition); with
        `authorized` (the post-save check), only once the workflow check has
        authorized the write. Anywhere else ``scrud_workflow`` refuses."""
        if self.checks_workflow(request) and (
            not authorized or _workflow_authorized(request)
        ):
            return _checking_write()
        return _not_checking_write()

    def check_object_permissions(self, request, obj):
        """DRF's object check, with ``scrud_workflow`` allowing for a PUT the
        workflow check covers and refusing for anything else
        (`write_checks`). Whether a refused resource is readable (404 or
        403) is asked outside it (`object_readable`)."""
        with self.write_checks(request):
            super().check_object_permissions(request, obj)

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
        given the resource's content (`object_for_checks`), the ``dict`` the
        resource holds: ``check_object_permissions``, every
        ``has_object_permission`` and ``has_context_permission`` it calls,
        their operators, and the readability check of a denied resource (404
        or 403), as the request check gives them the body; the permissions
        those checks build read a resource's contexts from it
        (`scrud_django.permissions.checks_read_content`, whatever the view's
        ``get_permissions()`` does). A resource stored in a context reserved
        once normalised is a 404 first, as scoped-rbac's
        ``check_object_permissions`` answers it for an object of its own. The
        `Resource` is returned."""
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
        if access.stored_in_reserved_context(obj):
            # The content checked below has no rbac_context attribute for
            # scoped-rbac's check_object_permissions to read.
            raise Http404
        with checks_read_content(self):
            self.check_object_permissions(self.request, self.object_for_checks(obj))
        return obj

    def object_for_checks(self, obj):
        """`obj` (a `Resource`) as object checks get it: its content
        (`scrud_django.permissions.content_of`), the stored ``dict``
        itself."""
        return content_of(obj)

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
        overrides apply, giving it the saved resource's content
        (`object_for_checks`), as the detail route's object check does: its
        ``has_object_permission``
        must allow it (as scoped-rbac's own post-save check asks; superusers
        only for a resource in no usable context), and its
        ``has_context_permission`` must allow it in each context its
        ``contexts_for`` names, and, for one with a
        ``has_context_permission`` of its own, so must the grants, each
        context read as the resource is there (`may_act_in_context`)."""
        check = self.object_for_checks(obj)
        permission = view_permission(request, self)
        for leaf in reading_content(self.get_post_save_permissions()):
            contexts = access.usable_contexts(leaf.contexts_for(check))
            allowed = leaf.has_object_permission(request, self, check) and all(
                leaf.has_context_permission(request, self, check, context)
                for context in contexts
            )
            if allowed and not reads_each_context(leaf):
                # Its own has_context_permission (and its super() call) read
                # the content as stored: the grants are asked too, each
                # context as the resource is there. Object checks only narrow.
                allowed = all(
                    may_act_in_context(request, permission, check, [context])
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
            with self.write_checks(request):
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

    def recheck_saved(self, obj):
        """`check_post_save_permissions` of the saved `obj`, with
        ``scrud_workflow`` allowing only once the workflow check has
        authorized the write (`write_checks`)."""
        with self.write_checks(self.request, authorized=True):
            self.check_post_save_permissions(self.request, obj)

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
            recheck=lambda obj, _moved_to: self.recheck_saved(obj),
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

    def conditional_create_lock(self, using):
        """A conditional create's lock: the type's row (`type_locked`), so
        that two conditional creates of the type run one at a time and the
        second is evaluated against the list the first left."""
        return type_locked(self.get_resource_type(), using)

    def create(self, request):
        """Create a resource from the body, once the request's preconditions
        are evaluated against the list (`list_precondition_response`), under
        the type's lock when they need its validators (`LockedWriteMixin`);
        other creates take no lock. scrudful has evaluated them before, and
        this evaluation then gives ``None``, unless ``create`` is called from
        a POST ``@action(detail=False)``, which scrudful evaluates in its
        place (with no list validators): a stale ``If-Match`` is then this
        412."""
        failed = self.list_precondition_response(request)
        if failed is not None:
            return failed
        return self.create_resource(request)

    def create_resource(self, request):
        """Validate the body, save it (`save_checked`) and answer 201."""
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
        """Delete a Resource: the locked instance (`dispatch`). One that a
        resource type uses (its schema, context or indexing policy) is kept:
        409 (`ResourceInUseError`)."""
        instance = self.get_instance(request, slug)
        self.check_every_context_of_policy(request, instance)
        try:
            ResourceRegistration.delete_locked(
                instance,
                resource_type=self.get_resource_type(),
                using=self.locked_write_db or write_db(),
            )
        except ProtectedError as exc:
            raise ResourceInUseError(instance, exc.protected_objects) from exc
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
        everyone), a body nested deeper than `max_body_depth`
        (`check_body_depth`) and one with a NUL character in a string
        (`check_body_nul`): they depend on the body alone. scoped-rbac's
        request check then reads the body itself (``request.data``). A method
        the view does not serve skips the body checks (its body is not
        parsed), so that it gets its 405 whatever its body, as scoped-rbac's
        request check leaves it to. So does scrudful's body-less copy of an
        OPTIONS, on which it checks one method for ``Allow``
        (``scoped_rbac.access.checks_allow``): scoped-rbac checks a POST or
        PUT there without a body, and the copy is no write to refuse. A
        request that *arrives* so is refused before it gets here
        (`LockedWriteMixin.initial`)."""
        if request.method in self.allowed_methods and not access.checks_allow(request):
            check_body_present(request, self)
            check_body_depth(request)
            check_body_nul(request)
            check_written_contexts(request)
        with self.write_checks(request):
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
    - scrudful's 400, 412 and 428 are problem details while
      ``SCOPED_RBAC["UNIFORM_ERRORS"]`` is on (`UniformErrorsMixin`); off,
      they are scrudful's native ``{"detail": ...}`` bodies;
    - an object an ``IsAuthorized`` subclass's object check refuses to read
      is a 404 on the detail route, for every method (`object_readable`);
    - a POST to the list carrying ``If-Match``, ``If-None-Match`` or
      ``If-Unmodified-Since`` is evaluated by scrudful against the list as
      the requester would GET it (`create_list_validators`), before your
      ``create`` runs: a 412 or 400 never calls it. A POST
      ``@action(detail=False)`` (and a ``create`` it calls) is not evaluated
      by scrudful: call `list_precondition_response` first in it (a success
      nothing evaluated is refused, ``ImproperlyConfigured``). When the
      preconditions need the list's validators, the evaluation and your
      ``create`` run in one transaction holding the collection's lock
      (`conditional_create_lock`: scoped-rbac's
      ``conditional.lock_collection`` of the view's model; migrate
      ``scoped_rbac`` on its database), so two conditional creates run one
      at a time. Writes of your own that change the list without creating
      do not take it; a lock of your own a create needs belongs in
      `conditional_create_lock`, taken before the list is read.
    """

    # The detail route's URL keyword, as the detail stubs below take it.
    lookup_url_kwarg = "slug"

    # scrud variable
    resource_type_name: str | None = None

    class Meta(ScrudfulMeta):
        pass

    def conditional_create_lock(self, using):
        """A conditional create's lock: scoped-rbac's collection lock of the
        view's model (``scoped_rbac.conditional.lock_collection``), held until
        the create's transaction ends."""
        return _collection_locked(self.get_queryset().model, using)

    def locked_write_database(self):
        """The database of a locked write (and of a conditional POST to the
        list): where the view's model is written (``router.db_for_write`` of
        the model of its ``queryset``, else of ``get_queryset()``'s). It is
        called before DRF wraps the request: a ``get_queryset()`` that reads
        ``self.request`` sees Django's ``HttpRequest`` (one that cannot
        should leave a ``queryset`` attribute, or override this)."""
        queryset = getattr(self, "queryset", None)
        if queryset is None:
            queryset = self.get_queryset()
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
