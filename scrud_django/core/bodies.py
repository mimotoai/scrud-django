"""The checks of a write's request body without DRF: a body that is missing,
nested too deeply, holds a NUL character, or names an ``rbac_context`` that
cannot be stored. They depend on the body alone, so a caller with any kind of
request passes the parsed body in."""

from collections.abc import Mapping

from django.db import connections, router

from scrud_django.exceptions import (
    RbacContextInvalidError,
    RequestBodyMissingError,
    RequestBodyNulError,
    RequestBodyTooDeepError,
)
from scrud_django.mixins.resource_mixin import rbac_contexts_of
from scrud_django.models.resource import MAX_CONTEXT_BYTES, Resource, context_fits

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


def check_present(present):
    """Raise `RequestBodyMissingError` (400) when the body is not `present`:
    an empty one, or one without a ``Content-Length``. A framework that reads
    it as ``{}`` would store an empty resource (a POST) or wipe the stored
    content (a PUT)."""
    if not present:
        raise RequestBodyMissingError()


def check_depth(body):
    """Raise `RequestBodyTooDeepError` (400) for a `body` nested deeper than
    `max_body_depth`."""
    limit = max_body_depth()
    if body_depth(body, limit) > limit:
        raise RequestBodyTooDeepError(limit)


def check_nul(body):
    """Raise `RequestBodyNulError` (400) for a `body` with a NUL character in
    any string (`body_has_nul`): PostgreSQL's ``jsonb`` cannot hold one, so it
    is refused on every database alike."""
    if body_has_nul(body):
        raise RequestBodyNulError()


def check_contexts(body):
    """Raise `RbacContextInvalidError` (400) when a `body`'s ``rbac_context``
    is an empty list, which would store the resource in no context (only a
    superuser could read it; a server-side ``ResourceRegistration`` may still
    store one), or names a context longer than its rows hold (`context_fits`:
    `MAX_CONTEXT_BYTES` of UTF-8)."""
    if not isinstance(body, Mapping):
        return
    context = body.get("rbac_context")
    if isinstance(context, (list, tuple)) and not context:
        raise RbacContextInvalidError(
            "rbac_context must name at least one context; leave it out (or give "
            '"") for the default context.'
        )
    if not all(context_fits(c) for c in rbac_contexts_of(body)):
        raise RbacContextInvalidError(
            f"An rbac_context may be at most {MAX_CONTEXT_BYTES} bytes long in UTF-8."
        )


def check_body(body, *, present):
    """Check the `body` of a POST, PUT or PATCH (parsed JSON), in order:
    `check_present`, `check_depth`, `check_nul`, `check_contexts`."""
    check_present(present)
    check_depth(body)
    check_nul(body)
    check_contexts(body)
