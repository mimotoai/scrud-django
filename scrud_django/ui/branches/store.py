"""The stored branches, read with system rights, and their chains."""

from collections.abc import Mapping
from types import MappingProxyType

from scrud_django.models import Resource
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    type_db,
    type_locked,
)
from scrud_django.rows import by_pk, unsorted
from scrud_django.ui.branches import MAIN, UI_BRANCH_SLUG, UI_BRANCH_TYPE_URI
from scrud_django.ui.branches.model import MAIN_BRANCH, Branch
from scrud_django.ui.cascade import _memo

# The key `branches` memoises under in a `cascade.rules_memo`.
_BRANCHES = "branches"


class BranchChainError(Exception):
    """A branch whose chain of parents names a missing branch or loops."""


def branch_from(pk, content) -> Branch:
    """The `Branch` stored as resource `pk` with `content` (a body the
    branch views accepted, or one stored as is)."""

    def text(key, default=None):
        value = content.get(key, default)
        return value if isinstance(value, str) else default

    point = content.get("branching_point")
    point = point if isinstance(point, Mapping) else {}
    return Branch(
        name=content["name"],
        context=text("rbac_context", ""),
        parent=text("parent"),
        base="pinned" if content.get("base") == "pinned" else "live",
        branching_point=MappingProxyType(dict(point)),
        prefix=text("prefix"),
        pk=pk,
    )


def branches(using=None) -> dict[str, Branch]:
    """Every branch by name, lowest pk first where a name is stored twice,
    plus the virtual `MAIN_BRANCH` while ``main`` is not stored. Read on
    `using` (default: the router's read database), once inside a
    `cascade.rules_memo`."""
    memo = _memo.get()
    if memo is not None and _BRANCHES in memo:
        return memo[_BRANCHES]
    found = _read_branches(using)
    if memo is not None:
        memo[_BRANCHES] = found
    return found


def _read_branches(using):
    found = {name: branch for name, (branch, _) in stored_branches(using).items()}
    found.setdefault(MAIN, MAIN_BRANCH)
    return found


def stored_branches(using=None) -> dict[str, tuple[Branch, Mapping]]:
    """Every stored branch by name, with its stored content, lowest pk first
    where a name is stored twice: one query on `using` (default: the
    router's read database), never memoised. The virtual main is not
    stored."""
    rows = Resource.objects.using(using) if using else Resource.objects
    rows = unsorted(rows.filter(resource_type__type_uri=UI_BRANCH_TYPE_URI))
    found: dict[str, tuple[Branch, Mapping]] = {}
    for pk, content in by_pk(rows.values_list("pk", "content")):
        name = content.get("name") if isinstance(content, Mapping) else None
        if isinstance(name, str) and name not in found:
            found[name] = (branch_from(pk, content), content)
    return found


def ensure_main(using) -> Branch:
    """``main``, stored first if it is virtual. Call it inside a transaction
    on `using`, the write database: the branch type's row is locked
    (`type_locked`) until it ends before ``main`` is read again and
    stored, so that concurrent first writes store it once."""
    main = branches(using)[MAIN]
    if main.pk is not None:
        return main
    with type_locked(get_resource_type_for(UI_BRANCH_SLUG, using=type_db()), using):
        main = _read_branches(using)[MAIN]
        if main.pk is not None:
            return main
    stored = ResourceRegistration.register(
        {
            "name": MAIN,
            "rbac_context": MAIN_BRANCH.context,
            "parent": None,
            "base": MAIN_BRANCH.base,
            "branching_point": {},
            "prefix": MAIN_BRANCH.prefix,
        },
        UI_BRANCH_SLUG,
    )
    memo = _memo.get()
    if memo is not None:
        memo.pop(_BRANCHES, None)
    return branch_from(stored.pk, stored.content)


def ancestors(name: str, all_branches: Mapping[str, Branch]) -> list[Branch]:
    """The ancestors of branch `name` in `all_branches`, nearest first.
    Raise `BranchChainError` when a parent is missing or the chain loops
    (back to `name` or to another ancestor)."""
    if name not in all_branches:
        raise BranchChainError(f"There is no branch named {name!r}.")
    seen = {name}
    chain = []
    parent = all_branches[name].parent
    while parent is not None:
        if parent in seen:
            raise BranchChainError(f"The parents of branch {name!r} form a cycle.")
        branch = all_branches.get(parent)
        if branch is None:
            raise BranchChainError(
                f"Branch {name!r} has a missing ancestor, {parent!r}."
            )
        seen.add(parent)
        chain.append(branch)
        parent = branch.parent
    return chain


def published_root(name: str, all_branches: Mapping[str, Branch]) -> Branch:
    """The published branch `name` belongs to: itself if it has a prefix,
    else its nearest ancestor with one. `BranchChainError` when it has
    neither."""
    branch = all_branches.get(name)
    if branch is not None and branch.published:
        return branch
    for ancestor in ancestors(name, all_branches):
        if ancestor.published:
            return ancestor
    raise BranchChainError(f"Branch {name!r} has no published ancestor.")
