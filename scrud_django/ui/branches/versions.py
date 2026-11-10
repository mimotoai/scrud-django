"""A branch's versions: every change to a branch's own items records its next
version, a ``ui-branch-versions`` resource holding the branch's own items
(its overrides, deletion markers included) and the ancestor versions it is
resolved against (its branching point), so that any version's effective state
can be rebuilt. Versions are read-only through the API."""

import contextlib
import contextvars
import logging
from collections.abc import Mapping

import reversion
from django.db import transaction

from scrud_django.exceptions import UIBranchVersionMissingError
from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.scrud_signals import scrud_post_delete, scrud_post_save, write_alias
from scrud_django.ui.branches import (
    ITEM_KINDS,
    MAIN,
    UI_BRANCH_VERSION_SLUG,
    UI_BRANCH_VERSION_TYPE_URI,
)
from scrud_django.ui.branches.model import Branch, branch_of
from scrud_django.ui.branches.store import (
    BranchChainError,
    _read_branches,
    branch_from,
    branches,
    ensure_main,
)

logger = logging.getLogger(__name__)

# The branches item writes marked inside `batched_versions`, as
# ``(name, using)`` in the order they were first marked; None outside it.
_batch: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "scrud_ui_branch_versions_batch", default=None
)

# The branch whose items `discard_branch` is deleting (no version recorded
# for it), or None.
_discarding: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "scrud_ui_branch_discarding", default=None
)


def _stored_branch(name: str, using: str) -> Branch | None:
    """Branch `name` as stored on `using` (``main`` stored first if it is
    virtual), or ``None``. A name missing from the memoised `branches` is
    read again: the branch may be newer than the memo."""
    if name == MAIN:
        return ensure_main(using)
    return branches(using).get(name) or _read_branches(using).get(name)


def _versions(name: str, using: str | None):
    rows = Resource.objects.using(using) if using else Resource.objects
    return rows.filter(
        resource_type__type_uri=UI_BRANCH_VERSION_TYPE_URI, content__branch=name
    )


def _latest_number(name: str, using: str | None) -> int:
    """The number of branch `name`'s latest version, 0 for none. Versions
    are stored in order (`record_version` holds the branch's lock), so the
    latest is the last stored."""
    latest = _versions(name, using).order_by("-pk").values_list("content", flat=True)
    content = latest.first()
    number = content.get("number") if isinstance(content, Mapping) else None
    return number if isinstance(number, int) else 0


def _own_items(type_uri: str, name: str, using: str) -> list:
    """The content of every stored item of `type_uri` on branch `name`,
    lowest pk first. An item stored before branches names none and is
    main's (`branch_of`), so main's are found by reading them all."""
    rows = Resource.objects.using(using).filter(resource_type__type_uri=type_uri)
    if name != MAIN:
        rows = rows.filter(content__branch=name)
    return [
        content
        for content in rows.order_by("pk").values_list("content", flat=True)
        if branch_of(content) == name
    ]


def record_version(branch_name: str, using: str) -> int:
    """Record the next version of branch `branch_name` on database `using`
    and return its number (the last one's plus one; 1 for the first): its
    own items, by kind, and its branching point as ``ancestors``, in the
    branch's context. The branch's row is locked (``select_for_update``)
    until the caller's transaction ends (or, outside one, this function's),
    so that two writes to one branch number their versions one after the
    other. `BranchChainError` when the branch is not stored."""
    with transaction.atomic(using=using, savepoint=False):
        branch = _stored_branch(branch_name, using)
        number = None if branch is None else _record(branch, using)
    if number is None:
        raise BranchChainError(f"There is no branch named {branch_name!r}.")
    return number


def _record(branch: Branch, using: str) -> int | None:
    """`record_version` of the stored `branch`, inside a transaction on
    `using`; ``None`` when its row is gone."""
    locked = list(
        Resource.objects.using(using)
        .select_for_update()
        .filter(pk=branch.pk)
        .values_list("content", flat=True)
    )
    if not locked:
        return None
    # Read under the lock: the branch as it is now.
    branch = branch_from(branch.pk, locked[0])
    number = _latest_number(branch.name, using) + 1
    # Never in a reversion revision, not even the item write's around this
    # one (the type's uses_reversion only keeps its own writes out of one).
    with reversion.create_revision(manage_manually=True, atomic=False):
        _store_version(branch, number, using)
    return number


def _store_version(branch: Branch, number: int, using: str):
    """Store version `number` of `branch`: its own items as they are on
    `using` now."""
    ResourceRegistration.register(
        {
            "branch": branch.name,
            "number": number,
            "rbac_context": branch.context,
            "items": {
                kind: _own_items(type_uri, branch.name, using)
                for kind, type_uri in ITEM_KINDS.items()
            },
            "ancestors": dict(branch.branching_point),
        },
        UI_BRANCH_VERSION_SLUG,
    )


def _record_item_write(name: str, using: str, written: str):
    """Record the next version of branch `name` for the item write described
    by `written`; log, and record nothing, when the branch is not stored."""
    with transaction.atomic(using=using, savepoint=False):
        branch = _stored_branch(name, using)
        if branch is not None and _record(branch, using) is not None:
            return
    logger.warning(
        "scrud-ui: no version recorded for %s: there is no branch named %r.",
        written,
        name,
    )


def current_version(branch_name: str, using: str) -> int:
    """The number of branch `branch_name`'s latest version on `using`,
    recording its first (`record_version`) when it has none."""
    return _latest_number(branch_name, using) or record_version(branch_name, using)


def snapshot(branch_name: str, number: int, using: str | None = None) -> dict:
    """The content of version `number` of branch `branch_name`, read on
    `using` (default: the router's read database).
    `UIBranchVersionMissingError` (404) when it is not stored."""
    content = (
        _versions(branch_name, using)
        .filter(content__number=number)
        .order_by("pk")
        .values_list("content", flat=True)
        .first()
    )
    if not isinstance(content, Mapping) or content.get("number") != number:
        raise UIBranchVersionMissingError(branch_name, number)
    return content


@contextlib.contextmanager
def batched_versions():
    """Inside the block, a write of an item only marks its branch; when the
    block ends without an error, one version is recorded for each marked
    branch, in the order they were first marked. Use it inside the
    transaction of the writes. A block inside another is part of it: the
    outermost records."""
    if _batch.get() is not None:
        yield
        return
    marked: dict = {}
    token = _batch.set(marked)
    try:
        yield
    finally:
        _batch.reset(token)
    # Sorted: concurrent batches then lock their branches' rows in one order.
    for name, using in sorted(marked):
        _record_item_write(name, using, "a batch of item writes")


def record_now(branch_name: str, using: str) -> int:
    """`record_version` of branch `branch_name` now, and, inside
    `batched_versions`, no other version for it when the block ends (unless
    its items are written again after this): a push records the target's
    version before the source's branching point is moved to it."""
    marked = _batch.get()
    if marked is not None:
        marked.pop((branch_name, using), None)
    return record_version(branch_name, using)


def item_written(sender, instance=None, resource_type_uri=None, **kwargs):
    """A ``scrud_post_save`` / ``scrud_post_delete`` receiver: a write of an
    item (a type in ``ITEM_KINDS``, read now, so that every kind added to it
    is versioned) records its branch's next version, inside the write's
    transaction, or, inside `batched_versions`, marks the branch. An item on
    a branch that is not stored records nothing (logged)."""
    if resource_type_uri not in ITEM_KINDS.values():
        return
    name = branch_of(getattr(instance, "content", None))
    if name == _discarding.get():
        return
    using = write_alias(sender, {**kwargs, "instance": instance})
    marked = _batch.get()
    if marked is not None:
        marked.setdefault((name, using))
        return
    pk = kwargs.get("pk", getattr(instance, "pk", None))
    written = f"a {kwargs.get('action')} of {resource_type_uri} pk {pk}"
    _record_item_write(name, using, written)


def discard_branch(name: str, using: str):
    """Delete the own items and the versions of branch `name` on `using`,
    inside the caller's transaction (the branch's deletion), recording no
    version for it: a branch created again under the name starts at
    version 1."""
    token = _discarding.set(name)
    try:
        with transaction.atomic(using=using, savepoint=False):
            for type_uri in ITEM_KINDS.values():
                pks = [
                    pk
                    for pk, content in Resource.objects.using(using)
                    .filter(resource_type__type_uri=type_uri, content__branch=name)
                    .values_list("pk", "content")
                    if branch_of(content) == name
                ]
                Resource.objects.using(using).filter(pk__in=pks).delete()
            pks = list(_versions(name, using).values_list("pk", flat=True))
            Resource.objects.using(using).filter(pk__in=pks).delete()
    finally:
        _discarding.reset(token)


# The receivers' dispatch_uids.
POST_SAVE_UID = "scrud_django.ui.branches.versions.post_save"
POST_DELETE_UID = "scrud_django.ui.branches.versions.post_delete"


def connect_receivers():
    """Connect `item_written` to ``scrud_post_save`` and
    ``scrud_post_delete`` (once: fixed ``dispatch_uid``s)."""
    scrud_post_save.connect(item_written, weak=False, dispatch_uid=POST_SAVE_UID)
    scrud_post_delete.connect(item_written, weak=False, dispatch_uid=POST_DELETE_UID)
