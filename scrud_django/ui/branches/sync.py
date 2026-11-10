"""Pulling a branch: moving its branching point to its ancestors' current
versions, and the conflicts that leaves between its overrides and the parent's
items, which are listed on the branch until each is resolved."""

from collections.abc import Mapping

from scrud_django.models import Resource
from scrud_django.registration import ResourceRegistration
from scrud_django.ui.branches import ITEM_KINDS, UI_BRANCH_TYPE_URI
from scrud_django.ui.branches.effective import chain_of, effective_items
from scrud_django.ui.branches.model import branch_of, item_key
from scrud_django.ui.branches.store import ancestors, branches
from scrud_django.ui.branches.versions import current_version


def _rows(type_uri: str, using):
    return (
        Resource.objects.using(using)
        .filter(resource_type__type_uri=type_uri)
        .order_by("pk")
        .values_list("pk", "etag", "content")
    )


def conflicts(source: str, against: str, using=None) -> list[dict]:
    """``{"kind", "key", "base", "current"}`` for each own row of branch
    `source` (a deletion marker too) whose ``base`` differs from the ETag of
    branch `against`'s effective item for the same key (``None`` for none, so
    a ``base`` of ``None`` conflicts once `against` has the key). Read on
    `using` (default: the router's read database)."""
    chain = chain_of(against, branches(using))
    found = []
    for kind, type_uri in ITEM_KINDS.items():
        rows = list(_rows(type_uri, using))
        current = {
            item_key(kind, content, chain[0]): etag
            for _pk, etag, content in effective_items(kind, rows, chain)
        }
        for _pk, _etag, content in rows:
            if branch_of(content) != source or not isinstance(content, Mapping):
                continue
            key = item_key(kind, content, chain[0])
            base, now = content.get("base"), current.get(key)
            if base != now:
                found.append(
                    {"kind": kind, "key": list(key), "base": base, "current": now}
                )
    return found


def pull(content: dict, using, all_branches=None) -> dict:
    """`content`, the body of a branch update, as a pull makes it: its
    ``branching_point`` the current version of each ancestor (`all_branches`,
    default the stored ones, say where the branch's chain is), its
    ``conflicts`` those against its parent, and ``pull`` removed."""
    name = content["name"]
    parent = content.get("parent")
    chain = ancestors(
        name, all_branches if all_branches is not None else branches(using)
    )
    pulled = {key: value for key, value in content.items() if key != "pull"}
    pulled["branching_point"] = {
        ancestor.name: current_version(ancestor.name, using) for ancestor in chain
    }
    pulled["conflicts"] = [] if parent is None else conflicts(name, parent, using)
    return pulled


def resolve_conflict(branch_name: str, kind: str, key: tuple, using: str):
    """Remove the conflict of item `key` of `kind` from the stored branch
    `branch_name`'s ``conflicts``, if listed. Call it in the transaction of
    the write that resolves it."""
    stored = (
        Resource.objects.using(using)
        .select_for_update()
        .filter(resource_type__type_uri=UI_BRANCH_TYPE_URI, content__name=branch_name)
        .order_by("pk")
        .first()
    )
    listed = stored.content.get("conflicts") if stored is not None else None
    if not isinstance(listed, list):
        return
    kept = [
        entry
        for entry in listed
        if not (
            isinstance(entry, Mapping)
            and entry.get("kind") == kind
            and entry.get("key") == list(key)
        )
    ]
    if len(kept) != len(listed):
        ResourceRegistration.update_locked(
            stored,
            {**stored.content, "conflicts": kept},
            resource_type=stored.resource_type,
            using=using,
        )
