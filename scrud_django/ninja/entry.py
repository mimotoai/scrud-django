"""The checks every scrud request on the ninja front end passes where it enters,
in the order DRF's ``dispatch`` / ``initial`` asks them."""

import functools
import inspect

from scoped_rbac import access
from scoped_rbac.ninja import resolve_user
from scrudful._django import precondition_headers

from scrud_django.core.lists import has_list_preconditions
from scrud_django.core.writes import write_transaction
from scrud_django.registration import write_db

# The methods whose whole operation runs in one write transaction, as DRF's
# ``LockedWriteMixin.dispatch`` runs them.
WRITE_METHODS = frozenset({"PUT", "PATCH", "DELETE"})


def entry(run):
    """Decorate a ninja operation's ``run`` (``router.add_decorator(entry,
    mode="view")``, on every scrud router), so that, for each request but a
    real OPTIONS (a CORS preflight is one), before anything else of the
    operation:

    1. a request rewritten from an OPTIONS is refused
       (``access.refuse_rewritten_options``: 403, 401 for the anonymous),
       as DRF's ``initial`` refuses it;
    2. the user is resolved (``scoped_rbac.ninja.resolve_user``, with the
       session user's CSRF check: 403), as DRF authenticates;
    3. a PUT, PATCH or DELETE, or a POST carrying ``If-Match``,
       ``If-None-Match`` or ``If-Unmodified-Since``, runs whole in one
       transaction on `write_db` (``core.writes.write_transaction``), rolled
       back when it answers a status of 400 or more (or raises), as DRF's
       ``dispatch`` runs it.

    A real OPTIONS passes straight through: scrud's OPTIONS operations
    resolve the user themselves, once they know it is no CORS preflight
    (``scrud_django.ninja.routers``). A refusal (or what a
    ``SCOPED_RBAC["NINJA_USER"]`` hook raises: ninja's
    ``AuthenticationError`` is a 401) is answered by the operation's API
    (``api.on_exception``). Operations are sync
    (``TypeError`` for an async one)."""
    if inspect.iscoroutinefunction(run):
        raise TypeError("scrud's ninja entry checks serve sync operations only.")

    @functools.wraps(run)
    def scrud_entry(request, *args, **kwargs):
        if request.method == "OPTIONS":
            return run(request, *args, **kwargs)
        try:
            access.refuse_rewritten_options(request)
            resolve_user(request)
        except Exception as exc:
            # An access error, or what a ``NINJA_USER`` hook raises (ninja's
            # ``AuthenticationError``: 401), as the operation would answer it.
            return _api_of(run).on_exception(request, exc)
        if not _in_write_transaction(request):
            return run(request, *args, **kwargs)
        with write_transaction(write_db()) as rollback:
            response = run(request, *args, **kwargs)
            if response.status_code >= 400:
                rollback()
        return response

    return scrud_entry


def _in_write_transaction(request) -> bool:
    if request.method in WRITE_METHODS:
        return True
    return request.method == "POST" and has_list_preconditions(
        precondition_headers(request)
    )


def _api_of(run):
    """The API of the operation whose ``run`` `run` is, or wraps (through
    decorators that use ``functools.wraps``), as scrudful finds it."""
    return inspect.unwrap(run).__self__.api
