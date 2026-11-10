"""The data of the studio page of a type: the branches its user may pick,
the preview controls, the branch strip (the branch's own items, its
conflicts, its CSS items and the app on it), and the item the editor reads.
Everything listed is what the user may GET: a branch they may select
(`selectable`), an item the per-row read check allows (as a JSON list
filters it)."""

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

from django.urls import NoReverseMatch, get_script_prefix, reverse

from scrud_django.models import Resource
from scrud_django.permissions import may_read_in_context, resource_contexts
from scrud_django.rows import by_pk, unsorted
from scrud_django.ui.branches import ITEM_KINDS, MAIN, UI_CSS_TYPE_URI
from scrud_django.ui.branches.effective import Selection, effective_rows
from scrud_django.ui.branches.model import Branch, branch_of, item_key
from scrud_django.ui.branches.selection import (
    BRANCH_PARAMETER,
    Mount,
    api_base,
    branch_content,
    selectable,
    selection_value,
)
from scrud_django.ui.branches.store import BranchChainError, branches, published_root
from scrud_django.ui.types import UI_STYLESHEET_TYPE_URI

MODES = ("view", "edit")


@dataclass(frozen=True)
class BranchChoice:
    """A branch of the picker: `published` ones are read-only."""

    name: str
    published: bool


def studio_branches(request, all_branches=None) -> list[BranchChoice]:
    """Every branch the user of `request` may select (`selectable`: GET on
    it and its working ancestors, an intact chain), wherever its published
    root is: ``main`` first, then by name."""
    all_branches = branches() if all_branches is None else all_branches
    found = [
        BranchChoice(name, branch.published)
        for name, branch in all_branches.items()
        if selectable(request, Selection(branch=name), all_branches=all_branches)
    ]
    return sorted(found, key=lambda choice: (choice.name != MAIN, choice.name))


def may_preview_context(request, context: str) -> bool:
    """Whether the user may preview in the data context `context`: ``""``,
    or one where they may GET UI stylesheets (ruling S7)."""
    return not context or may_read_in_context(
        request, UI_STYLESHEET_TYPE_URI, {"rbac_context": context}, [context]
    )


def page_context(request, slug: str) -> dict:
    """The template context of the studio page of type `slug`, from the
    page's query: ``branch`` (default: the first working branch the user may
    select, else ``main``), ``pinned`` (``1``: at the branching point),
    ``mode``, ``scope``, ``context`` and ``instance``, as the preview takes
    them. A branch the user may not select, a pinned snapshot that is
    missing and a data context they may not preview are dropped with a
    notice, the same for every refused value."""
    params = request.GET
    all_branches = branches()
    choices = studio_branches(request, all_branches)
    names = [choice.name for choice in choices]
    notices = []
    name = params.get("branch") or next(
        (choice.name for choice in choices if not choice.published), MAIN
    )
    if name not in names:
        notices.append(f"Branch {name!r} is not available; showing {MAIN!r}.")
        name = MAIN
    branch = all_branches[name]
    pinned = params.get("pinned") == "1" and branch.parent is not None
    selection = Selection(branch=name, pinned=pinned)
    if pinned and not selectable(request, selection, all_branches=all_branches):
        notices.append(f"Branch {name!r} cannot be shown pinned; showing it live.")
        selection = Selection(branch=name)
    mode = params.get("mode")
    mode = mode if mode in MODES else "view"
    scope = " ".join(params.get("scope", "").split())
    context = params.get("context", "").strip()
    if not may_preview_context(request, context):
        notices.append(f"Data context {context!r} is not available.")
        context = ""
    instance = params.get("instance", "").strip()
    controls = {
        "branch": selection_value(selection),
        "mode": mode,
        "scope": scope,
        "context": context,
        "instance": instance,
    }
    query = urlencode([(key, value) for key, value in controls.items() if value])
    return {
        "branches": choices,
        "branch": branch,
        "pinned": selection.pinned,
        "controls": controls,
        "query": query,
        "editable": not branch.published,
        "notices": notices,
        **strip_context(request, slug, branch, selection, all_branches),
    }


def strip_context(request, slug, branch: Branch, selection, all_branches) -> dict:
    """The template context of the branch strip of `branch` (shown as
    `selection`) on the page of type `slug`."""
    try:
        root = published_root(branch.name, all_branches)
    except BranchChainError:  # defensive: `selectable` refused a broken chain
        root = None
    return {
        "branch": branch,
        "root": root,
        "editable": not branch.published,
        "changes": own_items(request, branch),
        "conflicts": branch_conflicts(branch),
        "css_items": css_items(request, branch),
        "browse_url": browse_url(slug, root, selection),
    }


def _may_read(request, kind, content) -> bool:
    return may_read_in_context(
        request, ITEM_KINDS[kind], content, resource_contexts(content)
    )


def own_items(request, branch: Branch) -> list[dict]:
    """``{kind, name, applies_to, deleted}`` of each row of `branch`'s own
    (its changes: overrides, additions, deletion markers) that the user may
    read, by kind then lowest pk. ``main`` has no parent to change from:
    none."""
    if branch.name == MAIN:
        return []
    found = []
    for kind, type_uri in ITEM_KINDS.items():
        rows = unsorted(
            Resource.objects.filter(
                resource_type__type_uri=type_uri, content__branch=branch.name
            ).values_list("pk", "content")
        )
        for _pk, content in by_pk(rows):
            if not isinstance(content, Mapping) or branch_of(content) != branch.name:
                continue
            if not _may_read(request, kind, content):
                continue
            found.append(_described(kind, content, branch))
    return found


def _described(kind, content, branch) -> dict:
    key = item_key(kind, content, branch)
    return {
        "kind": kind,
        "name": key[0],
        "applies_to": key[1] if kind == "stylesheet" else "",
        "deleted": content.get("deleted") is True,
    }


def branch_conflicts(branch: Branch) -> list[dict]:
    """``{kind, name, applies_to}`` of each conflict listed on `branch` (the
    user may GET it: they selected it)."""
    content = branch_content(branch)
    listed = content.get("conflicts") if isinstance(content, Mapping) else None
    found = []
    for entry in listed if isinstance(listed, list) else ():
        if not isinstance(entry, Mapping) or entry.get("kind") not in ITEM_KINDS:
            continue
        key = entry.get("key")
        if not isinstance(key, list) or not key or not isinstance(key[0], str):
            continue
        applies = key[1] if len(key) > 1 and isinstance(key[1], str) else ""
        found.append({"kind": entry["kind"], "name": key[0], "applies_to": applies})
    return found


def css_items(request, branch: Branch) -> list[dict]:
    """``{name, own}`` of the CSS items effective on `branch` (live) that
    the user may read, in the order the page links them."""
    found = []
    for _pk, _etag, content in effective_rows(
        UI_CSS_TYPE_URI, Selection(branch=branch.name)
    ):
        name = content.get("name")
        if not isinstance(name, str) or not _may_read(request, "css", content):
            continue
        order = content.get("order")
        found.append(
            (
                order if isinstance(order, int) else 0,
                name,
                branch_of(content) == branch.name,
            )
        )
    found.sort(key=lambda item: item[:2])
    return [{"name": name, "own": own} for _order, name, own in found]


def browse_url(slug: str, root: Branch | None, selection: Selection) -> str | None:
    """The type's list in the app on `selection`'s branch: the API's URL
    under the prefix of the branch's published `root`, with
    ``?ui-branch=<selection>``, so that the switch is confirmed."""
    try:
        canonical = reverse(f"{slug}-list")
    except NoReverseMatch:
        return None
    if root is not None and root.name != MAIN and root.prefix:
        mount = Mount(
            prefix=root.prefix,
            branch=root.name,
            api_base=api_base(),
            script=get_script_prefix(),
        )
        canonical = mount.href(canonical)
    return f"{canonical}?{urlencode({BRANCH_PARAMETER: selection_value(selection)})}"


def read_item(request, branch: Branch, kind: str, name: str, target: str) -> dict:
    """The item of `kind` named `name` (a stylesheet: that applies to
    `target`) as `branch` shows it live, deletion markers included:
    ``{kind, name, applies_to, own, from, etag, content}`` (``etag`` quoted,
    as a header carries it; ``own``: it is `branch`'s own row, so a save is
    a PUT with it as ``If-Match``). ``None`` when the branch shows none, or
    one the user may not read: one answer for both."""
    key = (name, target) if kind == "stylesheet" else (name,)
    rows = effective_rows(
        ITEM_KINDS[kind], Selection(branch=branch.name), include_deleted=True
    )
    for _pk, etag, content in rows:
        if item_key(kind, content, branch) != key:
            continue
        if not _may_read(request, kind, content):
            return None
        return {
            "kind": kind,
            "name": name,
            "applies_to": target,
            "own": branch_of(content) == branch.name,
            "from": branch_of(content),
            "etag": f'"{etag}"' if etag else None,
            "content": content,
        }
    return None
