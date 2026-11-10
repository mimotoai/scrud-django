"""The effective state of a branch: its own items over its parent's current
state, recursively, and the selection of the branch a request renders.

With no selection (or ``main`` selected) the state is main's, computed from
the stored items alone: no branch row is read (ruling R1)."""

import contextlib
import contextvars
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

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
from scrud_django.ui.cascade import _skipped, _stored

logger = logging.getLogger(__name__)

BRANCH_HEADER = "Scrud-UI-Branch"


@dataclass(frozen=True)
class Selection:
    """The branch a render shows. `pinned` is read from Task 5 on."""

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
    kind: str, rows: Iterable, chain: list[Branch], on_duplicate=None
) -> list[tuple]:
    """The ``(pk, etag, content)`` rows of `rows` (lowest pk first) visible on
    the branch whose chain (itself first, then its ancestors) is `chain`: for
    each item key, the row of the nearest branch that has one, but for
    deletion markers. Lowest pk first. A row that loses its key to another
    on the same branch (a race in the uniqueness check) is passed to
    `on_duplicate(loser_row, winner_row, key)`."""
    rows = list(rows)
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
        (row for row in seen.values() if not row[2].get("deleted")),
        key=lambda row: row[0],
    )


def effective_rows(
    type_uri: str, selection: Selection | None = None
) -> list[tuple[int, str, dict]]:
    """``(pk, etag, content)`` of the items of `type_uri` visible on the
    selected branch (default: `active_selection`), lowest pk first, deletion
    markers removed. The rows are `cascade._stored`'s: one query per type per
    response. The selection must be selectable (`selection_for_request`)."""
    selection = selection or active_selection()
    if selection.branch == MAIN:
        chain = [MAIN_BRANCH]
    else:
        chain = chain_of(selection.branch, branches())
    kind = kind_of(type_uri)

    def skipped(loser, winner, key):
        reason = f"{kind} {winner[0]} has its name"
        if kind == "stylesheet":
            reason += f" in context {key[1]!r}"
        _skipped(kind, loser[0], key[0], reason)

    return effective_items(kind, _stored(type_uri), chain, skipped)


def selection_for_request(request) -> Selection:
    """The selection `request` makes: its `BRANCH_HEADER`, unless that names
    a branch that does not exist, whose chain is broken (logged), or that the
    user may not GET: then main."""
    headers = getattr(request, "headers", None)
    name = headers.get(BRANCH_HEADER) if headers is not None else None
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
    return Selection(branch=name)
