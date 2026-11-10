"""The effective state of a branch: its own items over its parent's current
state, recursively, and the selection of the branch a render shows (chosen
for a request by `scrud_django.ui.branches.selection.choose`).

With no selection (or ``main`` selected) the state is main's, computed from
the stored items alone: no branch row is read (ruling R1)."""

import contextlib
import contextvars
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from scrud_django.exceptions import UIBranchVersionMissingError
from scrud_django.models import Resource
from scrud_django.ui.branches import MAIN
from scrud_django.ui.branches.model import (
    MAIN_BRANCH,
    Branch,
    branch_of,
    item_key,
    kind_of,
)
from scrud_django.ui.branches.store import BranchChainError, ancestors, branches
from scrud_django.ui.branches.versions import snapshot
from scrud_django.ui.cascade import _memo, _skipped, _stored

BRANCH_HEADER = "Scrud-UI-Branch"
BRANCH_PARAMETER = "ui-branch"
PINNED_SUFFIX = "@pinned"


@dataclass(frozen=True)
class Selection:
    """The branch a render shows, `pinned` to its branching point."""

    branch: str = MAIN
    pinned: bool = False


_selection: contextvars.ContextVar[Selection | None] = contextvars.ContextVar(
    "scrud_ui_branch_selection", default=None
)


@contextlib.contextmanager
def using_selection(selection: Selection):
    """Render `selection`'s branch inside the block."""
    token = _selection.set(selection)
    try:
        yield
    finally:
        _selection.reset(token)


def active_selection() -> Selection:
    return _selection.get() or Selection()


def chain_of(name: str, all_branches: Mapping[str, Branch]) -> list[Branch]:
    """Branch `name` then its ancestors, nearest first; `BranchChainError`
    when its chain is broken."""
    chain = ancestors(name, all_branches)  # raises for an unknown name
    return [all_branches[name], *chain]


def effective_items(
    kind: str,
    rows: Iterable,
    chain: list[Branch],
    on_duplicate=None,
    on_unnamed=None,
    include_deleted: bool = False,
) -> list[tuple]:
    """The ``(pk, etag, content)`` rows of `rows` (lowest pk first) visible on
    the branch whose chain (itself first, then its ancestors) is `chain`: for
    each item key, the row of the nearest branch that has one, but for
    deletion markers (kept with `include_deleted`). Lowest pk first, rows
    without a pk (a snapshot's) before them. A row that loses its key to another
    on the same branch (a race in the uniqueness check) is passed to
    `on_duplicate(loser_row, winner_row, key)`. A row without a string name
    is left out (and passed to `on_unnamed(row)`)."""
    rows = list(rows)
    named = []
    for row in rows:
        name = row[2].get("name") if isinstance(row[2], Mapping) else None
        if isinstance(name, str):
            named.append(row)
        elif on_unnamed is not None:
            on_unnamed(row)
    rows = named
    seen: dict[tuple, tuple] = {}
    for branch in chain:
        for row in rows:
            if branch_of(row[2]) == branch.name:
                key = item_key(kind, row[2], branch)
                winner = seen.setdefault(key, row)
                if winner is not row and on_duplicate is not None:
                    if branch_of(winner[2]) == branch.name:
                        on_duplicate(row, winner, key)
    return sorted(
        (row for row in seen.values() if include_deleted or not row[2].get("deleted")),
        key=lambda row: (row[0] is not None, row[0] or 0),
    )


def effective_rows(
    type_uri: str,
    selection: Selection | None = None,
    include_deleted: bool = False,
) -> list[tuple[int, str, dict]]:
    """``(pk, etag, content)`` of the items of `type_uri` visible on the
    selected branch (default: `active_selection`), lowest pk first, deletion
    markers removed (kept with `include_deleted`). The rows are `cascade._stored`'s: one query per type per
    response. The selection must be selectable (`selection.choose`)."""
    selection = selection or active_selection()
    if selection.branch == MAIN:
        chain = [MAIN_BRANCH]
    else:
        chain = chain_of(selection.branch, branches())
    kind = kind_of(type_uri)
    rows = _rows_for(kind, type_uri, chain, selection.pinned)
    if rows is None:  # defensive: `selection.choose` already refused it
        chain = [MAIN_BRANCH]
        rows = _stored(type_uri)

    def skipped(loser, winner, key):
        reason = f"{kind} {winner[0]} has its name"
        if kind == "stylesheet":
            reason += f" in context {key[1]!r}"
        _skipped(kind, loser[0], key[0], reason)

    def unnamed(row):
        _skipped(kind, row[0], None, "it has no name")

    return effective_items(kind, rows, chain, skipped, unnamed, include_deleted)


def _pinned_at(chain, view_pinned):
    """The index in `chain` of the first branch pinned to its branching point
    (the selected branch when `view_pinned`), or ``None``."""
    return next(
        (
            i
            for i, branch in enumerate(chain)
            if branch.parent is not None
            and (branch.base == "pinned" or (view_pinned and i == 0))
        ),
        None,
    )


def _missing_snapshot(chain, view_pinned):
    """``(ancestor name, number)`` of the first pinned snapshot `chain` needs
    that is not stored, or ``None``."""
    pinned_at = _pinned_at(chain, view_pinned)
    if pinned_at is None:
        return None
    for ancestor in chain[pinned_at + 1 :]:
        number = chain[pinned_at].branching_point.get(ancestor.name)
        if _snapshot(ancestor.name, number) is None:
            return ancestor.name, number
    return None


def _rows_for(kind, type_uri, chain, view_pinned, using=None):
    """The rows `effective_items` takes for `chain`: every stored row when
    nothing is pinned. Else the rows of the branches down to the first pinned
    one (the selected branch when `view_pinned`) are live, and its ancestors'
    come from the snapshots at its branching point (one query each, memoised
    for the response). ``None`` when a snapshot is missing (not logged).
    Read on `using`, without memoising the live rows; default: the router's
    read database (`cascade._stored`)."""
    pinned_at = _pinned_at(chain, view_pinned)
    if pinned_at is None:
        return _live_rows(type_uri, using)
    live = {branch.name for branch in chain[: pinned_at + 1]}
    rows = [row for row in _live_rows(type_uri, using) if branch_of(row[2]) in live]
    pinned = chain[pinned_at]
    for ancestor in chain[pinned_at + 1 :]:
        number = pinned.branching_point.get(ancestor.name)
        snapped = _snapshot_rows(kind, ancestor, number, using)
        if snapped is None:
            return None
        rows.extend(snapped)
    return rows


def _live_rows(type_uri, using):
    if using is None:
        return _stored(type_uri)
    return (
        Resource.objects.using(using)
        .filter(resource_type__type_uri=type_uri)
        .order_by("pk")
        .values_list("pk", "etag", "content")
    )


def _snapshot_rows(kind, ancestor: Branch, number, using=None):
    """``(None, etag, content)`` of the items of `kind` in version `number`
    of `ancestor`, or ``None`` when it is not stored. The ETag is the one the
    version recorded for the item (ruling R38); a version recorded before
    ETags were has none, and the item gets one of its own,
    ``v<number>:<branch>:<key>``, which no live item has: an override based
    on it conflicts with the live item until it is rebased."""
    found = _snapshot(ancestor.name, number, using)
    if found is None:
        return None
    etags = found.get("etags")
    tags = etags.get(kind) if isinstance(etags, Mapping) else None
    tags = tags if isinstance(tags, list) else []
    rows = []
    for index, content in enumerate(found["items"].get(kind) or []):
        if not isinstance(content, Mapping):
            continue
        etag = tags[index] if index < len(tags) else None
        if not isinstance(etag, str):
            key = item_key(kind, content, ancestor) if content.get("name") else ()
            etag = f"v{number}:{ancestor.name}:{':'.join(map(str, key))}"
        rows.append((None, etag, content))
    return rows


def _snapshot(name, number, using=None):
    """The content of version `number` of branch `name` (its ``items`` a
    mapping), read on `using`, or ``None`` when it is not stored. Read once
    per `rules_memo`."""
    memo = _memo.get()
    key = ("snapshot", name, number, using)
    if memo is not None and key in memo:
        return memo[key]
    try:
        found = snapshot(name, number, using) if isinstance(number, int) else None
    except UIBranchVersionMissingError:
        found = None
    if not isinstance(found, Mapping) or not isinstance(found.get("items"), Mapping):
        found = None
    if memo is not None:
        memo[key] = found
    return found


def parent_state(kind: str, type_uri: str, branch: Branch, all_branches, using):
    """``(pk, etag, content)`` of the items of `type_uri` effective on
    `branch`'s parent as `branch` shows them (`all_branches` say where its
    chain is), read on `using`: the parent's current state on a live
    branch; on one pinned (or with a pinned ancestor), the ancestors' items
    as of the branching point, with the ETags their versions recorded. This
    is what an override on `branch` replaces, so its ``base`` (ruling R38).
    `BranchChainError` for a broken chain, and for a pinned snapshot that
    is missing."""
    chain = chain_of(branch.name, all_branches)
    rows = _rows_for(kind, type_uri, chain, False, using)
    if rows is None:
        raise BranchChainError(
            f"Branch {branch.name!r} is pinned to an ancestor version that is "
            "missing: pull it first."
        )
    return effective_items(kind, rows, chain[1:])
