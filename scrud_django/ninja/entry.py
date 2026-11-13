"""The checks every scrud request on the ninja front end passes where it enters,
in the order DRF's ``dispatch`` / ``initial`` asks them."""

import copy
import functools
import inspect

from django.urls import URLPattern
from scoped_rbac import access
from scoped_rbac.ninja import resolve_user
from scrudful._django import precondition_headers
from scrudful._hsr import is_preflight
from scrudful.utils import join_links, merge_expose_headers

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


#: The relation of scrudful's HTTP Schema Resource link.
HSR_REL = "https://w3id.org/semantic-http/ns#http-schema-resource-document"

#: The path segment of scrudful's HTTP Schema Resource routes (public).
HSR_SEGMENT = "_hsr"

#: A method no scrud route serves, which `_hsr_link_of` asks a path.
UNSERVED_METHOD = "TRACE"


def authenticate_first(api, serves) -> None:
    """Have `api`'s URL patterns whose name `serves` (a predicate) resolve
    the requester (``resolve_user``) before anything else of theirs, every
    method alike (but a CORS preflight, which each OPTIONS answers itself),
    as DRF authenticates every request in ``initial()`` before it looks for
    the method's handler: so credentials refused (a ``NINJA_USER`` hook
    raising ninja's ``AuthenticationError``, a session user's failed CSRF
    check) are refused for a method the path does not serve too (where
    scoped-rbac's 405 lets anything but an access error escape), and for an
    OPTIONS whose operation does not resolve the requester (scoped-rbac's
    role routes'). The refusal is answered by `api`, with the HTTP Schema
    Resource link the path gives every answer (`_described_as`), as the DRF
    view answers it. Its HSR routes (public) are left alone.

    Call it before ``scrud_django.ninja.errors.install`` (so that its
    wrapping is innermost: scoped-rbac's own refusals, such as a rewritten
    OPTIONS's, come first), before ``api.urls`` is read."""
    get_urls = api._get_urls

    @functools.wraps(get_urls)
    def _get_urls():
        patterns = get_urls()
        for pattern in patterns:
            if (
                isinstance(pattern, URLPattern)
                and pattern.name
                and serves(pattern.name)
                and HSR_SEGMENT not in str(pattern.pattern).split("/")
            ):
                pattern.callback = _authenticating_first(api, pattern.callback)
        return patterns

    api._get_urls = _get_urls


def _authenticating_first(api, view):
    @functools.wraps(view)
    def authenticated(request, *args, **kwargs):
        if not is_preflight(request.method, request.headers.get):
            try:
                resolve_user(request)
            except Exception as exc:  # answered as the API answers it
                refused = api.on_exception(request, exc)
                return _described_as(refused, _hsr_link_of(view, request, args, kwargs))
        return view(request, *args, **kwargs)

    return authenticated


def _hsr_link_of(view, request, args, kwargs):
    """The HTTP Schema Resource link `view`'s path gives its answers to
    `request` (``None`` for a path that has none): that of its answer to a
    copy of `request` with a method it does not serve (`UNSERVED_METHOD`),
    which scrudful answers 405 before anything else of the path runs."""
    probe = copy.copy(request)
    probe.method = UNSERVED_METHOD
    answer = view(probe, *args, **kwargs)
    for link in _split_links(answer.get("Link", "")):
        if f'rel="{HSR_REL}"' in link:
            return link
    return None


def _described_as(refused, hsr_link):
    """`refused` with `hsr_link`, ``Link`` and ``Allow`` exposed, as scrudful
    gives every answer of a described path; as it is without one."""
    if hsr_link is None:
        return refused
    refused["Link"] = join_links(refused.get("Link"), hsr_link)
    refused["Access-Control-Expose-Headers"] = merge_expose_headers(
        refused.get("Access-Control-Expose-Headers"), ("Link", "Allow")
    )
    return refused


def _split_links(value):
    """The link-values of a Link field value (no URL here holds ``", <"``)."""
    if not value:
        return []
    return [part if part.startswith("<") else "<" + part for part in value.split(", <")]
