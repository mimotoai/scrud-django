"""The parity harness of the two front ends: one exchange (a request and its
response) on a front end's mount in `tests.parity_urls`, and a case run on
both on fresh data, so that their responses compare byte for byte.

A response is normalized as the design allows, and in no other way:

- the mount prefix (``/drf/``, ``/ninja/``) becomes ``{mount}``;
- ``Date``, ``Vary``, ``WWW-Authenticate`` and ``Set-Cookie`` are dropped;
- the target of the HTTP Schema Resource link (`HSR_REL`) becomes ``{hsr}``
  (each front end serves its HSRs at URLs of its own);
- each key a case's setup created becomes ``{name}``, and the key of each
  resource the request itself created (a POST's) ``{created}`` (then
  ``{created-2}`` ...), in URL path segments only (PostgreSQL's sequences do
  not roll back, so keys differ between the front ends' runs; a list's ETag
  hashes its resources' keys, so a list case's setup stores its resources
  with keys of its own, the same in both runs);
- ``Content-Length`` is that of the body once so replaced.

Everything else (status, every other header, the body's bytes) must match.
"""

import datetime
import itertools
import json
import re
import uuid
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from unittest import mock

from django.db import transaction
from django.test import Client

from scrud_django import registration
from scrud_django.models import Resource

from .resource_types import ninja_type_specs

FRONT_ENDS = ("drf", "ninja")

#: Each front end's mount in `tests.parity_urls`.
MOUNTS = {"drf": "/drf/", "ninja": "/ninja/"}

#: The placeholder the mount prefix becomes, in paths, headers and bodies.
MOUNT = "{mount}"

#: The relation of the HTTP Schema Resource link scrudful adds to the
#: responses of a described path, and the placeholder its target becomes.
HSR_REL = "https://w3id.org/semantic-http/ns#http-schema-resource-document"
HSR = "{hsr}"

#: The response headers dropped.
IGNORED_HEADERS = frozenset({"date", "vary", "www-authenticate", "set-cookie"})

#: The time of a case's setup, then of its request: `parity` freezes the
#: clock, so both front ends store and answer the same times.
SETUP_TIME = datetime.datetime(2026, 10, 1, 12, 0, 0, tzinfo=datetime.UTC)
REQUEST_TIME = SETUP_TIME + datetime.timedelta(minutes=1)

#: What a resource's (and a resource type's) ``etag`` and ``modified_at`` are
#: made of: `parity` replaces them with its clock's.
CLOCK_TARGETS = {
    "django.utils.timezone.now": "now",
    "scrud_django.models.resource.uuid4": "uuid4",
    "scrud_django.registration.uuid4": "uuid4",
}


@dataclass(frozen=True)
class Exchange:
    status: int
    headers: dict[str, str]
    body: str | None


def _no_setup(client):
    return {}


@dataclass(frozen=True)
class Case:
    """One request of the parity corpus.

    - `path` holds ``{mount}`` (the front end's mount) and may hold
      ``{name}`` for a key `setup` returns.
    - `setup(client)` creates what the case needs (rows, users, grants;
      logging `client` in when the request is not anonymous) and returns
      ``{name: key}`` of the rows whose keys appear in URLs: keys of one
      table (the resources'), so that no two are alike.
    - `body` (sent as JSON unless a ``str`` or ``bytes``) and `headers` may
      be callables of those names, called after `setup`.
    """

    id: str
    method: str
    path: str
    setup: Callable[[Client], Mapping[str, object]] = _no_setup
    body: object = None
    headers: Mapping[str, str] | Callable[[Mapping], Mapping[str, str]] | None = None


class Clock:
    """``now()`` at `SETUP_TIME`, then at `REQUEST_TIME`; ``uuid4()`` a
    sequence started afresh for each front end."""

    def __init__(self):
        self.time = SETUP_TIME
        self.ids = itertools.count(1)

    def now(self):
        return self.time

    def uuid4(self):
        return uuid.UUID(int=next(self.ids))


def parity(case: Case) -> tuple[Exchange, Exchange]:
    """`case`'s exchange on each front end (`FRONT_ENDS`, in that order),
    each on fresh data: its setup and request run in a transaction rolled
    back afterwards, with the clock frozen (`CLOCK_TARGETS`), once both
    front ends serve their types (`serve_both`)."""
    serve_both()
    return tuple(_run(case, front_end) for front_end in FRONT_ENDS)


def serve_both():
    """Load `tests.parity_urls`, and register every type the two front ends
    serve, in the test's transaction: before, and outside, each run's
    transaction and clock. Otherwise the URLconf would be imported on the
    first run's first request, registering the types the session does not
    commit (``django_db_setup``: the hooked and computed ones) in that run's
    transaction only, and with its clock: the next run would find them gone,
    and the first run's ETags shifted. Registering a type already stored
    writes nothing."""
    from . import parity_urls  # noqa: F401

    for spec in [*registration.built_in_type_specs(), *ninja_type_specs()]:
        spec.register()


def _run(case, front_end):
    clock = Clock()
    with ExitStack() as stack:
        for target, attribute in CLOCK_TARGETS.items():
            stack.enter_context(mock.patch(target, getattr(clock, attribute)))
        with transaction.atomic():
            try:
                client = Client()
                keys = dict(case.setup(client))
                clock.time = REQUEST_TIME
                body = case.body(keys) if callable(case.body) else case.body
                headers = case.headers(keys) if callable(case.headers) else case.headers
                path = case.path.format(mount=MOUNT, **keys)
                stored = set(Resource.objects.values_list("pk", flat=True))
                return exchange(
                    client,
                    front_end,
                    case.method,
                    path,
                    body=body,
                    headers=headers,
                    keys=keys,
                    stored=stored,
                )
            finally:
                transaction.set_rollback(True)


def exchange(
    client,
    front_end,
    method,
    path,
    *,
    body=None,
    headers=None,
    keys=None,
    stored=None,
) -> Exchange:
    """Send `method` `path` (whose ``{mount}`` becomes `front_end`'s mount)
    with `client`, `body` as JSON (a ``str`` or ``bytes`` as is) and
    `headers`, and return the response `normalized`, with `keys` (``{name:
    key}``) as the keys to replace. Given `stored`, the keys of the resources
    stored before the request, each resource the request created is
    ``created`` (then ``created-2`` ...), in key order."""
    mount = MOUNTS[front_end]
    kwargs = {"headers": dict(headers or {})}
    if body is not None:
        kwargs["data"] = body if isinstance(body, str | bytes) else json.dumps(body)
        kwargs["content_type"] = "application/json"
    url = path.replace(MOUNT, mount)
    response = client.generic(method, url, **kwargs)
    keys = dict(keys or {})
    if stored is not None:
        created = Resource.objects.exclude(pk__in=stored).order_by("pk")
        for index, pk in enumerate(created.values_list("pk", flat=True), start=1):
            keys["created" if index == 1 else f"created-{index}"] = pk
    length_of = None
    if method == "HEAD" and response.has_header("Content-Length"):
        # A HEAD's Content-Length is that of the GET's body, which it leaves
        # out: it counts the mount and the keys too.
        get = client.generic("GET", url, **kwargs)
        assert get.status_code == response.status_code, get
        length_of = get.content
    return normalized(response, mount, keys, length_of=length_of)


def normalized(response, mount, keys, *, length_of=None) -> Exchange:
    """`response` as an `Exchange`, normalized (see the module docstring):
    `mount` is the front end's, `keys` ``{name: key}``, `length_of` the body
    a HEAD's ``Content-Length`` counts (the response's own otherwise)."""
    replace = _replacer(mount, keys)
    content = response.content if length_of is None else length_of
    headers = {}
    for name, value in response.headers.items():
        if name.lower() in IGNORED_HEADERS:
            continue
        if name.lower() == "content-length":
            if int(value) != len(content):
                raise AssertionError(f"Content-Length {value} is not the body's")
            value = str(len(replace(content.decode()).encode()))
        elif name.lower() == "link":
            value = replace(masked_hsr(value))
        else:
            value = replace(value)
        headers[name] = value
    text = response.content.decode() if response.content else None
    return Exchange(
        status=response.status_code,
        headers=headers,
        body=None if text is None else replace(text),
    )


def _replacer(mount, keys):
    """The replacement of `keys` in path segments, then of `mount`, in a
    text. ``ValueError`` for two names of one key, which no replacement could
    tell apart."""
    names = {}
    for name, key in keys.items():
        if str(key) in names:
            raise ValueError(f"{name!r} and {names[str(key)]!r} are both {key!r}")
        names[str(key)] = name
    # A segment: after a "/", up to the next "/", or the end of the path (its
    # query, fragment, or the end of the URL in JSON text or a Link).
    segment = re.compile(
        r"(?<=/)(" + "|".join(map(re.escape, names)) + r')(?=[/?#"><]|$)'
    )

    def replace(text):
        if names:
            text = segment.sub(lambda match: "{" + names[match.group(1)] + "}", text)
        return text.replace(mount, MOUNT)

    return replace


def split_links(value):
    """The link-values of a Link field value (no URL here holds ``", <"``)."""
    return [part if part.startswith("<") else "<" + part for part in value.split(", <")]


def masked_hsr(value):
    """The Link field value `value` with its HSR link's target `HSR`."""
    return ", ".join(
        f"<{HSR}>{link[link.index('>') + 1 :]}" if f'rel="{HSR_REL}"' in link else link
        for link in split_links(value)
    )
