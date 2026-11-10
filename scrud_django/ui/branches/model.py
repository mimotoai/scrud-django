"""A branch, and how an item on it is identified and authorized. Everything
here is computed from a `Branch` and an item's content, with no query: the
virtual `MAIN_BRANCH` serves every item on ``main`` (ruling R1 fixes its
values)."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from scrud_django.ui.branches import ITEM_KINDS, MAIN


@dataclass(frozen=True)
class Branch:
    name: str
    context: str
    parent: str | None
    base: str  # "live" | "pinned"
    branching_point: Mapping[str, int]
    prefix: str | None  # None: a working branch; else a published one
    pk: int | None  # None for the virtual main

    @property
    def published(self) -> bool:
        return self.prefix is not None


# `main` as it is until stored, and as it must be stored (but for its pk).
MAIN_BRANCH = Branch(
    name=MAIN,
    context="",
    parent=None,
    base="live",
    branching_point=MappingProxyType({}),
    prefix="",
    pk=None,
)


def kind_of(type_uri: str) -> str | None:
    """The item kind (an `ITEM_KINDS` key) of `type_uri`, or ``None``."""
    return next((kind for kind, uri in ITEM_KINDS.items() if uri == type_uri), None)


def branch_of(content) -> str:
    """The branch an item's `content` is on: its ``branch``, else `MAIN` (as
    every item stored before branches is)."""
    branch = content.get("branch") if isinstance(content, Mapping) else None
    return branch if isinstance(branch, str) else MAIN


def _context_of(content) -> str:
    context = content.get("rbac_context", "")
    return context if isinstance(context, str) else ""


def applies_to(content, branch: Branch) -> str:
    """The context a stylesheet on `branch` applies to (``""``: everywhere):
    its ``applies_to``, else, on a published branch, its ``rbac_context`` when
    that is not the branch's context (as phase 1 stored an ``acme``
    stylesheet in ``acme``), else ``""``."""
    value = content.get("applies_to")
    if isinstance(value, str):
        return value
    if not branch.published:
        return ""
    context = _context_of(content)
    return context if context != branch.context else ""


def item_key(kind: str, content, branch: Branch) -> tuple:
    """An item's identity on its branch: ``(name,)``, and for a stylesheet
    ``(name, applies_to)``."""
    name = content.get("name")
    if kind == "stylesheet":
        return (name, applies_to(content, branch))
    return (name,)


def authorizing_context(kind: str, content, branch: Branch) -> str:
    """The one context a direct write of the item on `branch` is authorized
    in, and so the ``rbac_context`` it must have: the branch's context, but
    for a stylesheet on a published branch that applies to a context, that
    context."""
    if kind == "stylesheet" and branch.published:
        target = applies_to(content, branch)
        if target:
            return target
    return branch.context
