import functools
import inspect
from collections.abc import Mapping
from contextlib import ExitStack, contextmanager

from django.core.exceptions import FieldError, ImproperlyConfigured, ValidationError
from django.db import DEFAULT_DB_ALIAS, router
from django.db.models import Max
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
from scoped_rbac.policy import Permission
from scoped_rbac.rest import AccessControlledAPIView
from scrudful._django import conditional_response, precondition_headers
from scrudful.conditional import parse_etag_list
from scrudful.rest_framework import ScrudfulViewFunc

from scrud_django import ui
from scrud_django.core import resources as core_resources
from scrud_django.core import writes as core_writes
from scrud_django.core.bodies import (
    MARIADB_MAX_BODY_DEPTH,  # noqa: F401
    MAX_BODY_DEPTH,  # noqa: F401
    body_depth,  # noqa: F401
    body_has_nul,  # noqa: F401
    check_contexts,
    check_depth,
    check_nul,
    check_present,
    max_body_depth,  # noqa: F401
)
from scrud_django.core.bodies import check_body as core_check_body
from scrud_django.core.lists import (  # noqa: F401  (re-exported)
    LIST_PRECONDITIONS,
    evaluate_list_preconditions,
    has_list_preconditions,
    latest,
    list_validators_needed,
)
from scrud_django.core.lists import list_etag as core_list_etag
from scrud_django.core.types import has_hooks, hook_leaf, type_options
from scrud_django.core.workflow import authorizing_write
from scrud_django.drf_requirements import (
    NotNeutral,
    ViewLeaf,
    hook_leaves,
    requirement_of,
)
from scrud_django.exceptions import (
    RbacContextInvalidError,  # noqa: F401  (re-exported)
    RequestBodyMissingError,  # noqa: F401  (re-exported)
    RequestBodyNulError,  # noqa: F401  (re-exported)
    RequestBodyTooDeepError,  # noqa: F401  (re-exported)
    ResourceInUseError,  # noqa: F401  (re-exported)
    ScrudException,
    as_drf,
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
from scrud_django.mixins.resource_mixin import rbac_contexts_of  # noqa: F401  (re-exported)
from scrud_django.models import Resource
from scrud_django.models.resource import (  # noqa: F401  (re-exported)
    MAX_CONTEXT_BYTES,
    context_fits,
)
from scrud_django.paginations import StandardResultsSetPagination
from scrud_django.permissions import (
    _not_checking_write,
    checks_read_content,
    content_of,
    may_act_in_context,  # noqa: F401  (re-exported)
    reading_content,
    reads_each_context,
)
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    lock_if_workflow_policy,
    type_db,
    type_locked,  # noqa: F401  (re-exported)
    write_db,
)
from scrud_django.serializers import ResourceSerializer
from scrud_django.validators import (  # noqa: F401  (re-exported)
    check_policy_is_unique,
)
from scrud_django.workflow import (
    WORKFLOW_POLICY_TYPE_URI,
    lock_workflow_policy_type,
)

# The methods whose lookup, authorization, preconditions, workflow check and
# write run in one transaction with the row locked (`ResourceViewMixin.dispatch`).
LOCKED_WRITE_METHODS = frozenset({"PUT", "PATCH", "DELETE"})

# The methods whose resource type is read on its write database
# (`ResourceViewMixin.get_resource_type`): the unsafe ones, any POST (one that
# writes nothing, an @action of your own, too). For PUT, PATCH, DELETE and a
# conditional POST the transaction rule there already does; this adds the
# POSTs that run in none.
TYPE_WRITE_METHODS = LOCKED_WRITE_METHODS | {"POST"}


# What a view records of the POST to its list it serves (`LockedWriteMixin`),
# forgotten when it serves another: the list's validators read
# (`create_list_validators`), whether the conditional create's lock is held,
# and whether its preconditions were evaluated (by scrudful, whose list
# callbacks record it, `_recording_evaluation`, or by
# `list_precondition_response`).
_CREATE_STATE = (
    "_scrud_create_validators",
    "_scrud_create_locked",
    "_scrud_list_preconditions_evaluated",
)


def list_preconditions_to_evaluate(headers):
    """Whether the preconditions `headers` of a POST to a list must be
    evaluated (`LockedWriteMixin.dispatch` refuses a response that is no
    error and did not):
    any of `LIST_PRECONDITIONS` but an ``If-Match: *`` without
    ``If-None-Match``, which passes on a list, which always exists
    (``If-Unmodified-Since`` is not evaluated beside an ``If-Match``)."""
    if not has_list_preconditions(headers):
        return False
    if_match = parse_etag_list(headers.get("if-match") or None)
    return not (if_match == ["*"] and not headers.get("if-none-match"))


# The methods whose body is the resource to store.
BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

# The viewset actions (DRF's ``view.action``) whose write the workflow check
# covers, with the method each serves: the list's create and the detail
# route's update (the routes ``registration.resource_type_urls`` builds). Any
# other action (an ``@action``, a handler of your own) may write without it.
# Not to be confused with ``SCRUD_WORKFLOW_ACTIONS``, the workflow actions
# run after a write.
WORKFLOW_VIEW_ACTIONS = {"create": "POST", "update": "PUT"}


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
    check_present(request.stream is not None)


def check_body_depth(request):
    """Raise `RequestBodyTooDeepError` (400) for a POST, PUT or PATCH body
    nested deeper than `max_body_depth`."""
    if request.method in BODY_METHODS:
        check_depth(request.data)


def check_body_nul(request):
    """Raise `RequestBodyNulError` (400) for a POST, PUT or PATCH body with a
    NUL character in any string (`body_has_nul`)."""
    if request.method in BODY_METHODS:
        check_nul(request.data)


def check_written_contexts(request):
    """Raise `RbacContextInvalidError` (400) when a POST, PUT or PATCH body's
    ``rbac_context`` is an empty list or names a context longer than its rows
    hold (`context_fits`: 2048 bytes of UTF-8)."""
    if request.method in BODY_METHODS:
        check_contexts(request.data)


def check_body(request, view=None):
    """Check the body of a POST, PUT or PATCH that `view` serves (one it does
    not is left to its 405) with `scrud_django.core.bodies.check_body`. The
    body is *present* when DRF reads one: not an empty one, nor one without a
    ``Content-Length`` (0 or absent, as for a chunked body), which DRF would
    read as ``{}``. A body that is not JSON is left to the parser (415, or
    400)."""
    if request.method not in BODY_METHODS:
        return
    if view is not None and request.method not in view.allowed_methods:
        return
    present = request.stream is not None
    core_check_body(request.data if present else None, present=present)


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
    # scrudful builds the HSR, with ``request`` None (they then give paths
    # for scrud's own routes, which scrudful resolves against
    # ``SCRUDFUL["BASE_URL"]``; on a response those are absolute, from the
    # request; a type's stored schema or context URI is as stored): they
    # take ``**kwargs``.
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


def _recording_evaluation(func):
    """`func`, a list validator callback of a scrudful handler
    (``list_etag_func``, ``list_last_modified_func``), which scrudful calls
    for a POST only to evaluate its preconditions: for a POST to a
    `LockedWriteMixin` view, refusing the evaluation, for a POST to the
    view's list, when it needs the list's validators without the conditional
    create's lock held (`ListValidatorsMixin.refuse_unlocked_list_read`),
    then recording on the view, once `func` returned, that the POST's
    preconditions were evaluated
    (`LockedWriteMixin.list_preconditions_evaluated`), whether they pass or
    not. Idempotent."""
    if func is None or getattr(func, "_scrud_records_evaluation", False):
        return func

    @functools.wraps(func)
    def recording(*args, **kwargs):
        view = args[0] if args else None
        request = args[1] if len(args) > 1 else None
        posted = isinstance(view, LockedWriteMixin) and request.method == "POST"
        if (
            posted
            and isinstance(view, ListValidatorsMixin)
            and view.is_list_post(request)
        ):
            view.refuse_unlocked_list_read(request)
        value = func(*args, **kwargs)
        if posted:
            view._scrud_list_preconditions_evaluated = True
        return value

    recording._scrud_records_evaluation = True
    return recording


def gate_uniform_errors(cls):
    """Make every scrudful handler of `cls` (and its bases), which
    ``scrudful_viewset`` or ``@scrudful`` installed, a
    `UniformErrorsViewFunc`, and have the list validator callbacks of every
    scrudful handler there (whichever its class or callbacks) record that
    scrudful evaluated a POST's preconditions with them
    (`_recording_evaluation`). Idempotent."""
    for klass in cls.__mro__:
        for value in list(vars(klass).values()):
            if not isinstance(value, ScrudfulViewFunc):
                continue
            value.list_etag_func = _recording_evaluation(value.list_etag_func)
            value.list_last_modified_func = _recording_evaluation(
                value.list_last_modified_func
            )
            if type(value) is ScrudfulViewFunc:
                # Keep the configured values under the properties' names
                # before the class changes, so no request sees neither.
                value._scrud_error = value.__dict__.get("error")
                value._scrud_error_body_func = value.__dict__.get("error_body_func")
                value.__class__ = UniformErrorsViewFunc


class ScrudErrorsMixin:
    """Hand a scrud error (neutral, not an ``APIException``) to DRF's
    exception handling wrapped (`scrud_django.exceptions.as_drf`), so that any
    handler, scrud's or an adopter's own, answers it with its status and
    detail."""

    def handle_exception(self, exc):
        if isinstance(exc, ScrudException):
            exc = as_drf(exc)
        return super().handle_exception(exc)


class UniformErrorsMixin:
    """Gate the scrudful handlers' problem-details errors on
    ``SCOPED_RBAC["UNIFORM_ERRORS"]`` (`UniformErrorsViewFunc`), and have
    their list validator callbacks record the evaluations they serve
    (`gate_uniform_errors`): ``as_view``, which runs after
    ``scrudful_viewset`` decorated the class, converts them."""

    @classmethod
    def as_view(cls, *args, **kwargs):
        gate_uniform_errors(cls)
        return super().as_view(*args, **kwargs)


class LockedWriteMixin:
    """PUT, PATCH and DELETE in one transaction with the row locked, and a
    POST to the list whose preconditions must be evaluated in one transaction
    too: for `ResourceViewMixin` and `GenericViewMixin` views.

    `dispatch` runs the whole request in one transaction on
    `locked_write_database`: DRF's permission check, the lookup with the row
    locked (`locked`, which the view applies to the queryset it looks the
    object up in), scrudful's precondition evaluation (If-Match,
    If-Unmodified-Since) against that row, the object check and the write of
    that same instance. A concurrent write of the row waits for it to
    commit, and then evaluates its own preconditions against what it
    committed. An error response (4xx, 5xx: a 412, a 400) rolls the
    transaction back; a success or a redirect commits it.

    A POST to the list (`is_list_post`: any POST but one to an item)
    carrying ``If-Match``, ``If-None-Match`` or ``If-Unmodified-Since``
    (`list_preconditions_to_evaluate`: all but an ``If-Match: *`` without
    ``If-None-Match``, which always passes on a list, and is not wrapped)
    runs in one
    transaction on `locked_write_database` too. When its preconditions need
    the list's validators (`is_conditional_create`), conditional creates
    run one at a time: once DRF's ``initial`` has authenticated, authorized
    and throttled it, `conditional_create_lock` is taken, and held until the
    response. The preconditions are then evaluated against the list: by
    scrudful, before a scrudful handler of the view with list validators
    runs (a viewset's ``create``, which ``scrudful_viewset`` gives
    ``Meta``'s, whichever they are; its callbacks record it,
    `gate_uniform_errors`), else by
    `ListValidatorsMixin.list_precondition_response`, which the handler
    calls first (``ResourceViewMixin.create`` does). An error response rolls
    the transaction back. A response that is no error is refused
    (``ImproperlyConfigured``) and rolled back when nothing evaluated the
    preconditions (`list_preconditions_evaluated`), whether or not the
    handler wrote: one scrudful does not evaluate (an ``@action``, or a
    handler of your own, whose ``@scrudful`` has no list validators, or a
    ``create`` override not decorated again with ``scrudful_viewset`` that
    does not reach scrudful's ``create`` through ``super().create()``, or a
    handler scrudful skips because it already handles the request) and that
    does not call `list_precondition_response`.

    What an evaluation answers is the handler's to return: a handler that
    gets scrudful's 412 or 400 (from a scrudful handler it calls) or
    `list_precondition_response`'s, or an exception an evaluation raised,
    must return it (or let it propagate). One that drops it and answers
    otherwise may go undetected: what it writes then commits. (A list
    callback that raises records no evaluation, so a success after dropping
    its exception is refused as unevaluated, unless an evaluation was
    recorded before it raised: by ``list_etag_func``, which scrudful calls
    before ``list_last_modified_func``, or by `list_precondition_response`.
    An exception scrudful raises after a callback returned, from
    ``quote_etag`` say, is not covered.)

    Other requests are not wrapped. A subclass overriding ``dispatch`` or
    ``initial`` must call ``super()``: without it, a conditional create's
    preconditions are refused (``ImproperlyConfigured``) rather than
    evaluated unlocked."""

    # The database of the request's locked write transaction (`dispatch`), or
    # ``None`` outside one.
    locked_write_db: str | None = None

    # The database of the transaction of a POST to the list whose
    # preconditions must be evaluated (`dispatch`), or ``None`` outside one.
    conditional_create_db: str | None = None

    def locked_write_database(self):
        """The database a locked write's transaction opens on."""
        raise NotImplementedError()

    def is_list_post(self, request):
        """Whether `request` is a POST to the collection this view serves:
        any POST but one to an item. The router tells (``detail``, which it
        gives each route's view). In a view built without one (``as_view``
        with its actions, or a view that is no viewset), a POST mapped to
        ``create`` is the list's; one mapped to the handler of an extra
        action of the viewset (``get_extra_actions``, its own or one
        ``@<action>.mapping.post`` adds) is as the action's ``detail`` says;
        another (a handler of the view's own, a plain view's ``post``) is the
        list's unless the URL names an item (the view's ``lookup_url_kwarg``,
        else its ``lookup_field``, among the URL's keywords). So a POST to a
        collection nested under a parent whose URL keyword has that name
        (``parents/<slug>/notes/`` for scrud's views), served by such a
        handler, is taken for a POST to an item (its preconditions are then
        refused, ``ImproperlyConfigured``, or not evaluated): pass
        ``detail=False`` to ``as_view``, as a router does (a plain view: set
        ``detail = False`` on it), or make the handler an
        ``@action(detail=False)``."""
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
        lookup = getattr(self, "lookup_url_kwarg", None) or getattr(
            self, "lookup_field", None
        )
        return lookup not in (getattr(self, "kwargs", None) or {})

    def is_conditional_create(self, request):
        """Whether `request` is a POST to the list (`is_list_post`) carrying
        a precondition that needs the list's validators
        (`list_validators_needed`; ``If-Match: *`` and ``If-None-Match: *``
        alone do not)."""
        return self.is_list_post(request) and list_validators_needed(
            precondition_headers(request)
        )

    def list_preconditions_evaluated(self):
        """Whether the preconditions of the POST this view serves were
        evaluated against the list, whether they passed or not: scrudful
        called a list validator callback of one of the view's handlers for it
        (`_recording_evaluation`; it calls them only to evaluate, and
        evaluates only the outer of two nested handlers), or
        `ListValidatorsMixin.list_precondition_response` evaluated them.
        Recorded as it happens, not predicted from the handler. Whether they
        passed is not recorded: the handler returns what the evaluation
        answered."""
        return bool(self.__dict__.get("_scrud_list_preconditions_evaluated"))

    def check_write_database(self, using):
        """Refuse (``ImproperlyConfigured``) a locked write or a conditional
        POST whose transaction opens on database `using`
        (`locked_write_database`) when the view's writes go elsewhere.
        Called by `initial` once the request is authorized, before any lock
        is taken. Here, nothing to check."""

    def conditional_create_lock(self, using):
        """A context manager taking the lock every conditional create of this
        view's collection takes, on database `using`, inside the create's
        transaction."""
        raise NotImplementedError()

    def dispatch(self, request, *args, **kwargs):
        # A view instance serves one request: forget what a reused one
        # recorded of its last (`_CREATE_STATE`).
        for name in _CREATE_STATE:
            self.__dict__.pop(name, None)
        if request.method in LOCKED_WRITE_METHODS:
            attribute = "locked_write_db"
        elif self.is_list_post(request) and list_preconditions_to_evaluate(
            precondition_headers(request)
        ):
            attribute = "conditional_create_db"
        else:
            return super().dispatch(request, *args, **kwargs)
        db = self.locked_write_database()
        with core_writes.write_transaction(db) as rollback, ExitStack() as locks:
            setattr(self, attribute, db)
            self._scrud_create_locks = locks
            try:
                response = super().dispatch(request, *args, **kwargs)
            finally:
                setattr(self, attribute, None)
                self._scrud_create_locks = None
            if response.status_code >= 400:
                rollback()
            elif attribute == "conditional_create_db":
                # Raised inside the transaction: what the handler wrote is
                # rolled back.
                if not self.list_preconditions_evaluated():
                    raise ImproperlyConfigured(
                        f"{type(self).__name__} answered a POST to its list "
                        f"with {response.status_code} without evaluating its "
                        "preconditions against the list. A POST handler "
                        "scrudful does not evaluate (an @action, or a handler "
                        "of your own, whose @scrudful has no list_etag_func "
                        "or list_last_modified_func) must call "
                        "list_precondition_response first, and a create "
                        "override that does not reach scrudful's create "
                        "(super().create()) must be decorated again with "
                        "@scrudful_viewset (or call it first)."
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
        (authentication, permissions, throttles), then, inside `dispatch`'s
        transaction, `check_write_database`, then, for a conditional create
        (`is_conditional_create`), its lock (`conditional_create_lock`),
        held until that transaction ends: after the request is authorized, so
        a refused one never waits for it or holds it."""
        try:
            access.refuse_rewritten_options(request)
        except access.AccessError as exc:
            raise drf_error(exc, request) from exc
        super().initial(request, *args, **kwargs)
        db = self.locked_write_db or self.conditional_create_db
        if db is not None:
            self.check_write_database(db)
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
        request's transaction ends; inside `unlocked_lookups`, read there
        without the lock."""
        if self.locked_write_db is not None:
            queryset = queryset.using(self.locked_write_db)
            if not self.lookups_unlocked:
                queryset = queryset.select_for_update()
        return queryset

    # True inside `unlocked_lookups`.
    lookups_unlocked = False

    @contextmanager
    def unlocked_lookups(self):
        """Inside the block, a locked write's lookups (`locked`, and
        ``get_object()``) read its database as they would, filtered and
        checked the same, but lock no row: for a lookup made after the
        request was refused (the HTML answer to a refused PUT,
        `scrud_django.ui.renderer.load_stored`), which must neither wait for
        the row lock nor hold it."""
        previous, self.lookups_unlocked = self.lookups_unlocked, True
        try:
            yield
        finally:
            self.lookups_unlocked = previous


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
        an object readable.

        The view's requirement (`requirement_of`, built as a GET of the
        detail route has it) is asked through
        `scrud_django.core.resources.readable_content`, the grants for
        ``http.GET`` on the type the view names
        (``resource_type_iri_for``); a view with none (`NotNeutral`: DRF's
        ``~`` over an ``IsAuthorized``) asks its permissions itself."""
        grants = isinstance(self, AccessControlledAPIView)
        with (
            _not_checking_write(),
            checks_read_content(self),
            as_detail_read(self, request) as read,
        ):
            try:
                requirement = requirement_of(self)
            except NotNeutral:
                requirement = None
            if requirement is not None:
                permission = (
                    Permission("http.GET", self.resource_type_iri_for(read))
                    if grants
                    else None
                )
                return core_resources.readable_content(
                    read, obj, requirement, grants=grants, permission=permission
                )
        with _not_checking_write(), checks_read_content(self):
            if grants:
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
            self.check_object_hooks(request, obj)
        except (PermissionDenied, NotAuthenticated):
            if not self.object_readable(request, obj):
                raise Http404 from None
            raise

    def type_hooks(self):
        """The `scrud_django.core.types.HookLeaf` of the hooks registered for
        the view's type, or ``None`` for none (`ResourceViewMixin`'s)."""
        return None

    def check_object_hooks(self, request, obj):
        """Refuse (``permission_denied``) `obj` (the object as the object
        check gets it) unless the object checks registered for the view's
        type (`type_hooks`) allow it. They only narrow: the view's own
        checks ran first."""
        hooks = self.type_hooks()
        if hooks is not None and not hooks.has_object_permission(request, obj):
            self.permission_denied(request)

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
            try:
                requirement = requirement_of(self)
            except NotNeutral:
                requirement = None
            if requirement is None:
                if all(
                    map(reads_each_context, reading_content(get_rbac_permissions()))
                ):
                    return
                allowed = grants_allow(
                    self, request, obj, view_permission(request, self)
                )
            else:
                try:
                    core_resources.check_grants_in_each_context(
                        request, obj, requirement, view_permission(request, self)
                    )
                    allowed = True
                except access.AccessDenied:
                    allowed = False
        if not allowed:
            # DRF's own refusal (403, or 401 for the anonymous user).
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
        return core_list_etag(
            self.get_resource_type(),
            items=items,
            count=count,
            query=sorted(request.query_params.lists()),
        )

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
        not a conditional create, `LockedWriteMixin.is_conditional_create`:
        a POST to an item, or one whose ``dispatch`` or ``initial`` skips
        ``super()``), they are refused (``ImproperlyConfigured``) rather
        than evaluated unlocked."""
        validators = self.__dict__.get("_scrud_create_validators")
        if validators is None:
            validators = (None, None)
            if list_validators_needed(precondition_headers(request)):
                self.refuse_unlocked_list_read(request)
                with self.as_list_read(request, self.conditional_create_db) as read:
                    validators = (self.list_etag(read), self.list_last_modified())
            self._scrud_create_validators = validators
        return validators

    def refuse_unlocked_list_read(self, request):
        """Refuse (``ImproperlyConfigured``) to evaluate the preconditions of
        POST `request` against the list when they need its validators
        (`list_validators_needed`) and the conditional create's lock is not
        held (`LockedWriteMixin`: a POST to an item, or a ``dispatch`` or
        ``initial`` that skips ``super()``): they would be read unlocked.
        `create_list_validators` and every list callback of a scrudful
        handler for a POST to the view's list (`_recording_evaluation`, a
        ``Meta`` of your own's too) call it first."""
        if not list_validators_needed(precondition_headers(request)):
            return
        if self.conditional_create_db is None or not self.__dict__.get(
            "_scrud_create_locked"
        ):
            raise ImproperlyConfigured(
                f"{type(self).__name__} evaluates a conditional POST against "
                "its list outside a conditional create's locked transaction: "
                "serve it as a POST to the list (LockedWriteMixin."
                "is_list_post: any POST but one to an item; an as_view() "
                "handler on a nested URL naming a parent by the view's "
                "lookup keyword needs detail=False), and call super() in "
                "dispatch and initial."
            )

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
        conditional create's lock (`create_list_validators`). It evaluates
        nothing for a POST handler whose ``@scrudful`` has no list
        validators, such as an ``@action`` as ``scrudful.E007`` asks it
        (unless given ``list_etag_func=ScrudfulMeta.list_etag_func`` and
        ``list_last_modified_func=ScrudfulMeta.list_last_modified_func``),
        nor for a ``create`` such a handler calls (scrudful evaluates only
        the outer of two nested handlers): call this first there, or in that
        ``create``, as ``ResourceViewMixin.create`` does. That POST runs in
        the transaction `LockedWriteMixin.dispatch` opens, and reads the list
        under the conditional create's lock; a response that is no error
        whose preconditions nothing evaluated is refused
        (``ImproperlyConfigured``). Once it has evaluated them it records
        so, whether they passed or not: the response it gives must be
        returned, as nothing refuses a success that drops it. A refusal it
        raises records nothing.

        With `using` ``None`` it reads the list as `create_list_validators`
        does, under that lock (else ``ImproperlyConfigured``). With a `using`
        other than the conditional create's database it reads the list there
        and holds nothing: to hold it, call it inside a transaction there that
        first locks a row every such create locks, and create in that
        transaction."""
        headers = precondition_headers(request)
        if not has_list_preconditions(headers):
            return None

        def validators():
            if using is None or using == self.conditional_create_db:
                return self.create_list_validators(request)
            with self.as_list_read(request, using) as read:
                return self.list_etag(read), self.list_last_modified()

        result = evaluate_list_preconditions(request.method, headers, validators)
        self._scrud_list_preconditions_evaluated = True
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


class _PostSaveLeaf(ViewLeaf):
    """The `ViewLeaf` of `permission` (an ``IsAuthorized`` of `view`, reading
    the content: `reading_content`) as the post-save check asks it
    (`scrud_django.core.writes.check_post_save`): its object check is the
    permission's own, asked as it is, whatever the request's method, its
    refusals DRF's own."""

    def has_object_permission(self, request, obj):
        return self._leaf().has_object_permission(request, self.view, obj)


class ReadableMixin:
    """Put first in a view composing scoped-rbac's ``AccessControlledAPIView``
    with `ResourceViewMixin` (or `GenericViewMixin`), as the scrud viewsets
    do: ``AccessControlledAPIView.check_object_permissions`` then asks
    `ListValidatorsMixin.object_readable` whether a refused object is a 403
    or a 404, which reads each of a resource's contexts with the resource as
    it is there, rather than its own ``is_readable``."""

    def is_readable(self, request, obj):
        return self.object_readable(request, obj)


class ResourceViewMixin(
    ScrudErrorsMixin, UniformErrorsMixin, LockedWriteMixin, ListValidatorsMixin
):
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
        stale type), DRF's, then, with ``scrud_django.ui`` installed, GET on
        the branch of a request under a published branch's prefix
        (``scrud_django.ui.branches.middleware.check_mount``) and the
        fragment protocol's headers of a request that negotiated HTML
        (``scrud_django.ui.renderer.check_ui_headers``: a 400 for an invalid
        ``Scrud-UI-Pointer``, after authentication and authorization, before
        a PUT writes), then, inside a locked write (`dispatch`), the
        ``WorkflowPolicy`` type lock (`lock_if_workflow_policy`) before the
        row is read: every ``WorkflowPolicy`` write locks the type row and then
        the policy's row, in that order, so two cannot deadlock."""
        self._scrud_resource_types = {}
        super().initial(request, *args, **kwargs)
        if ui.is_enabled():
            from scrud_django.ui.branches.middleware import check_mount
            from scrud_django.ui.renderer import check_ui_headers

            # Authenticated and authorized first: a 401 or 403 stays one.
            check_mount(request)
            check_ui_headers(request)
        if self.locked_write_db is not None:
            lock_if_workflow_policy(self.get_resource_type(), self.locked_write_db)

    def get_renderers(self):
        """DRF's, and with ``scrud_django.ui`` installed the HTML renderer,
        ahead of the browsable API (which ``?format=api`` still reaches)."""
        renderers = super().get_renderers()
        if ui.is_enabled():
            from scrud_django.ui.renderer import ui_renderers

            renderers = ui_renderers(renderers)
        return renderers

    def perform_content_negotiation(self, request, force=False):
        """DRF's, recording whether it chose HTML: an exception handler may
        swap the accepted renderer for a JSON one (`finalize_response`)."""
        negotiated = super().perform_content_negotiation(request, force)
        if ui.is_enabled():
            from scrud_django.ui.renderer import note_negotiation

            note_negotiation(request, negotiated[0])
        return negotiated

    def finalize_response(self, request, response, *args, **kwargs):
        """DRF's; a request that negotiated HTML is answered as HTML, errors
        included (`scrud_django.ui.renderer.finalize_html`). A refused PUT
        looks the stored resource up first, through `get_object`'s checks
        (`scrud_django.ui.renderer.load_stored`), inside the request's
        transaction."""
        if ui.is_enabled():
            from scrud_django.ui.renderer import load_stored

            load_stored(self, request, response)
        response = super().finalize_response(request, response, *args, **kwargs)
        if ui.is_enabled():
            from scrud_django.ui.renderer import finalize_html

            finalize_html(request, response)
        return response

    def checks_workflow(self, request):
        """Whether `request` is a write the workflow check
        (``WorkflowValidator``) covers: a POST the view serves as its
        ``create`` action or a PUT it serves as its ``update`` action
        (`WORKFLOW_VIEW_ACTIONS`), keyed on DRF's ``self.action``. Any other
        action, such as an ``@action`` that writes the resource itself, and
        a view with no action (``None``: a method the view does not serve,
        or a view that is not a viewset) is not: ``scrud_workflow`` refuses
        it. A subclass whose own action writes only through ``create`` or
        ``update`` (so the workflow check runs) may override this to name
        it."""
        action = getattr(self, "action", None)
        return (
            action in WORKFLOW_VIEW_ACTIONS
            and request.method == WORKFLOW_VIEW_ACTIONS[action]
            and request.method in self.allowed_methods
        )

    def write_checks(self, request, authorized=False):
        """The block in which a check of `request` runs: for a write the
        workflow check covers (`checks_workflow`: the view's ``create`` or
        ``update`` action), with the ``scrud_workflow`` operator allowing
        (the workflow check decides the transition); with `authorized` (the
        post-save check), only once the workflow check has authorized the
        write. Anywhere else (an ``@action`` of the view included)
        ``scrud_workflow`` refuses."""
        return authorizing_write(
            request, covered=self.checks_workflow(request), authorized=authorized
        )

    def check_object_permissions(self, request, obj):
        """DRF's object check, with ``scrud_workflow`` allowing for a PUT the
        workflow check covers (the ``update`` action) and refusing for
        anything else
        (`write_checks`). Whether a refused resource is readable (404 or
        403) is asked outside it (`object_readable`)."""
        with self.write_checks(request):
            super().check_object_permissions(request, obj)

    def type_hooks(self):
        """The `scrud_django.core.types.HookLeaf` of the ``checks`` and
        ``object_checks`` registered for the view's type
        (``resource_type_name``; ``registration.json_resource_type``),
        naming the type as the view's permissions do
        (``resource_type_iri``), or ``None`` when it has none (the type is
        then not read). It takes part in the view's requirement
        (`scrud_django.drf_requirements.requirement_of`), its request check
        (`check_request_hooks`), object check (`check_object_hooks`), list,
        post-save check (`_post_save_requirement`) and workflow check
        (``scrud_django.validators.narrow_of``)."""
        if not has_hooks(type_options(self.resource_type_name)):
            return None
        return hook_leaf(
            self.get_resource_type(),
            self.resource_type_name,
            type_iri=getattr(self, "resource_type_iri", None),
        )

    def check_request_hooks(self, request):
        """Refuse (``permission_denied``) a POST, PUT or PATCH the view
        serves (not scrudful's body-less copy of an OPTIONS,
        ``access.checks_allow``) unless the checks registered for its type
        (`type_hooks`) allow it in each context its body writes. They only
        narrow: the view's own checks ran first."""
        if request.method not in self.allowed_methods or access.checks_allow(request):
            return
        hooks = self.type_hooks()
        if hooks is None:
            return
        body = request.data if request.method in BODY_METHODS else None
        if not hooks.has_permission(request, body):
            self.permission_denied(request)

    def get_resource_type(self):
        """The view's resource type, read once per request and database (the
        filter, the serializer, the validators, the Links and the write all
        ask for it). For an unsafe method (`TYPE_WRITE_METHODS`; and inside
        a locked write's or a conditional POST's transaction, `dispatch`) it
        is read on the type's write database (`type_db`), with its documents
        (``registration.TYPE_DOCUMENTS``), not through the read router: a
        lagging replica would validate the body against an older schema, and
        a conditional create locks the type row it reads
        (`conditional_create_lock`): read through the router, two creates of
        a slug with several rows could lock different rows, and not run one
        at a time."""
        transaction_db = self.locked_write_db or self.conditional_create_db
        method = getattr(getattr(self, "request", None), "method", None)
        writes = transaction_db is not None or method in TYPE_WRITE_METHODS
        using = type_db() if writes else None
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
        `Resource` is returned. Inside a locked write (`dispatch`) the lookup
        reads the write database with the row locked (`locked`; not inside
        `unlocked_lookups`), whatever
        database a ``get_queryset()`` of a subclass selects (``using()``). A
        ``get_queryset()`` or filter backend of a subclass that makes the
        queryset one PostgreSQL cannot lock ``FOR UPDATE`` (``DISTINCT``,
        ``GROUP BY``, the nullable side of an outer join) fails such a write
        there with ``NotSupportedError``; MySQL and MariaDB lock it, and
        SQLite has no row locks."""
        previous, self._scrud_in_get_object = self._scrud_in_get_object, True
        try:
            queryset = self.locked(self.filter_queryset(self.get_queryset()))
        finally:
            self._scrud_in_get_object = previous
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        obj = get_object_or_404(
            queryset, **{self.lookup_field: self.kwargs[lookup_url_kwarg]}
        )
        # The type the view read (on the same database) cached on it, not
        # read again; a 404 for one stored in a reserved context: the content
        # checked below has no rbac_context attribute for scoped-rbac's
        # check_object_permissions to read.
        core_resources.found_item(self.get_resource_type(), obj)
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
        PUT or DELETE its row is locked (`dispatch`). With
        ``scrud_django.ui`` installed, a PUT answered as HTML keeps a copy of
        its content (`scrud_django.ui.renderer.remember_stored`)."""
        instance = self.get_object()
        if ui.is_enabled():
            from scrud_django.ui.renderer import remember_stored

            remember_stored(self, request, instance)
        return instance

    def get_serializer(self, *args, **kwargs):
        kwargs["resource_type"] = self.get_resource_type()
        return super().get_serializer(*args, **kwargs)

    def perform_create(self, serializer):
        """``serializer.save()``, in a reversion revision when the type uses
        reversion (`scrud_django.core.writes.creating_revision`)."""
        with core_writes.creating_revision(self.get_resource_type()):
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

        It asks each permission itself (`_post_save_requirement`), so that an
        ``IsAuthorized`` subclass's overrides apply, giving it the saved
        resource's content (`object_for_checks`), as the detail route's object
        check does, the request's action named on the view's type
        (``resource_type_iri_for``): its ``has_object_permission``
        must allow it (as scoped-rbac's own post-save check asks; superusers
        only for a resource in no usable context), and its
        ``has_context_permission`` must allow it in each context its
        ``contexts_for`` names, and, for one with a
        ``has_context_permission`` of its own, so must the grants, each
        context read as the resource is there (`may_act_in_context`):
        `scrud_django.core.writes.check_post_save`."""
        check = self.object_for_checks(obj)
        type_iri = self.resource_type_iri_for(request)
        core_writes.check_post_save(
            request,
            self.get_resource_type(),
            obj,
            self._post_save_requirement(),
            type_iri=type_iri,
            content=check,
        )

    def _post_save_requirement(self):
        """`get_post_save_permissions`, each reading the content
        (`reading_content`), as the requirement
        `scrud_django.core.writes.check_post_save` checks a saved resource
        against: every one must allow it, and so must the hooks registered
        for the view's type (`type_hooks`)."""
        return [
            _PostSaveLeaf(permission, self)
            for permission in reading_content(self.get_post_save_permissions())
        ] + hook_leaves(self)

    def check_every_context_of_policy(self, request, instance):
        """For a ``WorkflowPolicy``, deny (403) an update or delete unless the
        request's action is allowed in **every** context the stored policy
        applies to: it governs each of them. The object check is any-of; a
        policy written through the API has one context, but one stored with a
        list (before 0.12.0, or server-side) has several."""
        if instance.resource_type.type_uri != WORKFLOW_POLICY_TYPE_URI:
            return
        try:
            core_writes.check_every_context_of_policy(
                request,
                instance,
                None,  # The view's own check asks the view's permissions.
                covered=self.checks_workflow(request),
                check=self.check_post_save_permissions,
            )
        except access.AccessError as exc:
            raise drf_error(exc, request) from exc

    def serialized_policy_save(self, save, db):
        """`save` for a ``WorkflowPolicy``: inside the write's transaction,
        take `lock_workflow_policy_type`, save, and re-check that the saved
        policy is the only one for its type in its contexts
        (`check_policy_is_unique`, 400 and rolled back otherwise). The
        validator's check ran before the lock; a concurrent write of the same
        type and context committed since is seen here."""

        return core_writes.serialized_policy_save(
            save, db, lock=lock_workflow_policy_type
        )

    def recheck_saved(self, obj):
        """`check_post_save_permissions` of the saved `obj`, with
        ``scrud_workflow`` allowing only once the workflow check has
        authorized the write (`write_checks`). `save_checked` no longer calls
        it: `scrud_django.core.writes.save_checked` runs
        `check_post_save_permissions` in the same block itself."""
        with self.write_checks(self.request, authorized=True):
            self.check_post_save_permissions(self.request, obj)

    def save_checked(self, save, instance=None):
        """Run `save` (which returns the saved resource) in a transaction and
        check the resource as stored before it commits
        (`scrud_django.core.writes.save_checked`, given the view's queryset,
        `serialized_policy_save` and `check_post_save_permissions`): a
        reserved context is a 400 and `check_post_save_permissions` a 403
        (run with ``scrud_workflow`` allowing only once the workflow check has
        authorized the write, `write_checks`), either rolling the write
        back. The request was authorized by the context its body names; this
        checks where the resource actually landed (scoped-rbac's ``SaveCheck``).
        `instance` is the resource an update replaces (``None`` for a create).
        Returns the saved resource as read back.

        The check refreshes the saved resource from its row, which empties
        its cache of related objects: the view's type (`get_resource_type`,
        read on its write database) is cached on it again, and on what is
        returned, so that what reads it later (the response's ``Location``,
        and what runs on commit with it: search indexing, the
        ``SCRUD_WORKFLOW_ACTIONS`` `ScrudSignalProcessor` defers) does not
        load it through the read router. The check and this re-caching run
        in one transaction on the write database, so that a write outside
        `dispatch`'s transaction commits only once the type is cached again:
        the check's own transaction, outermost, would commit, and run what
        waits on the commit, before. Outside it are a POST to the list with
        no precondition to evaluate (`list_preconditions_to_evaluate`: none
        of ``If-Match``, ``If-None-Match`` and ``If-Unmodified-Since``, or
        ``If-Match: *`` and no ``If-None-Match``) and a POST to an item (an
        ``@action`` creating)."""
        request = self.request
        resource_type = self.get_resource_type()
        queryset = self.get_queryset()
        # The request's locked write transaction, if any (`dispatch`); else
        # the un-hinted write alias, as there (instance-hint routing of
        # scrud_django's models is not supported for the API).
        db = self.locked_write_db or write_db()
        covered = self.checks_workflow(request)
        try:
            return core_writes.save_checked(
                request,
                resource_type,
                save,
                instance=instance,
                # The view's own check asks the view's permissions.
                requirement=None,
                using=db,
                covered=covered,
                queryset=queryset,
                serialize=self.serialized_policy_save,
                recheck=self.check_post_save_permissions,
            )
        except access.AccessError as exc:
            raise drf_error(exc, request) from exc

    def cache_resource_type(self, *objects):
        """Cache the view's type (`get_resource_type`) on each `Resource` of
        `objects` (lists and tuples of them too) that is of it."""
        core_writes.cache_resource_type(self.get_resource_type(), *objects)

    def conditional_create_lock(self, using):
        """A conditional create's lock: the type's row (`type_locked`), so
        that two conditional creates of the type run one at a time and the
        second is evaluated against the list the first left."""
        return core_writes.conditional_create_lock(self.get_resource_type(), using)

    def create(self, request):
        """Create a resource from the body, once the request's preconditions
        are evaluated against the list, under the type's lock when they need
        its validators (`LockedWriteMixin`); other creates take no lock.
        Evaluated once: scrudful has evaluated them, before this runs, when
        this ``create`` (as ``scrudful_viewset`` decorates it) is the outer
        scrudful handler (`list_preconditions_evaluated`). Called from a
        handler scrudful evaluates nothing for (an ``@action`` whose
        ``@scrudful`` has no list validators), this evaluates them
        (`list_precondition_response`): a stale ``If-Match`` is its 412."""
        if not self.list_preconditions_evaluated():
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
        try:
            core_writes.destroy(
                request,
                self.get_resource_type(),
                instance,
                # The view's own check asks the view's permissions.
                requirement=None,
                using=self.locked_write_db or write_db(),
                covered=self.checks_workflow(request),
                check=self.check_post_save_permissions,
            )
        except access.AccessError as exc:
            raise drf_error(exc, request) from exc
        return Response(status=status.HTTP_204_NO_CONTENT)

    def retrieve(self, request, slug: str):
        """Return the resource for the given resource type name."""
        instance = self.get_instance(request, slug)
        serializer = self.get_serializer(instance=instance, many=False)
        return Response(serializer.data)

    def check_permissions(self, request):
        """Refuse an empty body, a body whose ``rbac_context`` cannot be
        stored, one nested deeper than `max_body_depth` and one with a NUL
        character in a string (`check_body`), with 400, before scoped-rbac's
        request check (which would authorize an empty body as ``{}`` and
        refuse an empty context list with 403 for everyone): they depend on
        the body alone. scoped-rbac's
        request check then reads the body itself (``request.data``). A method
        the view does not serve skips the body checks (its body is not
        parsed), so that it gets its 405 whatever its body, as scoped-rbac's
        request check leaves it to. So does scrudful's body-less copy of an
        OPTIONS, on which it checks one method for ``Allow``
        (``scoped_rbac.access.checks_allow``): scoped-rbac checks a POST or
        PUT there without a body, and the copy is no write to refuse. A
        request that *arrives* so is refused before it gets here
        (`LockedWriteMixin.initial`)."""
        try:
            if request.method in self.allowed_methods and not access.checks_allow(
                request
            ):
                check_body(request, self)
            with self.write_checks(request):
                super().check_permissions(request)
            self.check_request_hooks(request)
        except ScrudException as exc:
            # DRF's browsable API calls this outside ``dispatch`` and
            # catches ``APIException``s only.
            raise as_drf(exc) from exc

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
        # The view's removal (`scrud_django.filters.readable_removal`), not
        # `scrud_django.core.lists.list_last_modified`'s: it also asks that
        # the view's RBAC filter and ``contexts_for`` be the stamped ones.
        removed = readable_removal(
            self.request,
            self,
            self.get_resource_type(),
            using=self.list_queryset().db,
        )
        return latest(newest, removed)

    def list(self, request):
        """The resources of the type the requester may read, a page at a time."""
        page = self.list_page()

        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return Response(self.get_paginated_response(serializer.data))

        serializer = self.get_serializer(instance=self.list_queryset(), many=True)
        return Response(serializer.data)


class GenericViewMixin(
    ScrudErrorsMixin, UniformErrorsMixin, LockedWriteMixin, ListValidatorsMixin
):
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
      `locked_write_database` (where the view's model is written) and
      `get_object` locks the row the URL names (`lock_object_row`, a plain
      query, whatever the view's queryset is) before it looks it up, so
      scrudful's preconditions, the object check and your
      ``update``/``destroy`` see one row, and an error response (4xx,
      5xx) rolls the write back;
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
      ``create`` runs: a 412 or 400 never calls it. A POST handler whose
      ``@scrudful`` has no list validators (an ``@action``, a decorated
      handler of your own) is not evaluated by scrudful, nor is a ``create``
      it calls, nor a ``create`` override not decorated again with
      ``scrudful_viewset`` that does not call ``super().create()``: call
      `list_precondition_response` first in it, and return its response.
      (An undecorated handler that calls the scrudful ``create`` is
      evaluated by it.) Such a POST runs in one transaction
      (`LockedWriteMixin`), and a response that is no error whose
      preconditions nothing evaluated is refused (``ImproperlyConfigured``)
      and rolled back (but for an ``If-Match: *`` without
      ``If-None-Match``, which passes on a list). A handler that gets
      scrudful's 412 or 400, or `list_precondition_response`'s, must return
      it: one dropped is not detected.
      When the preconditions need the
      list's validators, the evaluation and your ``create`` run in that
      transaction holding the collection's lock
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

    # Whether get_queryset() reads a replica of locked_write_database()
    # (using()): set, `check_write_database` allows it; that is all it does.
    # Inside a locked write or a conditional POST's transaction the lookup,
    # the list's validators and the locks read the write database whether
    # or not it is set. Unset, such a view is refused (ImproperlyConfigured)
    # on every PUT, PATCH, DELETE or conditional POST it is authorized for.
    reads_replica: bool = False

    class Meta(ScrudfulMeta):
        pass

    def conditional_create_lock(self, using):
        """A conditional create's lock: scoped-rbac's collection lock of the
        view's model (``scoped_rbac.conditional.lock_collection``), held until
        the create's transaction ends."""
        return _collection_locked(self.get_queryset().model, using)

    def locked_write_database(self):
        """The database of a locked write (and of a POST to the list whose
        preconditions must be evaluated): where the view's model is written,
        ``router.db_for_write`` of the model of the ``queryset`` attribute,
        else of the ``serializer_class`` attribute's ``Meta.model``, else the
        default database. It is called before DRF has wrapped the request
        (``self.request`` is Django's, ``self.action`` unset), so it reads no
        ``get_queryset()``: a view whose model only ``get_queryset()`` names,
        written elsewhere, overrides this, as does a view that writes to a
        database the router does not (``using()`` in ``get_queryset()``,
        its creates there too); a ``get_queryset()`` reading a replica sets
        `reads_replica` instead (`check_write_database` refuses either
        mismatch)."""
        queryset = getattr(self, "queryset", None)
        model = getattr(queryset, "model", None)
        if model is None:
            meta = getattr(getattr(self, "serializer_class", None), "Meta", None)
            model = getattr(meta, "model", None)
        return DEFAULT_DB_ALIAS if model is None else router.db_for_write(model)

    def check_write_database(self, using):
        """Refuse (``ImproperlyConfigured``) a transaction on database `using`
        when the view's objects are written or read elsewhere: the
        transaction and its lock would serialize and roll back nothing of
        them, and the list's validators would be read on another database
        than the one the requester's GET reads. Only for the inherited
        `locked_write_database`, which reads no ``get_queryset()`` (it runs
        before DRF wraps the request) and so may not know the model:

        - the router writes the model (``get_queryset()``'s) elsewhere; or
        - ``get_queryset()`` selects another database (``using()``), unless
          the view declares it a replica of `using` (`reads_replica`). Either
          way, inside the transaction the lookup, the list's validators and
          the locks read `using`, and so do the item validators, from the
          locked object, with ``Meta.object_validators`` on, as
          `ScrudfulMeta` has it (off, item validators of your own computed
          from ``get_queryset()`` read where it says).

        A view that overrides `locked_write_database` has chosen its
        database: its writes, creates included (a ``ModelSerializer`` creates
        where the router writes), must go there, which this cannot check."""
        if type(self).locked_write_database is not (
            GenericViewMixin.locked_write_database
        ):
            return
        queryset = self.get_queryset()
        model = queryset.model
        written = router.db_for_write(model)
        if written != using:
            raise ImproperlyConfigured(
                f"{type(self).__name__} opens its transaction on the "
                f"{using!r} database, and the router writes "
                f"{model._meta.label} on {written!r}: name the model with a "
                "queryset or serializer_class attribute, or override "
                f"locked_write_database() to return {written!r}."
            )
        # A QuerySet's db is the router's read database unless using() chose
        # one (_db).
        selected = queryset._db
        if selected not in (None, using) and not self.reads_replica:
            raise ImproperlyConfigured(
                f"{type(self).__name__}'s get_queryset() reads "
                f"{model._meta.label} on {selected!r}, and its transaction "
                f"opens on {using!r}, where the router writes it: if "
                f"{selected!r} is a replica of {using!r}, set reads_replica = "
                "True on the view; if the view writes there, override "
                f"locked_write_database() to return {selected!r} (and create "
                "there too)."
            )

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
        404. Inside `unlocked_lookups` no row is locked."""
        previous, self._scrud_in_get_object = self._scrud_in_get_object, True
        try:
            if self.locked_write_db is None or self.lookups_unlocked:
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
