"""Pulling a branch: moving its branching point to its ancestors' current
versions, and the conflicts that leaves between its overrides and the parent's
items, which are listed on the branch until each is resolved. Pushing a
branch's overrides into another branch, and restoring a version of a branch."""

from collections.abc import Mapping
from dataclasses import dataclass

from rest_framework.exceptions import PermissionDenied
from scoped_rbac.policy import Permission

from scrud_django.exceptions import UIPushConflictError, UIPushForbiddenError
from scrud_django.models import Resource, ResourceType
from scrud_django.permissions import may_act_in_context
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    latest_revision_first,
    type_db,
)
from scrud_django.ui.branches import (
    ITEM_KINDS,
    MAIN,
    UI_BRANCH_SLUG,
    UI_BRANCH_TYPE_URI,
)
from scrud_django.ui.branches.effective import chain_of, effective_items
from scrud_django.ui.branches.model import (
    Branch,
    authorizing_context,
    branch_of,
    item_key,
)
from scrud_django.ui.branches.store import ancestors, branches
from scrud_django.ui.branches.versions import (
    batched_versions,
    current_version,
    record_now,
    snapshot,
)

# Flags an item's write accepts but never stores.
WRITE_ONLY = ("rebase",)


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


@dataclass(frozen=True)
class _Write:
    """One item write of a push or restore into branch `target`: ``http.PUT``
    of `content` over the target's own `row`, ``http.POST`` of a new row
    with `content`, or ``http.DELETE`` of `row`."""

    kind: str
    type_uri: str
    key: tuple
    method: str
    target: Branch
    row: Resource | None = None
    content: dict | None = None

    def check(self, request):
        """Refuse (`UIPushForbiddenError`, 403) unless `request` may make
        this write directly: `method` on the item's type in its authorizing
        context on the target."""
        item = self.content if self.content is not None else self.row.content
        context = authorizing_context(self.kind, item, self.target)
        permission = Permission(self.method, self.type_uri)
        if not may_act_in_context(request, permission, item, [context]):
            raise UIPushForbiddenError(self.kind, self.key)

    def apply(self, resource_type, using):
        if self.method == "http.DELETE":
            ResourceRegistration.delete_locked(
                self.row, resource_type=resource_type, using=using
            )
        elif self.method == "http.PUT":
            ResourceRegistration.update_locked(
                self.row, self.content, resource_type=resource_type, using=using
            )
        else:
            ResourceRegistration.register_instance(
                Resource(content=self.content, resource_type=resource_type)
            )


def _item_type(type_uri: str) -> ResourceType:
    """The latest revision of the item type `type_uri`."""
    return latest_revision_first(
        ResourceType.objects.using(type_db()).filter(type_uri=type_uri)
    ).first()


def _locked_branches(names, using) -> dict[str, Resource]:
    """The stored rows of the branches `names` on `using`, by name, each
    locked (``select_for_update``) in name order until the transaction ends.
    A virtual ``main`` has none."""
    locked = {}
    for name in sorted(set(names)):
        row = (
            Resource.objects.using(using)
            .select_for_update()
            .filter(
                resource_type_id__in=_type_ids(UI_BRANCH_TYPE_URI, using),
                content__name=name,
            )
            .order_by("pk")
            .first()
        )
        if row is not None:
            locked[name] = row
    return locked


def _type_ids(type_uri, using) -> list:
    return list(
        ResourceType.objects.using(using)
        .filter(type_uri=type_uri)
        .values_list("pk", flat=True)
    )


def _own_rows(type_uri: str, name: str, using) -> list[Resource]:
    """Branch `name`'s own items of `type_uri` on `using`, lowest pk first,
    each locked until the transaction ends (items stored before branches,
    with no ``branch``, are main's)."""
    rows = (
        Resource.objects.using(using)
        .select_for_update()
        .filter(resource_type_id__in=_type_ids(type_uri, using))
    )
    if name != MAIN:
        rows = rows.filter(content__branch=name)
    return [row for row in rows.order_by("pk") if branch_of(row.content) == name]


def _keyed(kind: str, rows, branch: Branch) -> dict[tuple, Resource]:
    """`rows` (items on `branch`) by `item_key`, the lowest pk's for a key
    stored twice."""
    keyed = {}
    for row in rows:
        if isinstance(row.content, Mapping) and isinstance(
            row.content.get("name"), str
        ):
            keyed.setdefault(item_key(kind, row.content, branch), row)
    return keyed


def _parent_etags(kind, type_uri, target: Branch, all_branches, using) -> dict:
    """The ETag of each item key visible on `target`'s parent (none without
    one): the ``base`` of a row a push adds to `target`."""
    if target.parent is None:
        return {}
    chain = chain_of(target.parent, all_branches)
    return {
        item_key(kind, content, chain[0]): etag
        for _pk, etag, content in effective_items(kind, _rows(type_uri, using), chain)
    }


def _onto(kind: str, item, key: tuple, target: Branch, base) -> dict:
    """`item` as a row of branch `target` with item key `key` stores it: its
    ``branch``, a stylesheet's ``applies_to``, its ``rbac_context`` (its
    authorizing context there) and `base`, without write-only flags."""
    content = {name: value for name, value in item.items() if name not in WRITE_ONLY}
    content["branch"] = target.name
    if kind == "stylesheet":
        content["applies_to"] = key[1]
    content["rbac_context"] = authorizing_context(kind, content, target)
    content["base"] = base
    return content


def _push_writes(kind, type_uri, item, key, target, theirs, parent) -> list[_Write]:
    """The writes that put the source's own `item` (item key `key`) into
    `target`, whose own rows are `theirs` (by key) and whose parent's items
    have the ETags `parent` (by key). A row the target has is updated and
    keeps its ``base``; a new one's ``base`` is the parent's ETag. A deletion
    marker deletes the target's row, and hides the parent's item, if any,
    with a marker of its own."""
    mine = theirs.get(key)

    def write(method, row=None, base=None):
        content = (
            None if method == "http.DELETE" else _onto(kind, item, key, target, base)
        )
        return _Write(kind, type_uri, key, method, target, row, content)

    if item.get("deleted") is True:
        writes = [] if mine is None else [write("http.DELETE", row=mine)]
        if key in parent:
            writes.append(write("http.POST", base=parent[key]))
        return writes
    if mine is not None:
        return [write("http.PUT", row=mine, base=mine.content.get("base"))]
    return [write("http.POST", base=parent.get(key))]


def _may_on_branch(request, method, branch: Branch, stored: Resource | None):
    """Refuse (``PermissionDenied``, 403) unless `request` may `method`
    branch `branch` (stored as `stored`, ``None`` for a virtual main) in
    its context."""
    content = (
        stored.content
        if stored is not None
        else {"name": branch.name, "rbac_context": branch.context}
    )
    permission = Permission(method, UI_BRANCH_TYPE_URI)
    if not may_act_in_context(request, permission, content, [branch.context]):
        raise PermissionDenied(
            f"A push from branch {branch.name!r} needs {method} on it: it reads "
            "and empties its overrides."
        )


def _apply(writes, using):
    """Run `writes`, already checked, in order."""
    types = {}
    for write in writes:
        if write.type_uri not in types:
            types[write.type_uri] = _item_type(write.type_uri)
        write.apply(types[write.type_uri], using)


def apply_push(request, content, using) -> int:
    """Write the overrides of branch ``content["source"]`` into branch
    ``content["target"]`` for `request`, in its transaction on `using`, and
    return the target's new version. Everything is checked before anything
    is written: GET and PUT on the source (``PermissionDenied``, 403), no
    `conflicts` of the source against the target (`UIPushConflictError`,
    409), and, for each item, the grant a direct write of it into the target
    needs (`UIPushForbiddenError`, 403). Then, in one `batched_versions`, each
    override is written into the target (`_push_writes`), the source's own
    rows are deleted and its ``conflicts`` cleared, and the target's version
    is recorded, then the source's: when the target is the source's parent,
    the source's branching point first moves to its ancestors' current
    versions, as a pull would."""
    all_branches = branches(using)
    source = all_branches[content["source"]]
    target = all_branches[content["target"]]
    locked = _locked_branches((source.name, target.name), using)
    for method in ("http.GET", "http.PUT"):
        _may_on_branch(request, method, source, locked.get(source.name))
    source_rows = {
        kind: _own_rows(type_uri, source.name, using)
        for kind, type_uri in ITEM_KINDS.items()
    }
    target_rows = {
        kind: _own_rows(type_uri, target.name, using)
        for kind, type_uri in ITEM_KINDS.items()
    }
    found = conflicts(source.name, target.name, using)
    if found:
        raise UIPushConflictError(found)
    writes = []
    for kind, type_uri in ITEM_KINDS.items():
        theirs = _keyed(kind, target_rows[kind], target)
        parent = _parent_etags(kind, type_uri, target, all_branches, using)
        for key, row in _keyed(kind, source_rows[kind], source).items():
            writes.extend(
                _push_writes(kind, type_uri, row.content, key, target, theirs, parent)
            )
    for write in writes:
        write.check(request)
    emptied = [
        _Write(kind, ITEM_KINDS[kind], (), "http.DELETE", source, row)
        for kind, rows in source_rows.items()
        for row in rows
    ]
    with batched_versions():
        _apply([*writes, *emptied], using)
        number = record_now(target.name, using)
        moved = _empty_source(source, target, locked.get(source.name), using)
        if emptied or moved:
            record_now(source.name, using)
    return number


def _empty_source(source: Branch, target: Branch, stored, using) -> bool:
    """Clear the pushed `source`'s ``conflicts`` (its row `stored`, ``None``
    for a virtual main) and, when `target` is its parent, move its branching
    point to its ancestors' current versions (the target's new one
    included), as a pull would; whether its row changed."""
    if stored is None or not isinstance(stored.content, Mapping):
        return False
    content = dict(stored.content)
    if "conflicts" in content:
        content["conflicts"] = []
    if source.parent == target.name:
        content["branching_point"] = {
            ancestor.name: current_version(ancestor.name, using)
            for ancestor in ancestors(source.name, branches(using))
        }
    if content == stored.content:
        return False
    ResourceRegistration.update_locked(
        stored,
        content,
        resource_type=get_resource_type_for(UI_BRANCH_SLUG, using=type_db()),
        using=using,
    )
    return True


def apply_restore(request, content, using) -> int:
    """Make branch ``content["target"]``'s own items those of its version
    ``content["restore"]`` (`snapshot`, `UIBranchVersionMissingError` 404 when
    it is not stored) for `request`, in its transaction on `using`, and
    return the target's new version. Each own row the version lacks is
    deleted; each of its items is written over the row with its key, or
    added; each item keeps the ``base`` it had then. A row already as the
    version has it is not written. Every write is first checked as a push's
    is (`UIPushForbiddenError`, 403); then all are made, and one version is
    recorded. The branch itself (its base mode included) is unchanged."""
    all_branches = branches(using)
    target = all_branches[content["target"]]
    _locked_branches((target.name,), using)
    items = snapshot(target.name, content["restore"], using).get("items")
    items = items if isinstance(items, Mapping) else {}
    writes = []
    for kind, type_uri in ITEM_KINDS.items():
        theirs = _keyed(kind, _own_rows(type_uri, target.name, using), target)
        wanted = {}
        for item in items.get(kind) or []:
            if isinstance(item, Mapping) and isinstance(item.get("name"), str):
                key = item_key(kind, item, target)
                wanted.setdefault(key, _onto(kind, item, key, target, item.get("base")))
        for key, row in theirs.items():
            if key not in wanted:
                writes.append(_Write(kind, type_uri, key, "http.DELETE", target, row))
        for key, restored in wanted.items():
            row = theirs.get(key)
            if row is None:
                method = "http.POST"
            elif row.content != restored:
                method = "http.PUT"
            else:
                continue
            writes.append(_Write(kind, type_uri, key, method, target, row, restored))
    for write in writes:
        write.check(request)
    with batched_versions():
        _apply(writes, using)
        return record_now(target.name, using)
