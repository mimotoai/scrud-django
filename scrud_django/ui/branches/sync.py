"""Pulling a branch: moving its branching point to its ancestors' current
versions, and the conflicts that leaves between its overrides and the parent's
items, which are listed on the branch until each is resolved. Pushing a
branch's overrides into another branch, and restoring a version of a branch."""

from collections.abc import Mapping
from dataclasses import dataclass

from rest_framework.exceptions import PermissionDenied
from scoped_rbac.policy import Permission

from scrud_django.exceptions import (
    UIBranchInvalidError,
    UIPushConflictError,
    UIPushForbiddenError,
)
from scrud_django.models import Resource, ResourceType
from scrud_django.permissions import (
    may_act_in_context,
    resource_contexts,
)
from scrud_django.registration import (
    ResourceRegistration,
    get_resource_type_for,
    latest_revision_first,
    type_db,
)
from scrud_django.rows import by_pk, locked_by_pk, unsorted
from scrud_django.ui.branches import (
    ITEM_KINDS,
    MAIN,
    UI_BRANCH_SLUG,
    UI_BRANCH_TYPE_URI,
)
from scrud_django.ui.branches.effective import (
    branch_state,
    chain_of,
    parent_state,
)
from scrud_django.ui.branches.model import (
    Branch,
    authorizing_context,
    branch_of,
    item_key,
)
from scrud_django.ui.branches.reads import may_read_item
from scrud_django.ui.branches.store import BranchChainError, ancestors, branches
from scrud_django.ui.branches.versions import (
    batched_versions,
    current_version,
    record_now,
    snapshot,
)
from scrud_django.ui.layers import LAYER_BRANCH

# Flags an item's write accepts but never stores.
WRITE_ONLY = ("rebase",)


def _rows(type_uri: str, using) -> list:
    """``(pk, etag, content)`` of every item of `type_uri` on `using`, lowest
    pk first (sorted here: `scrud_django.rows`)."""
    return by_pk(
        unsorted(
            Resource.objects.using(using).filter(resource_type__type_uri=type_uri)
        ).values_list("pk", "etag", "content")
    )


def conflicts(source: str, against: str, using=None) -> list[dict]:
    """``{"kind", "key", "base", "current"}`` for each own row of branch
    `source` (a deletion marker too) whose ``base`` differs from the ETag of
    branch `against`'s effective item for the same key (``None`` for none, so
    a ``base`` of ``None`` conflicts once `against` has the key), as
    `against` shows it (`branch_state`: as of its branching point where it
    is pinned; ruling R38), the packaged layers' items beneath it: an
    override of a layer item that `against` does not override is compared
    with the layer's. Read on `using` (default: the router's read
    database)."""
    all_branches = branches(using)
    against_branch = chain_of(against, all_branches)[0]
    found = []
    for kind, type_uri in ITEM_KINDS.items():
        rows = list(_rows(type_uri, using))
        current = {
            item_key(kind, content, against_branch): etag
            for _pk, etag, content in branch_state(
                kind, type_uri, against, all_branches, using
            )
        }
        for _pk, _etag, content in rows:
            if branch_of(content) != source or not isinstance(content, Mapping):
                continue
            key = item_key(kind, content, against_branch)
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
        context on the target, for what is written, and, for a PUT or DELETE,
        on the target's stored row in its stored contexts, as a direct write's
        object check (``get_object()``) reads it."""
        item = self.content if self.content is not None else self.row.content
        context = authorizing_context(self.kind, item, self.target)
        permission = Permission(self.method, self.type_uri)
        allowed = may_act_in_context(request, permission, item, [context])
        if allowed and self.row is not None:
            stored = self.row.content
            allowed = may_act_in_context(
                request, permission, stored, resource_contexts(stored)
            )
        if not allowed:
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


def _own_rows(type_uri: str, name: str, using, lock=True) -> list[Resource]:
    """Branch `name`'s own items of `type_uri` on `using`, lowest pk first,
    each locked until the transaction ends unless not `lock` (items stored
    before branches, with no ``branch``, are main's)."""
    rows = Resource.objects.using(using).filter(
        resource_type_id__in=_type_ids(type_uri, using)
    )
    if name != MAIN:
        rows = rows.filter(content__branch=name)
    found = locked_by_pk(rows) if lock else by_pk(unsorted(rows), pk=lambda row: row.pk)
    return [row for row in found if branch_of(row.content) == name]


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


def _parent_items(kind, type_uri, target: Branch, all_branches, using) -> dict:
    """``(pk, etag, content)`` of each item key visible on `target`'s
    parent as `target` shows it (`parent_state`, with the packaged layers'
    items beneath it, pk ``None``; a root's are the layers' alone)."""
    parent = LAYER_BRANCH if target.parent is None else all_branches[target.parent]
    return {
        item_key(kind, content, parent): (pk, etag, content)
        for pk, etag, content in parent_state(
            kind, type_uri, target, all_branches, using
        )
    }


def _parent_etags(kind, type_uri, target: Branch, all_branches, using) -> dict:
    """The ETag of each item key visible on `target`'s parent as `target`
    shows it (`_parent_items`): the ``base`` of a deletion marker a push
    adds to `target` over a row of its own."""
    return {
        key: etag
        for key, (_pk, etag, _content) in _parent_items(
            kind, type_uri, target, all_branches, using
        ).items()
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
    `target`, whose own rows are `theirs` (by key); `parent()` gives the
    ETags of the items of `target`'s parent (by key). A row the target has
    is updated and keeps its ``base``. Where the target has none, the
    conflict check accepted `item`'s ``base`` as the target's item, its
    parent's: the new row takes it (ruling R41, minor 1: not a second read,
    which a concurrent write may have moved). A deletion marker deletes the
    target's row, and hides the parent's item, if any, with a marker of its
    own."""
    mine = theirs.get(key)
    checked = item.get("base")

    def write(method, row=None, base=None):
        content = (
            None if method == "http.DELETE" else _onto(kind, item, key, target, base)
        )
        return _Write(kind, type_uri, key, method, target, row, content)

    if item.get("deleted") is True:
        if mine is None:
            # Nothing of the target's own: a marker hides the parent's item,
            # if the check found one.
            return [] if checked is None else [write("http.POST", base=checked)]
        writes = [write("http.DELETE", row=mine)]
        etags = parent()
        if key in etags:
            writes.append(write("http.POST", base=etags[key]))
        return writes
    if mine is not None:
        return [write("http.PUT", row=mine, base=mine.content.get("base"))]
    return [write("http.POST", base=checked)]


def _once(compute):
    """`compute()`, computed on the first call only."""
    found = []

    def get():
        if not found:
            found.append(compute())
        return found[0]

    return get


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


def _resolve_deleted(writes, target: Branch, using):
    """Remove from `target`'s ``conflicts`` the key of each of its own rows
    `writes` deleted: as a direct DELETE of an override does."""
    for write in writes:
        if write.method == "http.DELETE":
            resolve_conflict(target.name, write.kind, write.key, using)


def _readable_conflicts(request, found, source, target, all_branches, using):
    """The entries of `found` (`conflicts` of `source` against `target`) on
    items `request` may GET in the target: the target's item for the key, or,
    where it has none, the source's as the push would write it there."""
    shown = []
    for kind, type_uri in ITEM_KINDS.items():
        entries = [entry for entry in found if entry["kind"] == kind]
        if not entries:
            continue
        theirs = {
            item_key(kind, content, target): (pk, content)
            for pk, _etag, content in branch_state(
                kind, type_uri, target.name, all_branches, using, include_deleted=True
            )
        }
        mine = {}
        for _pk, _etag, content in _rows(type_uri, using):
            if branch_of(content) == source.name and isinstance(content, Mapping):
                mine.setdefault(item_key(kind, content, source), content)
        for entry in entries:
            key = tuple(entry["key"])
            pk, item = theirs.get(key, (None, None))
            if item is None and key in mine:
                # As the push would write it: a new row on the target.
                pk, item = None, _onto(kind, mine[key], key, target, None)
            if item is not None and may_read_item(request, kind, pk, item):
                shown.append(entry)
    return shown


def refuse_push_between(source: str, target: str, all_branches):
    """Refuse (`UIBranchInvalidError`, 400) a push of branch `source` into
    branch `target` (both in `all_branches`) that would empty a root (a
    source with no parent: ``main`` is the live site's) or write a branch's
    overrides into its own base (a source that is the target or one of its
    ancestors; a broken chain of the target is refused alike)."""
    if all_branches[source].parent is None:
        raise UIBranchInvalidError(
            f"Branch {source!r} has no parent: pushing it would empty "
            "it of its items. Restore a version, or edit it directly."
        )
    try:
        chain = ancestors(target, all_branches)
    except BranchChainError as error:
        raise UIBranchInvalidError(str(error)) from error
    if source == target or source in {branch.name for branch in chain}:
        raise UIBranchInvalidError(
            f"Branch {source!r} cannot be pushed into {target!r}: it is "
            "the target or one of its ancestors."
        )


@dataclass(frozen=True)
class PushPlan:
    """What a push of one branch into another would do, for one user:
    `writes`, the item writes into the target, in order (`_push_writes`);
    `emptied`, the deletions of the source's own rows; `conflicts`, those of
    the source against the target (`conflicts`) on items the user may read
    there (`_readable_conflicts`); and `hidden_conflicts`, how many others
    there are. A push with any conflict is refused."""

    writes: list[_Write]
    emptied: list[_Write]
    conflicts: list[dict]
    hidden_conflicts: int

    @property
    def conflicting(self) -> bool:
        return bool(self.conflicts) or self.hidden_conflicts > 0


def plan_push(
    request, source: str, target: str, using=None, *, lock: bool, all_branches=None
) -> PushPlan:
    """The `PushPlan` of a push of branch `source` into branch `target` (by
    name, both in `all_branches`, default `branches(using)`) for `request`,
    read on `using` (default: the router's read database), its rows locked
    until the transaction ends with `lock` (required: a write's plan locks
    them, a read's does not). Nothing is written and nothing
    raised for a conflict, and no grant is checked: `apply_push` checks
    what a push needs, and a preview what it shows."""
    if all_branches is None:
        all_branches = branches(using)
    source, target = all_branches[source], all_branches[target]
    source_rows = {
        kind: _own_rows(type_uri, source.name, using, lock)
        for kind, type_uri in ITEM_KINDS.items()
    }
    target_rows = {
        kind: _own_rows(type_uri, target.name, using, lock)
        for kind, type_uri in ITEM_KINDS.items()
    }
    found = conflicts(source.name, target.name, using)
    shown = (
        _readable_conflicts(request, found, source, target, all_branches, using)
        if found
        else []
    )
    writes = []
    for kind, type_uri in ITEM_KINDS.items():
        theirs = _keyed(kind, target_rows[kind], target)
        parent = _once(
            lambda kind=kind, type_uri=type_uri: _parent_etags(
                kind, type_uri, target, all_branches, using
            )
        )
        for key, row in _keyed(kind, source_rows[kind], source).items():
            writes.extend(
                _push_writes(kind, type_uri, row.content, key, target, theirs, parent)
            )
    emptied = [
        _Write(kind, ITEM_KINDS[kind], (), "http.DELETE", source, row)
        for kind, rows in source_rows.items()
        for row in rows
    ]
    return PushPlan(writes, emptied, shown, len(found) - len(shown))


# A previewed change's action, by the method of its write.
ACTIONS = {"http.POST": "create", "http.PUT": "update", "http.DELETE": "delete"}


def push_changes(
    request, plan: PushPlan, target: str, using=None, *, all_branches=None
) -> tuple[list[dict], int]:
    """``(changes, unreadable)``: ``{"kind", "key", "action", "before",
    "after"}`` of each write of `plan` (a push into branch `target`) that
    `request` may read, in order, and how many others there are. A write is
    readable when `request` may read (`may_read_item`) its ``before``, its
    ``after`` and the source's row it comes from. Its ``action`` is by its
    method (`ACTIONS`); ``before``, the target's row it updates or deletes,
    or, for a new row, the item the target shows beneath it (its parent's,
    or a packaged layer's), ``None`` where there is none; ``after``, the row
    as written, ``None`` for a deletion."""
    if all_branches is None:
        all_branches = branches(using)
    target = all_branches[target]
    emptied = {}  # the source's own rows, by kind
    for write in plan.emptied:
        emptied.setdefault(write.kind, []).append(write)
    sources = {
        kind: _keyed(kind, [write.row for write in writes], writes[0].target)
        for kind, writes in emptied.items()
    }
    beneath = {
        kind: _once(
            lambda kind=kind, type_uri=type_uri: _parent_items(
                kind, type_uri, target, all_branches, using
            )
        )
        for kind, type_uri in ITEM_KINDS.items()
    }
    changes = []
    for write in plan.writes:
        if write.row is not None:
            before_pk, before = write.row.pk, write.row.content
        else:
            before_pk, _etag, before = beneath[write.kind]().get(
                write.key, (None, None, None)
            )
        source = sources.get(write.kind, {}).get(write.key)
        seen = (
            (source.pk, source.content) if source is not None else (None, None),
            (before_pk, before),
            (write.row.pk if write.row is not None else None, write.content),
        )
        if all(
            may_read_item(request, write.kind, pk, content)
            for pk, content in seen
            if content is not None
        ):
            changes.append(
                {
                    "kind": write.kind,
                    "key": list(write.key),
                    "action": ACTIONS[write.method],
                    "before": before,
                    "after": write.content,
                }
            )
    return changes, len(plan.writes) - len(changes)


def apply_push(request, content, using) -> int:
    """Write the overrides of branch ``content["source"]`` into branch
    ``content["target"]`` for `request`, in its transaction on `using`, and
    return the target's new version. Everything is checked before anything
    is written: GET and PUT on the source (``PermissionDenied``, 403), no
    `conflicts` of the source against the target (`UIPushConflictError`,
    409, describing only those on items the pusher may read there), and, for
    each item, the grant a direct write of it into the target needs
    (`UIPushForbiddenError`, 403). Then, in one `batched_versions`, the
    `plan_push` is written: each override into the target (`_push_writes`;
    a target row it deletes leaves the target's ``conflicts``), the
    source's own rows are deleted and its ``conflicts`` cleared, and the
    target's version is recorded, then the source's: when the target is the
    source's parent, the source's branching point first moves to its
    ancestors' current versions, as a pull would."""
    all_branches = branches(using)
    source = all_branches[content["source"]]
    target = all_branches[content["target"]]
    locked = _locked_branches((source.name, target.name), using)
    for method in ("http.GET", "http.PUT"):
        _may_on_branch(request, method, source, locked.get(source.name))
    plan = plan_push(
        request, source.name, target.name, using, lock=True, all_branches=all_branches
    )
    if plan.conflicting:
        raise UIPushConflictError(plan.conflicts, plan.hidden_conflicts)
    for write in plan.writes:
        write.check(request)
    with batched_versions():
        _apply([*plan.writes, *plan.emptied], using)
        _resolve_deleted(plan.writes, target, using)
        number = record_now(target.name, using)
        moved = _empty_source(source, target, locked.get(source.name), using)
        if plan.emptied or moved:
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
        _resolve_deleted(writes, target, using)
        return record_now(target.name, using)
