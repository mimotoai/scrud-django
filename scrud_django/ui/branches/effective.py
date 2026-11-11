"""The effective state of a branch: its own items over its parent's current
state, recursively, and the selection of the branch a render shows (chosen
for a request by `scrud_django.ui.branches.selection.choose`).

With no selection (or ``main`` selected) the state is main's, computed from
the stored items alone: no branch row is read (ruling R1).

The packaged layers' items (`scrud_django.ui.layers`) are the bottom of every
chain, beneath every root: `effective_rows`, `branch_state` and
`parent_state` end each chain with `LAYER_BRANCH`, and `_rows_for` adds their
rows (a pinned branch sees the installed layers too)."""

import contextlib
import contextvars
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

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
from scrud_django.ui.layers import LAYER_BRANCH, layer_rows

BRANCH_HEADER = "Scrud-UI-Branch"
BRANCH_PARAMETER = "ui-branch"
PINNED_SUFFIX = "@pinned"
SAFE_PARAMETER = "ui-safe"
# Sent by scrud-ui.js on the requests of a safe page (``data-scrud-safe``):
# the page's own fragment requests target the path without the query.
SAFE_HEADER = "Scrud-UI-Safe"


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


_safe_mode: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "scrud_ui_safe_mode", default=False
)


@contextlib.contextmanager
def using_safe_mode(on: bool = True):
    """Render from the packaged layers alone inside the block (`on`): see
    `effective_rows`. A context variable, so it holds for any work the render
    does in-process (an embed's, a thread that copies the context)."""
    token = _safe_mode.set(on)
    try:
        yield
    finally:
        _safe_mode.reset(token)


def safe_mode() -> bool:
    """Whether the render in progress is in safe mode (`using_safe_mode`)."""
    return _safe_mode.get()


@dataclass(frozen=True)
class Draft:
    """An unsaved version of an item of `kind` (an ``ITEM_KINDS`` key):
    `content`, with its ``name`` (and a stylesheet's ``applies_to``)."""

    kind: str
    content: dict


# The ETag prefix of a draft's row (`using_drafts`).
DRAFT_ETAG_PREFIX = "draft:"


@dataclass(frozen=True)
class _Drafts:
    """The drafts of branch `branch` a render shows: ``{kind: [row]}``, and
    each draft row's position in the cascade (by ETag), as `effective_rows`
    finds it."""

    branch: str
    rows: dict
    positions: dict = field(default_factory=dict)


_drafts: contextvars.ContextVar[_Drafts | None] = contextvars.ContextVar(
    "scrud_ui_drafts", default=None
)


@contextlib.contextmanager
def using_drafts(branch: str, drafts: Iterable[Draft]):
    """Inside the block, `effective_rows` shows each of `drafts` as branch
    `branch`'s own row of its item: ``(None, "draft:<sha256>", content |
    {"branch": branch})``, in place of the branch's stored row of that item,
    if any, for every chain `branch` is in. Nothing is stored. A context
    variable, read when the rows are read (not memoised with them), so an
    embed's render, which opens a rules memo of its own, shows the drafts too.
    The caller has checked them (`studio.drafts.validate_draft`): at most one
    per item, each with a string name. In safe mode they are ignored, as
    every stored item is."""
    rows: dict[str, list] = {}
    for draft in drafts:
        content = {**draft.content, "branch": branch}
        digest = hashlib.sha256(
            json.dumps([draft.kind, content], sort_keys=True, default=str).encode(
                "utf-8"
            )
        ).hexdigest()
        rows.setdefault(draft.kind, []).append(
            (None, DRAFT_ETAG_PREFIX + digest, content)
        )
    token = _drafts.set(_Drafts(branch, rows))
    try:
        yield
    finally:
        _drafts.reset(token)


@contextlib.contextmanager
def stored_state():
    """Inside the block, data reads see the stored items: neither the safe
    mode (`using_safe_mode`) nor the drafts (`using_drafts`) of a render in
    progress, which are render-only state. An embedded request runs inside
    its embedder's context, so `dispatch.dispatch_path` runs the embedded
    view in this block (and a view's data read, a list of UI items or a
    computed type of the studio, opens it too, as defense in depth);
    rendering (the cascade's components, stylesheets, CSS and JS) does not,
    and keeps both."""
    safe_token = _safe_mode.set(False)
    drafts_token = _drafts.set(None)
    try:
        yield
    finally:
        _drafts.reset(drafts_token)
        _safe_mode.reset(safe_token)


def draft_position(etag) -> int | None:
    """The cascade position of the draft row whose ETag is `etag`, where its
    save would put it: the pk of the branch's own row it replaces (a save
    updates that row), else after every stored row of its kind on the chain
    (a save creates one), in the drafts' order; ``None`` for a row that is
    no draft of the render in progress."""
    state = _drafts.get()
    if state is None or not isinstance(etag, str):
        return None
    return state.positions.get(etag)


def _with_drafts(kind, rows, chain):
    """`rows` (of the items of `kind` on `chain`) with the drafts of the
    render in progress (`using_drafts`) as their branch's own rows, when that
    branch is in `chain`: each in place of the branch's own row of its item
    key; the rest as they are. Records each draft's `draft_position`."""
    state = _drafts.get()
    drafted = state.rows.get(kind) if state is not None else None
    branch = (
        next((b for b in chain if b.name == state.branch), None) if drafted else None
    )
    if branch is None:
        return rows
    keys = {item_key(kind, content, branch) for _pk, _etag, content in drafted}
    replaced: dict[tuple, int | None] = {}
    kept = []
    for row in rows:
        content = row[2]
        if (
            isinstance(content, Mapping)
            and isinstance(content.get("name"), str)
            and branch_of(content) == branch.name
            and item_key(kind, content, branch) in keys
        ):
            replaced.setdefault(item_key(kind, content, branch), row[0])
            continue
        kept.append(row)
    newest = max((row[0] for row in rows if row[0] is not None), default=0)
    for index, (_pk, etag, content) in enumerate(drafted):
        pk = replaced.get(item_key(kind, content, branch))
        state.positions[etag] = pk if pk is not None else newest + 1 + index
    return [*kept, *drafted]


def query_param(request, name: str) -> str | None:
    """The query parameter `name` of `request` (a DRF or a Django one)."""
    params = getattr(request, "query_params", None)
    if params is None:
        params = getattr(request, "GET", {})
    return params.get(name)


def safe_mode_requested(request) -> bool:
    """Whether `request` asks for safe mode: ``?ui-safe=1``, or the header
    ``Scrud-UI-Safe: 1`` (`SAFE_HEADER`), which the requests of a safe page
    send. Any other value is ignored."""
    if query_param(request, SAFE_PARAMETER) == "1":
        return True
    headers = getattr(request, "headers", None)
    return headers is not None and headers.get(SAFE_HEADER) == "1"


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
    presentation. A JSON list shows the rows as stored (``False``).

    The packaged layers' items (pk ``None``) are beneath every branch's: an
    item key no branch of the chain has shows the layer's item. In safe mode
    (`using_safe_mode`) they are all there is: the chain is ``[LAYER_BRANCH]``
    and `selection` is not looked at.

    Inside `using_drafts`, when its branch is in the chain, each draft is
    that branch's own row of its item, in place of the stored one, sorted
    where its save would put it (`draft_position`)."""
    kind = kind_of(type_uri)
    if safe_mode():
        # Stored items are ignored, on every branch: no query is made.
        chain, rows = [], layer_rows(type_uri)
    else:
        selection = selection or active_selection()
        if selection.branch == MAIN:
            chain = [MAIN_BRANCH]
        else:
            chain = chain_of(selection.branch, branches())
        rows = _rows_for(kind, type_uri, chain, selection.pinned)
        if rows is None:  # defensive: `selection.choose` already refused it
            chain = [MAIN_BRANCH]
            rows = [*_stored(type_uri, {MAIN}), *layer_rows(type_uri)]
    chain = [*chain, LAYER_BRANCH]
    if authorized_only:
        rows = _authorized(kind, rows, chain)
    rows = _with_drafts(kind, rows, chain)

    def skipped(loser, winner, key):
        reason = f"{kind} {winner[0]} has its name"
        if kind == "stylesheet":
            reason += f" in context {key[1]!r}"
        _skipped(kind, loser[0], key[0], reason)

    def unnamed(row):
        _skipped(kind, row[0], None, "it has no name")

    found = effective_items(kind, rows, chain, skipped, unnamed, include_deleted)
    if _drafts.get() is None:
        return found
    # A draft sorts where its save would put it (`draft_position`).
    return sorted(
        found,
        key=lambda row: (
            row[0] is not None or draft_position(row[1]) is not None,
            row[0] if row[0] is not None else draft_position(row[1]) or 0,
        ),
    )


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
    database (`cascade._stored`). The packaged layers' rows
    (`layers.layer_rows`, never pinned: versions do not record them) come
    last; only a chain that ends with `LAYER_BRANCH` shows them."""
    pinned_at = _pinned_at(chain, view_pinned)
    live = chain if pinned_at is None else chain[: pinned_at + 1]
    rows = list(_live_rows(type_uri, {branch.name for branch in live}, using))
    if pinned_at is not None:
        pinned = chain[pinned_at]
        for ancestor in chain[pinned_at + 1 :]:
            number = pinned.branching_point.get(ancestor.name)
            snapped = _snapshot_rows(kind, ancestor, number, using)
            if snapped is None:
                return None
            rows.extend(snapped)
    rows.extend(layer_rows(type_uri))
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
    `_rows_for`, and the packaged layers' items beneath them), read on
    `using`: what a push into it overwrites, and what a pull of a child is
    compared with (ruling R38). Deletion markers are kept with
    `include_deleted`. `BranchChainError` for a broken chain, and for a
    pinned snapshot that is missing."""
    chain = chain_of(name, all_branches)
    rows = _rows_for(kind, type_uri, chain, False, using)
    if rows is None:
        raise BranchChainError(
            f"Branch {name!r} is pinned to an ancestor version that is "
            "missing: pull it first."
        )
    return effective_items(
        kind, rows, [*chain, LAYER_BRANCH], include_deleted=include_deleted
    )


def parent_state(
    kind: str,
    type_uri: str,
    branch: Branch,
    all_branches,
    using,
    include_deleted: bool = False,
):
    """``(pk, etag, content)`` of the items of `type_uri` effective on
    `branch`'s parent as `branch` shows them (`all_branches` say where its
    chain is), read on `using`: the parent's current state on a live
    branch; on one pinned (or with a pinned ancestor), the ancestors' items
    as of the branching point, with the ETags their versions recorded; and
    beneath them the packaged layers' items (alone, for a root). This is
    what an override on `branch` replaces, so its ``base`` (ruling R38).
    Deletion markers are kept with `include_deleted`.
    `BranchChainError` for a broken chain, and for a pinned snapshot that
    is missing."""
    chain = chain_of(branch.name, all_branches)
    rows = _rows_for(kind, type_uri, chain, False, using)
    if rows is None:
        raise BranchChainError(
            f"Branch {branch.name!r} is pinned to an ancestor version that is "
            "missing: pull it first."
        )
    return effective_items(
        kind, rows, [*chain[1:], LAYER_BRANCH], include_deleted=include_deleted
    )
