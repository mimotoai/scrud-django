"""The studio's reads and writes of resources, made through the framework
neutral core (`scrud_django.core`) as the user of the studio request, and
their answers as JSON: `write` (a create, update, delete or read of one item,
answered as the resource route answers it, its refusals included),
`as_response` (the studio's HTTP answer to one) and `validate_draft` (a write
asked its checks and not made).

Each runs on a request of its own: `request`'s host and scheme, its user,
the write's method and a JSON body, with no header but the ``If-Match``
given, and no session or cookies (the user is `request`'s, not
authenticated again). So every
check of the API applies to the studio user (the type's requirement,
branch visibility, the hooks of the UI types) and the studio adds no
rights. A type the UI does not serve (the API does not route it, or the UI
refuses its routes' authorization, `scrud_django.ui.pages.refusal`) is the
404 of an unknown slug, as at its pages. No CSRF check applies (the studio request that makes
one passed it: an unsafe `write` from a safe request is refused, ruling
S8)."""

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured
from django.http import Http404, HttpResponse
from django.test.client import RequestFactory
from django.urls import NoReverseMatch, reverse
from scrudful.conditional import quote_etag

from scrud_django.core import resources, writes
from scrud_django.core.bodies import check_body
from scrud_django.core.errors import error_answer
from scrud_django.core.json import JSON, json_response
from scrud_django.core.types import requirement_for
from scrud_django.models import ResourceType
from scrud_django.registration import get_resource_type_for, type_db, write_db

logger = logging.getLogger(__name__)

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

# The htmx event a successful write triggers (``HX-Trigger``).
SAVED_EVENT = "scrud-studio-saved"

PROBLEM_JSON = "application/problem+json"


@dataclass(frozen=True)
class Answer:
    """What a resource route answers: its `status`, the `content` of the
    JSON body (``None``: no body), the `etag` of the item as a header has it
    (in quotes: what a later ``If-Match`` sends), the `location` of a created
    item, and the `problem` body of a refusal (then `content` is ``None``)."""

    status: int
    content: Mapping | None = None
    etag: str | None = None
    location: str | None = None
    problem: dict | list | None = None
    # The ``Content-Type`` of a refusal's body.
    problem_type: str = PROBLEM_JSON

    @property
    def ok(self) -> bool:
        return self.status < 400

    @property
    def body(self):
        """The JSON body of the answer: its content, or its problem."""
        return self.problem if self.problem is not None else self.content


def write(
    request,
    slug: str,
    method: str,
    *,
    pk=None,
    body: Mapping | None = None,
    if_match: str | None = None,
) -> Answer:
    """The answer of the resource route of type `slug` (its list, or with
    `pk` one item) to a `method` request made for the user of `request`:
    ``POST`` creates (from `body`), ``PUT`` updates item `pk` to `body`
    with `if_match` (an ETag in quotes, as sent), ``DELETE`` deletes it and
    ``GET`` reads it. A write runs in one transaction on the write
    database, which any refusal rolls back; the refusal is answered, not
    raised (`Answer.problem`).

    An unsafe `method` is refused (``ImproperlyConfigured``, a programming
    error) when `request`'s own method is safe, which Django's CSRF check
    lets through. A `pk` no item has (``""``, or one with a ``/``) is the
    404 of the API. A precondition that fails (no `if_match` on a PUT or
    DELETE: 428; a stale one: 412) is answered, nothing written."""
    method = method.upper()
    if method not in SAFE_METHODS and request.method.upper() in SAFE_METHODS:
        raise ImproperlyConfigured(
            f"answers.write: a {method} from a {request.method} request would "
            "skip the CSRF check; make it from the studio's POST."
        )
    if method == "GET" and pk is None:
        raise ValueError("answers.write reads one item: a GET needs its pk.")
    internal = _internal(request, slug, method, pk, body, if_match)
    try:
        resource_type = _served_type(slug, None if method == "GET" else type_db())
        requirement = requirement_for(resource_type, slug)
        if method == "GET":
            return _read(internal, resource_type, requirement, pk)
        with writes.write_transaction(write_db()):
            return _write(internal, resource_type, requirement, method, pk, body)
    except Exception as exc:
        return _refusal(exc, internal)


def _read(request, resource_type, requirement, pk) -> Answer:
    obj = resources.load_for_read(
        request, resource_type, pk, requirement=requirement, method="GET"
    )
    content, etag, last_modified = resources.item_read(request, resource_type, obj)
    failed = _precondition(
        request, etag=etag, last_modified=last_modified, method="GET"
    )
    if failed is not None:
        return failed
    return Answer(200, content, quote_etag(etag))


def _write(request, resource_type, requirement, method, pk, body) -> Answer:
    """The answer of the `method` write, as the resource route writes it
    (`writes.create_answer`, `writes.item_write_answer`), inside the
    caller's transaction on the write database."""
    using = write_db()
    if method == "POST":
        answer = writes.create_answer(
            request, resource_type, body, requirement=requirement, using=using
        )
    elif method in ("PUT", "DELETE"):
        answer = writes.item_write_answer(
            request,
            resource_type,
            pk,
            method,
            body,
            requirement=requirement,
            using=using,
        )
    else:
        raise ImproperlyConfigured(f"answers.write: no {method} of an item.")
    if answer.problem is not None:
        return _failed(answer.problem)
    return Answer(
        answer.status,
        answer.content,
        None if answer.etag is None else quote_etag(answer.etag),
        answer.location,
    )


def _precondition(request, *, etag, last_modified, method) -> Answer | None:
    """The answer of a failed precondition of the `method` request on the
    item with `etag` (unquoted) and `last_modified`
    (`resources.item_precondition_answer`), else ``None`` to go on."""
    found = resources.item_precondition_answer(
        request, etag=etag, last_modified=last_modified, method=method
    )
    return None if found is None else _failed(found)


def _failed(found) -> Answer:
    """The answer of the failed precondition `found`, ``(status, body,
    headers)`` (`scrud_django.core.errors.precondition_answer`'s)."""
    status, body, headers = found
    return Answer(
        status,
        etag=headers.get("ETag"),
        problem=body or None,
        problem_type=headers.get("Content-Type", PROBLEM_JSON),
    )


def _served_type(slug, using):
    """The type `slug` (read on `using`), unless the UI does not serve it:
    the API does not route it, or the UI refuses it for the authorization of
    its routes' views (`scrud_django.ui.pages.refusal`), which the core
    would not ask. Refused, it is the very ``Http404`` an unknown slug
    raises (`get_resource_type_for`), so its answer is the same."""
    from scrud_django.ui import pages

    if not pages.routed(slug) or pages.refusal(slug) is not None:
        raise Http404(f"No {ResourceType._meta.object_name} matches the given query.")
    return get_resource_type_for(slug, using=using)


def _refusal(exc: Exception, request) -> Answer:
    """`exc`, raised serving `request`, as the route answers it, else raised
    again (a bug, not a refusal)."""
    answered = error_answer(exc, request)
    if answered is None:
        raise exc
    status, body, headers = answered
    return Answer(
        status,
        problem=body,
        problem_type=headers.get("Content-Type", PROBLEM_JSON),
    )


def _internal(request, slug, method, pk, body, if_match):
    """The Django request of a `method` to the route of type `slug` made for
    the user of `request`: its host and scheme, JSON, and `if_match` as its
    only precondition."""
    try:
        if pk is None:
            url = reverse(f"{slug}-list")
        else:
            url = reverse(f"{slug}-detail", args=[pk])
    except NoReverseMatch:
        url = request.path
    extra = {"HTTP_ACCEPT": JSON}
    if if_match is not None:
        extra["HTTP_IF_MATCH"] = if_match
    meta = getattr(request, "META", {})
    for key in ("HTTP_HOST", "SERVER_NAME", "SERVER_PORT"):
        if key in meta:
            extra[key] = meta[key]
    sent = {}
    if body is not None:
        sent = {"data": json.dumps(body), "content_type": JSON}
    internal = RequestFactory().generic(
        method, url, secure=request.is_secure(), **sent, **extra
    )
    internal.user = getattr(request, "user", None)
    if internal.user is None:
        internal.user = AnonymousUser()
    return internal


def as_response(answer: Answer, *, saved_trigger: bool) -> HttpResponse:
    """`answer` as the studio's HTTP answer: its status and JSON body (a
    refusal's problem, with its media type), its ``ETag`` and ``Location``,
    and on success, when `saved_trigger`, the `SAVED_EVENT` trigger."""
    body = answer.body
    if body is None:
        response = HttpResponse(status=answer.status)
        del response["Content-Type"]
    else:
        response = json_response(
            body,
            status=answer.status,
            content_type=answer.problem_type if answer.problem is not None else JSON,
        )
    if answer.etag is not None:
        response["ETag"] = answer.etag
    if answer.location is not None:
        response["Location"] = answer.location
    if saved_trigger and 200 <= answer.status < 300:
        response["HX-Trigger"] = SAVED_EVENT
    return response


def validate_draft(request, slug: str, body: Mapping, *, pk=None) -> dict | None:
    """Why the write of `body` to the type `slug` (a create, or with `pk` an
    update of that item) would be refused for the user of `request`, as the
    problem body the write would answer; ``None`` when it would not be.
    Nothing is saved: it is asked the write's checks in a transaction that
    rolls back, in the order the write runs them: the request check (the
    body's contexts), for an update the object check of the row it replaces,
    the type's ``prepare_write`` (on a copy of `body`), its JSON Schema and
    the workflow check. The checks a save makes once stored (scoped RBAC's
    re-check of the stored row, the type's ``after_write``) and its
    preconditions (``If-Match``) are not run. A check that fails as a server
    error (status 500 or more) is also logged at ERROR."""
    method = "POST" if pk is None else "PUT"
    internal = _internal(request, slug, method, pk, body, None)
    try:
        resource_type = _served_type(slug, type_db())
        requirement = requirement_for(resource_type, slug)
        using = write_db()
        with writes.write_transaction(using) as rollback:
            try:
                check_body(body, present=True)
                stored = None
                if pk is not None:
                    stored = resources.load_for_read(
                        internal,
                        resource_type,
                        pk,
                        requirement=requirement,
                        method="PUT",
                        lock_using=using,
                        body=body,
                    )
                _validate(internal, resource_type, requirement, body, stored, using)
            finally:
                rollback()
    except Exception as exc:
        answer = _refusal(exc, internal)
        if answer.status >= 500:
            # No answer about the draft: a check that broke.
            logger.error(
                "scrud-ui: checking the draft %s %r failed",
                slug,
                body.get("name"),
                exc_info=True,
            )
        return answer.problem
    return None


def _validate(request, resource_type, requirement, body, stored, using) -> None:
    """`writes.validate_write` of `body`, as `create` and `update` run it
    (the type's ``prepare_write`` given a copy of the body)."""
    prepare_write = writes.write_hooks(resource_type).prepare_write
    readable = None
    if stored is not None:

        def readable():
            return resources.object_readable(
                request, resource_type, stored, requirement
            )

    def prepare(sent):
        if prepare_write is not None and isinstance(sent, Mapping):
            sent = dict(sent)
        return writes.prepared_body(
            request,
            resource_type,
            sent,
            instance=stored,
            using=using,
            prepare_write=prepare_write,
        )

    writes.validate_write(
        request,
        resource_type,
        body,
        stored=stored,
        requirement=requirement,
        readable=readable,
        prepare=prepare,
    )
