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
from scrud_django.ui.branches import MAIN
from scrud_django.ui.branches.model import (
    MAIN_BRANCH,
    Branch,
    authorizing_context,
    branch_of,
    item_key,
    kind_of,
)
from scrud_django.ui.branches.store import BranchChainError, ancestors, branches
from scrud_django.ui.branches.versions import snapshot
from scrud_django.ui.cascade import _memo, _skipped, _stored, item_rows

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
    authorized_only: bool = True,
) -> list[tuple[int, str, dict]]:
    """``(pk, etag, content)`` of the items of `type_uri` visible on the
    selected branch (default: `active_selection`), lowest pk first, deletion
    markers removed (kept with `include_deleted`). The rows are
    `cascade._stored`'s: one query per response, of the selected chain's
    branches only. The selection must be selectable (`selection.choose`).

    With `authorized_only` (rendering), a row whose ``rbac_context`` is not
    its `authorizing_context` on its branch is skipped, as if absent, and
    logged (ruling R41, minor 3): no write through the API stores one, so
    it was stored around the checks that decide who controls a branch's
    presentation. A JSON list shows the rows as stored (``False``)."""
    selection = selection or active_selection()
    if selection.branch == MAIN:
        chain = [MAIN_BRANCH]
    else:
        chain = chain_of(selection.branch, branches())
    kind = kind_of(type_uri)
    rows = _rows_for(kind, type_uri, chain, selection.pinned)
    if rows is None:  # defensive: `selection.choose` already refused it
        chain = [MAIN_BRANCH]
        rows = _stored(type_uri, {MAIN})
    if authorized_only:
        rows = _authorized(kind, rows, chain)

    def skipped(loser, winner, key):
        reason = f"{kind} {winner[0]} has its name"
        if kind == "stylesheet":
            reason += f" in context {key[1]!r}"
        _skipped(kind, loser[0], key[0], reason)

    def unnamed(row):
        _skipped(kind, row[0], None, "it has no name")

    return effective_items(kind, rows, chain, skipped, unnamed, include_deleted)


def _authorized(kind, rows, chain):
    """`rows` but those on a branch of `chain` whose ``rbac_context`` (``""``
    when absent) is not their `authorizing_context` there (logged)."""
    by_name = {branch.name: branch for branch in chain}
    kept = []
    for row in rows:
        content = row[2]
        branch = by_name.get(branch_of(content))
        if branch is None or not isinstance(content, Mapping):
            kept.append(row)  # not effective here, or skipped as unnamed
            continue
        context = content.get("rbac_context", "")
        expected = authorizing_context(kind, content, branch)
        if context == expected:
            kept.append(row)
            continue
        name = content.get("name")
        _skipped(
            kind,
            row[0],
            name if isinstance(name, str) else None,
            f"its rbac_context {context!r} is not {expected!r}, the context its "
            f"write is authorized in on branch {branch.name!r}",
        )
    return kept


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
    """The rows `effective_items` takes for `chain`: the stored rows of the
    chain's branches (no other's: ruling R39) when nothing is pinned. Else
    the rows of the branches down to the first pinned one (the selected
    branch when `view_pinned`) are live, and its ancestors' come from the
    snapshots at its branching point (one query each, memoised for the
    response). ``None`` when a snapshot is missing (not logged). Read on
    `using`, without memoising the live rows; default: the router's read
    database (`cascade._stored`)."""
    pinned_at = _pinned_at(chain, view_pinned)
    live = chain if pinned_at is None else chain[: pinned_at + 1]
    rows = list(_live_rows(type_uri, {branch.name for branch in live}, using))
    if pinned_at is None:
        return rows
    pinned = chain[pinned_at]
    for ancestor in chain[pinned_at + 1 :]:
        number = pinned.branching_point.get(ancestor.name)
        snapped = _snapshot_rows(kind, ancestor, number, using)
        if snapped is None:
            return None
        rows.extend(snapped)
    return rows


def _live_rows(type_uri, names, using):
    """The stored rows of `type_uri` on the branches `names`: filtered in
    SQL (`cascade.on_branches`, ruling R39), and in Python as `branch_of`
    reads them."""
    if using is None:
        rows = _stored(type_uri, names)
    else:
        rows = item_rows([type_uri], names, using)[type_uri]
    return [row for row in rows if branch_of(row[2]) in names]


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


def branch_state(
    kind: str, type_uri: str, name: str, all_branches, using, include_deleted=False
):
    """``(pk, etag, content)`` of the items of `type_uri` effective on
    branch `name` as it shows itself (with its own pins and its ancestors',
    `_rows_for`), read on `using`: what a push into it overwrites, and what
    a pull of a child is compared with (ruling R38). Deletion markers are
    kept with `include_deleted`. `BranchChainError` for a broken chain, and
    for a pinned snapshot that is missing."""
    chain = chain_of(name, all_branches)
    rows = _rows_for(kind, type_uri, chain, False, using)
    if rows is None:
        raise BranchChainError(
            f"Branch {name!r} is pinned to an ancestor version that is "
            "missing: pull it first."
        )
    return effective_items(kind, rows, chain, include_deleted=include_deleted)


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
