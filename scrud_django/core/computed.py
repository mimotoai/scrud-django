"""A computed type's answer without DRF: the content its ``compute`` gives for
a key and the requester, the strong ETag of that content, the
``If-None-Match`` that makes it a 304, and its schema and JSON-LD context
``Link``, as ``scrud_django.computed.ComputedResourceViewSet`` and the ninja
computed route (``scrud_django.ninja.computed``) answer them, to an
authenticated requester only (`check_authenticated`, DRF's
``IsAuthenticated``)."""

import contextlib
import datetime
import decimal
import hashlib
import ipaddress
import json
import uuid
from collections.abc import Callable, Mapping

from django.db.models.query import QuerySet
from django.utils import timezone
from django.utils.encoding import force_str
from django.utils.functional import Promise
from scoped_rbac import access

from scrud_django.core.types import type_options_for_slug
from scrud_django.headers import get_context_uri_for, get_schema_uri_for

#: The relations and media types of a computed resource's Links, in order.
SCHEMA_REL = ("describedby", "application/schema+json")
CONTEXT_REL = ("http://www.w3.org/ns/json-ld#context", "application/ld+json")


class ContentEncoder(json.JSONEncoder):
    """JSON of the values DRF's ``JSONEncoder`` writes, written as it writes
    them (`computed_etag` hashes what ``ComputedResourceViewSet`` hashed with
    it): lazy text, dates and times (an aware time refused), durations,
    decimals, UUIDs, IP addresses, querysets, bytes and other sequences and
    iterables."""

    def default(self, obj):
        if isinstance(obj, Promise):
            return force_str(obj)
        if isinstance(obj, datetime.datetime):
            text = obj.isoformat()
            return text[:-6] + "Z" if text.endswith("+00:00") else text
        if isinstance(obj, datetime.date):
            return obj.isoformat()
        if isinstance(obj, datetime.time):
            if timezone.is_aware(obj):
                raise ValueError("JSON can't represent timezone-aware times.")
            return obj.isoformat()
        if isinstance(obj, datetime.timedelta):
            return str(obj.total_seconds())
        if isinstance(obj, decimal.Decimal):
            return float(obj)
        if isinstance(
            obj,
            uuid.UUID
            | ipaddress.IPv4Address
            | ipaddress.IPv6Address
            | ipaddress.IPv4Network
            | ipaddress.IPv6Network
            | ipaddress.IPv4Interface
            | ipaddress.IPv6Interface,
        ):
            return str(obj)
        if isinstance(obj, QuerySet):
            return tuple(obj)
        if isinstance(obj, bytes):
            return obj.decode()
        if hasattr(obj, "tolist"):
            return obj.tolist()
        if hasattr(obj, "__getitem__"):
            cls = list if isinstance(obj, list | tuple) else dict
            with contextlib.suppress(Exception):
                return cls(obj)
        elif hasattr(obj, "__iter__"):
            return tuple(obj)
        return super().default(obj)


def computed_etag(content) -> str:
    """The strong ETag of `content`: the SHA-256 of its canonical JSON (keys
    sorted, compact, written by `ContentEncoder`)."""
    canonical = json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        cls=ContentEncoder,
    )
    return '"' + hashlib.sha256(canonical.encode()).hexdigest() + '"'


def computed_answer(
    request,
    resource_type,
    key: str,
    *,
    compute: Callable[..., Mapping] | None = None,
) -> tuple[Mapping, str]:
    """The content of `resource_type`'s resource `key` for `request`, and its
    ETag (`computed_etag`): ``compute(request, key)``, by default the type's
    registered ``compute`` (``computed_resource_type(..., compute=)``;
    ``NotImplementedError`` without one). It may raise ``Http404`` or
    ``PermissionDenied``; content that is not a mapping is a ``TypeError``,
    never served."""
    if compute is None:
        compute = type_options_for_slug(resource_type.slug).compute
        if compute is None:
            raise NotImplementedError
    content = compute(request, key)
    if not isinstance(content, Mapping):
        raise TypeError(
            f"{_name_of(compute)} returned a {type(content).__name__}, not a mapping."
        )
    return content, computed_etag(content)


def _name_of(compute) -> str:
    """`compute`'s name in an error: ``<class>.<method>`` for a bound method
    (the class of its instance), its qualified name otherwise."""
    owner = getattr(compute, "__self__", None)
    if owner is not None:
        return f"{type(owner).__name__}.{compute.__name__}"
    return getattr(compute, "__qualname__", repr(compute))


def not_modified(request, etag: str) -> bool:
    """Whether `request`'s ``If-None-Match`` names `etag` (weak comparison)
    or is ``*``: a GET or HEAD then answers 304."""
    header = request.headers.get("If-None-Match", "")
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == etag:
            return True
    return False


def computed_link(request, resource_type, *, namespace: str | None = None):
    """The ``Link`` field value of a resource of `resource_type`: its schema
    (``rel="describedby"``) and its JSON-LD context, each absolute against
    `request`, within the URL namespace it was resolved under (`namespace`
    first without one), either left out when the type has none; ``None``
    without either, or without a type."""
    if resource_type is None:
        return None
    links = [
        f'<{url}>; rel="{rel}"; type="{media_type}"'
        for url, (rel, media_type) in (
            (get_schema_uri_for(resource_type, request, namespace), SCHEMA_REL),
            (get_context_uri_for(resource_type, request, namespace), CONTEXT_REL),
        )
        if url
    ]
    return ", ".join(links) or None


def check_authenticated(request) -> None:
    """Refuse (``access.AccessDenied``: a 401 for the anonymous) a request
    whose user is not authenticated, as DRF's ``IsAuthenticated`` refuses
    it."""
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated):
        raise access.AccessDenied("Authentication credentials were not provided.")
