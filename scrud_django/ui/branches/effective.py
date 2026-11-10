"""The effective state of a branch: its own items over its parent's current
state, recursively, and the selection of the branch a request renders.

With no selection (or ``main`` selected) the state is main's, computed from
the stored items alone: no branch row is read (ruling R1)."""

import contextlib
import contextvars
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from scrud_django.exceptions import UIBranchVersionMissingError
from scrud_django.permissions import may_read_in_context
from scrud_django.ui.branches import MAIN, UI_BRANCH_TYPE_URI
from scrud_django.ui.branches.model import (
    MAIN_BRANCH,
    Branch,
    branch_of,
    item_key,
    kind_of,
)
from scrud_django.ui.branches.store import (
    BranchChainError,
    ancestors,
    branches,
    published_root,
)
from scrud_django.ui.branches.versions import snapshot
from scrud_django.ui.cascade import _memo, _skipped, _stored

logger = logging.getLogger(__name__)

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
    response. The selection must be selectable (`selection_for_request`)."""
    selection = selection or active_selection()
    if selection.branch == MAIN:
        chain = [MAIN_BRANCH]
    else:
        chain = chain_of(selection.branch, branches())
    kind = kind_of(type_uri)
    rows = _rows_for(kind, type_uri, chain, selection.pinned)
    if rows is None:  # a pinned ancestor's snapshot is missing: main
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


def _rows_for(kind, type_uri, chain, view_pinned):
    """The rows `effective_items` takes for `chain`: every stored row when
    nothing is pinned. Else the rows of the branches down to the first pinned
    one (the selected branch when `view_pinned`) are live, and its ancestors'
    come from the snapshots at its branching point (one query each, memoised
    for the response). ``None`` when a snapshot is missing."""
    pinned_at = next(
        (
            i
            for i, branch in enumerate(chain)
            if branch.parent is not None
            and (branch.base == "pinned" or (view_pinned and i == 0))
        ),
        None,
    )
    if pinned_at is None:
        return _stored(type_uri)
    live = {branch.name for branch in chain[: pinned_at + 1]}
    rows = [row for row in _stored(type_uri) if branch_of(row[2]) in live]
    pinned = chain[pinned_at]
    for ancestor in chain[pinned_at + 1 :]:
        number = pinned.branching_point.get(ancestor.name)
        items = _snapshot_items(ancestor.name, number)
        if items is None:
            logger.warning(
                "scrud-ui: not rendering branch %r: the version %r of %r "
                "it is pinned to is missing",
                chain[0].name,
                number,
                ancestor.name,
            )
            return None
        for content in items.get(kind) or []:
            if not isinstance(content, Mapping):
                continue
            key = item_key(kind, content, ancestor) if content.get("name") else ()
            etag = f"v{number}:{ancestor.name}:{':'.join(map(str, key))}"
            rows.append((None, etag, content))
    return rows


def _snapshot_items(name, number):
    """``{kind: [content, ...]}`` of version `number` of branch `name`, or
    ``None`` when it is not stored. Read once per `rules_memo`."""
    memo = _memo.get()
    key = ("snapshot", name, number)
    if memo is not None and key in memo:
        return memo[key]
    try:
        items = snapshot(name, number)["items"] if isinstance(number, int) else None
    except UIBranchVersionMissingError:
        items = None
    if not isinstance(items, Mapping):
        items = None
    if memo is not None:
        memo[key] = items
    return items


def selection_for_request(request) -> Selection:
    """The selection `request` makes: its `BRANCH_HEADER`, else its
    `BRANCH_PARAMETER` query parameter, unless that names
    a branch that does not exist, whose chain is broken (logged), or that the
    user may not GET: then main."""
    headers = getattr(request, "headers", None)
    name = headers.get(BRANCH_HEADER) if headers is not None else None
    if not name:
        params = getattr(request, "GET", None)
        name = params.get(BRANCH_PARAMETER) if params is not None else None
    pinned = bool(name) and name.endswith(PINNED_SUFFIX)
    if pinned:
        name = name[: -len(PINNED_SUFFIX)]
    if not name or name == MAIN:
        return Selection()
    all_branches = branches()
    branch = all_branches.get(name)
    if branch is None:
        return Selection()
    try:
        ancestors(name, all_branches)
        published_root(name, all_branches)
    except BranchChainError as error:
        logger.warning("scrud-ui: not rendering branch %r: %s", name, error)
        return Selection()
    content = {
        "name": branch.name,
        "rbac_context": branch.context,
        "parent": branch.parent,
        "prefix": branch.prefix,
    }
    if not may_read_in_context(request, UI_BRANCH_TYPE_URI, content, [branch.context]):
        return Selection()
    return Selection(branch=name, pinned=pinned)
