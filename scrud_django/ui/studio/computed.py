"""The studio's data as computed resource types (spec E2), so that a studio
built as a UI layer renders it through the cascade like any resource:

- ``ui-branch-items/<branch>/``: every item a branch shows, own, inherited
  or packaged, with its content, its conflict and the action that overrides
  it on the branch;
- ``ui-branch-status/<branch>/``: the branch's chain, changes, conflicts and
  browse URL, what the user may do with it (``may_write``, ``may_publish``,
  the ``pull`` and ``promote`` actions), its pushes and versions, and the
  studio's endpoints;
- ``ui-type-catalog/all/``: the registered types, as the bootstrap studio
  lists them;
- ``ui-selectable-branches/mine/``: the branches the user may select;
- ``ui-push-previews/<source>/``: what a push of a branch would change in
  its target (spec E3).

Each answers only what its user may GET as JSON: a branch it may select
(`selection.selectable`; the push preview: both of its branches the user may
GET), else 404, the same for every cause; an item the item's own read check allows (packaged
layer items are public). A write is offered as an action (`_action`) only
when the user's grants allow it (`authz.may_send`), and never on a published
branch. The bootstrap studio's strip reads the same functions
(`scrud_django.ui.studio.page`)."""

import contextlib
import importlib.resources
import logging
from collections.abc import Mapping

from django.db.models import Q
from django.http import Http404
from django.urls import NoReverseMatch, reverse

from scrud_django import registration
from scrud_django.computed import ComputedResourceViewSet
from scrud_django.exceptions import UIBranchInvalidError
from scrud_django.models import Resource
from scrud_django.permissions import may_read_in_context, resource_contexts
from scrud_django.rows import unsorted
from scrud_django.ui.authz import may_post, may_send
from scrud_django.ui.branches import (
    ITEM_KINDS,
    MAIN,
    UI_BRANCH_SLUG,
    UI_BRANCH_TYPE_URI,
    UI_BRANCH_VERSION_SLUG,
    UI_BRANCH_VERSION_TYPE_URI,
    UI_PUSH_SLUG,
    UI_PUSH_TYPE_URI,
)
from scrud_django.ui.branches.effective import (
    Selection,
    chain_of,
    stored_state,
)
from scrud_django.ui.branches.model import branch_of, item_key
from scrud_django.ui.branches.reads import may_read_item
from scrud_django.ui.branches.selection import (
    may_get,
    parse_value,
    selectable,
    selection_value,
)
from scrud_django.ui.branches.store import BranchChainError, branches, published_root
from scrud_django.ui.branches.sync import (
    plan_push,
    push_changes,
    refuse_push_between,
)
from scrud_django.ui.cascade import request_memo
from scrud_django.ui.layers import LAYER_BRANCH, LAYER_BRANCH_NAME, layer_conflicts
from scrud_django.ui.studio.page import (
    branch_conflicts,
    browse_url,
    conflict_key,
    own_items,
    own_rows,
    readable_rows,
    studio_branches,
)
from scrud_django.ui.studio.views import registered_types
from scrud_django.ui.studio.writes import KIND_SLUGS, item_content

TYPE_URI_BASE = "http://api.openteams.com/json-ld/"
UI_BRANCH_ITEMS_SLUG = "ui-branch-items"
UI_BRANCH_STATUS_SLUG = "ui-branch-status"
UI_TYPE_CATALOG_SLUG = "ui-type-catalog"
UI_SELECTABLE_BRANCHES_SLUG = "ui-selectable-branches"
UI_PUSH_PREVIEWS_SLUG = "ui-push-previews"
# The one key of the catalog and of the branch picker.
CATALOG_KEY = "all"
SELECTABLE_KEY = "mine"
# The query parameter of the status that names the type to browse.
TYPE_PARAMETER = "type"
# The query parameter of the push preview that names the target branch.
TARGET_PARAMETER = "target"
# The most pushes, and versions, a branch's status lists.
LISTED_MAX = 20
# The slug the studio's slug-taking routes are reversed with, then replaced
# by ``{slug}`` (`_endpoints`).
_SLUG_PLACEHOLDER = "scrud-studio-slug"

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _stored_state():
    """The block reads the stored items as a JSON request does: one memo
    for the response (shared with a render's, for an embed), and neither
    the safe mode nor the drafts of a render it is embedded in (an embed
    runs inside its embedder's context)."""
    with request_memo(), stored_state():
        yield


def _selected(request, key):
    """``(selection, branch, all_branches)`` of `key`, a selection value
    (a branch name, optionally ``@pinned``) the user may select; else
    `Http404`, the same for every cause."""
    selection = parse_value(key)
    all_branches = branches()
    if selection is None or not selectable(
        request, selection, all_branches=all_branches
    ):
        raise Http404("No such branch.")
    branch = all_branches[selection.branch]
    if branch.parent is None:  # a root has no branching point to pin to
        selection = Selection(branch=branch.name)
    return selection, branch, all_branches


def _may_get_branch(request, branch) -> bool:
    """Whether the user of `request` may GET `branch` (`may_get`): ``main``,
    which everyone may select, needs no grant, as for a push."""
    return branch.name == MAIN or may_get(request, branch)


def _named_chain(request, branch, root, all_branches) -> list:
    """`branch`'s chain (`chain_of`) as its status names it: the branch, its
    working ancestors (a selectable branch's user may GET them all:
    `selection.may_get_chain`) and its published `root` (which the browse
    URL names anyway), then each ancestor above it only when the user may
    GET it (`_may_get_branch`)."""
    named = []
    above_root = False
    for ancestor in chain_of(branch.name, all_branches):
        if (
            not above_root
            or not ancestor.published
            or _may_get_branch(request, ancestor)
        ):
            named.append(ancestor)
        if root is None or ancestor.name == root.name:
            above_root = True
    return named


def _conflicts(request, branch) -> list[dict]:
    """``{kind, name, applies_to, with}`` of each conflict of `branch` on
    an own row of it the user may read: those a pull listed on it
    (``with: "parent"``), then those with a packaged layer
    (`layer_conflicts`, ``with: "layer"``)."""
    own = {}
    for kind, rows in own_rows(branch).items():
        for pk, _etag, content in rows:
            own.setdefault((kind, item_key(kind, content, branch)), (pk, content))

    def readable(kind, key):
        pk, content = own.get((kind, key), (None, None))
        return content is not None and may_read_item(request, kind, pk, content)

    found = [
        {**conflict, "with": "parent"}
        for conflict in branch_conflicts(branch)
        if readable(conflict["kind"], conflict_key(conflict))
    ]
    for conflict in layer_conflicts(branch):
        kind, key = conflict["kind"], tuple(conflict["key"])
        if readable(kind, key):
            found.append(
                {
                    "kind": kind,
                    "name": key[0],
                    "applies_to": key[1] if kind == "stylesheet" else "",
                    "with": "layer",
                }
            )
    return found


class BranchItemsViewSet(ComputedResourceViewSet):
    """``ui-branch-items/<branch>/``: the items `branch` shows (live, or
    pinned with ``@pinned``)."""

    def compute(self, request, key):
        with _stored_state():
            selection, branch, all_branches = _selected(request, key)
            on = {**all_branches, LAYER_BRANCH_NAME: LAYER_BRANCH}
            conflicts = {}  # the first listed for a key: parent, then layer
            for conflict in _conflicts(request, branch):
                key = (conflict["kind"], conflict_key(conflict))
                conflicts.setdefault(key, conflict["with"])
            items = []
            for kind in ITEM_KINDS:
                rows = readable_rows(request, kind, selection)
                for pk, etag, content in rows:
                    items.append(
                        _item(
                            request,
                            kind,
                            (pk, etag, content),
                            selection,
                            branch,
                            on,
                            conflicts,
                        )
                    )
            return {"branch": selection_value(selection), "items": items}


def _action(method: str, href: str, body: dict, etag: str | None = None) -> dict:
    """What a studio control sends, unchanged: `body` as JSON with `method`
    to the API path `href`, with `etag` (quoted) as ``If-Match``, or none."""
    return {"method": method, "href": href, "body": body, "etag": etag}


def _override(request, kind, key, content, selection, branch) -> dict | None:
    """The POST that creates `branch`'s override of the item of `kind` it
    shows with `content` and `item_key` `key`, but does not own: the
    content without ``base`` (the server sets it) and ``deleted``, as a
    studio save sends it (`item_content`: on `branch`, in its authorizing
    context). ``None`` on a published branch (a push writes there), on a
    ``@pinned`` view (`selection`: it shows the parent as of the branching
    point, but the server bases the override on what the branch itself
    shows, so it would silently revert the parent's changes since), for a
    deletion marker, and when the user may not POST that body."""
    if branch.published or selection.pinned or content.get("deleted") is True:
        return None
    kept = {k: v for k, v in content.items() if k not in ("base", "deleted")}
    target = key[1] if kind == "stylesheet" else ""
    body = item_content(kind, key[0], target, kept, branch)
    if not may_post(request, ITEM_KINDS[kind], body):
        return None
    return _action("POST", reverse(f"{KIND_SLUGS[kind]}-list"), body)


def _item(request, kind, row, selection, branch, on, conflicts) -> dict:
    """The entry of the item of `kind` stored as `row`, ``(pk, etag,
    content)`` (a row `effective_rows` gives), in the items of `branch`
    shown as `selection`; `on` are the branches by name, the layers'
    virtual branch included."""
    pk, etag, content = row
    source = branch_of(content)
    key = item_key(kind, content, on.get(source, branch))
    packaged = source == LAYER_BRANCH_NAME
    own = source == branch.name
    item = {"kind": kind, "name": key[0]}
    if kind == "stylesheet":
        item["applies_to"] = key[1]
    item["origin"] = "packaged" if packaged else "own" if own else "inherited"
    item["from"] = None if packaged else source
    item["href"] = (
        reverse(f"{KIND_SLUGS[kind]}-detail", args=[pk])
        if pk is not None and not packaged
        else None
    )
    item["etag"] = f'"{etag}"' if etag else None
    item["content"] = content
    item["conflict"] = conflicts.get((kind, key)) if own else None
    item["override"] = (
        None if own else _override(request, kind, key, content, selection, branch)
    )
    return item


class BranchStatusViewSet(ComputedResourceViewSet):
    """``ui-branch-status/<branch>/``: `branch`'s chain, changes, conflicts
    and, with ``?type=<slug>``, the URL of that type's list on it."""

    def compute(self, request, key):
        with _stored_state():
            selection, branch, all_branches = _selected(request, key)
            try:
                root = published_root(branch.name, all_branches)
            except BranchChainError as error:
                # `selectable` refuses such a branch: not expected, so said.
                logger.warning(
                    "scrud-ui: the status of branch %r has no published root: %s",
                    branch.name,
                    error,
                )
                root = None
            slug = request.query_params.get(TYPE_PARAMETER)
            chain = _named_chain(request, branch, root, all_branches)
            content, etag = _branch_row(branch)
            may_write = (
                not branch.published
                and branch.pk is not None
                and may_send(request, "PUT", UI_BRANCH_TYPE_URI, content)
            )
            return {
                "branch": selection_value(selection),
                "published": branch.published,
                "chain": [
                    {"name": b.name, "published": b.published, "prefix": b.prefix}
                    for b in chain
                ],
                "changed": own_items(request, branch),
                "conflicts": _conflicts(request, branch),
                "browse_url": browse_url(slug, root, selection) if slug else None,
                "may_write": may_write,
                "may_publish": may_send(
                    request, "PUT", UI_BRANCH_TYPE_URI, content, [""]
                ),
                "pushes": _pushes(request, branch),
                "versions": _versions(request, branch),
                "actions": _actions(request, branch, content, etag, chain, may_write),
                "endpoints": _endpoints(),
            }


def _branch_row(branch) -> tuple[dict, str | None]:
    """``(content, etag)`` of `branch` as stored; for the virtual ``main``
    (or a row gone since the branches were read), its name and context and
    no ETag. Read unsorted (`scrud_django.rows`)."""
    if branch.pk is not None:
        rows = unsorted(Resource.objects.filter(pk=branch.pk))
        for content, etag in rows.values_list("content", "etag")[:1]:
            if isinstance(content, Mapping):
                return dict(content), etag
    return {"name": branch.name, "rbac_context": branch.context}, None


def _latest_readable(request, type_uri, rows) -> list[tuple[int, dict]]:
    """``(pk, content)`` of the `LISTED_MAX` newest of `rows` (resources of
    `type_uri`) the user may GET as JSON (the detail route's check,
    `may_read_in_context` in the resource's contexts), newest first. The
    keys are sorted alone, then the rows read by key, a batch at a time
    (`scrud_django.rows`)."""
    pks = list(rows.order_by("-pk").values_list("pk", flat=True))
    found = []
    for start in range(0, len(pks), LISTED_MAX):
        batch = pks[start : start + LISTED_MAX]
        contents = dict(
            unsorted(rows.filter(pk__in=batch)).values_list("pk", "content")
        )
        for pk in batch:
            content = contents.get(pk)
            if isinstance(content, Mapping) and may_read_in_context(
                request, type_uri, content, resource_contexts(content)
            ):
                found.append((pk, content))
                if len(found) == LISTED_MAX:
                    return found
    return found


def _pushes(request, branch) -> list[dict]:
    """``{href, source, target, state, label}`` of the latest pushes with
    `branch` as source or target (restores included) that the user may
    GET, newest first."""
    rows = Resource.objects.filter(resource_type__type_uri=UI_PUSH_TYPE_URI).filter(
        Q(content__source=branch.name) | Q(content__target=branch.name)
    )
    found = []
    for pk, content in _latest_readable(request, UI_PUSH_TYPE_URI, rows):
        if branch.name not in (content.get("source"), content.get("target")):
            continue  # a collation that ignores case matched it
        label = content.get("label")
        found.append(
            {
                "href": reverse(f"{UI_PUSH_SLUG}-detail", args=[pk]),
                "source": content.get("source"),
                "target": content.get("target"),
                "state": content.get("state"),
                "label": label if isinstance(label, str) else None,
            }
        )
    return found


def _versions(request, branch) -> list[dict]:
    """``{number, href}`` of `branch`'s latest versions the user may GET,
    newest first (none when they may not GET its versions)."""
    rows = Resource.objects.filter(
        resource_type__type_uri=UI_BRANCH_VERSION_TYPE_URI,
        content__branch=branch.name,
    )
    return [
        {
            "number": content.get("number"),
            "href": reverse(f"{UI_BRANCH_VERSION_SLUG}-detail", args=[pk]),
        }
        for pk, content in _latest_readable(request, UI_BRANCH_VERSION_TYPE_URI, rows)
        if content.get("branch") == branch.name
    ]


def _actions(request, branch, content, etag, chain, may_write) -> dict:
    """``{pull, promote}``: what the studio may send to change `branch`
    (stored with `content` and `etag`), shown with its named `chain`.
    ``pull`` is the PUT of `content` with ``pull: true`` and ``If-Match``
    `etag`, when the branch has a parent and the user may write it;
    ``promote`` has an entry ``{target, apply, propose}`` per ancestor in
    `chain`, nearest first, whose ``apply`` and ``propose`` are the POSTs of
    a push of `branch` into it (``applied``, ``proposed``), each when the
    user may POST it. A published branch has none: changes reach it by a
    push from a working branch."""
    if branch.published:
        return {"pull": None, "promote": []}
    pull = None
    if branch.parent is not None and may_write and etag is not None:
        pull = _action(
            "PUT",
            reverse(f"{UI_BRANCH_SLUG}-detail", args=[branch.pk]),
            {**content, "pull": True},
            f'"{etag}"',
        )
    pushes = reverse(f"{UI_PUSH_SLUG}-list")
    promote = []
    for target in chain[1:]:
        entry = {"target": target.name}
        for name, state in (("apply", "applied"), ("propose", "proposed")):
            body = {
                "source": branch.name,
                "target": target.name,
                "state": state,
                "rbac_context": target.context,
            }
            entry[name] = (
                _action("POST", pushes, body)
                if may_post(request, UI_PUSH_TYPE_URI, body)
                else None
            )
        promote.append(entry)
    return {"pull": pull, "promote": promote}


def _endpoints() -> dict | None:
    """The studio's own endpoints a self-hosted studio calls, from the
    ``scrud-studio`` namespace: ``explain`` and ``options`` take the type's
    slug, so they are templates in which ``{slug}`` stands for it. ``None``
    when the studio's URLs are not included."""

    def slugged(name):
        path = reverse(f"scrud-studio:{name}", args=[_SLUG_PLACEHOLDER])
        return path.replace(f"/{_SLUG_PLACEHOLDER}/", "/{slug}/", 1)

    try:
        return {
            "live_preview": reverse("scrud-studio:live-preview"),
            "explain": slugged("explain"),
            "options": slugged("new-component-options"),
            "new_component": reverse("scrud-studio:new-component"),
        }
    except NoReverseMatch:
        return None


class TypeCatalogViewSet(ComputedResourceViewSet):
    """``ui-type-catalog/all/``: every registered type, as the bootstrap
    studio lists them (`registered_types`: unfiltered)."""

    def compute(self, request, key):
        if key != CATALOG_KEY:
            raise Http404("No such catalog.")
        return {"types": registered_types()}


class SelectableBranchesViewSet(ComputedResourceViewSet):
    """``ui-selectable-branches/mine/``: the branches the user may select
    (`studio_branches`), each with the URL of its resource when they may
    GET it."""

    def compute(self, request, key):
        if key != SELECTABLE_KEY:
            raise Http404("No such list.")
        with _stored_state():
            all_branches = branches()
            found = []
            for choice in studio_branches(request, all_branches):
                branch = all_branches[choice.name]
                href = None
                if branch.pk is not None and may_get(request, branch):
                    href = reverse(f"{UI_BRANCH_SLUG}-detail", args=[branch.pk])
                found.append(
                    {"name": choice.name, "published": choice.published, "href": href}
                )
            return {"branches": found}


class PushPreviewViewSet(ComputedResourceViewSet):
    """``ui-push-previews/<source>/?target=<name>``: what a push of branch
    `source` into branch ``target`` (default: `source`'s parent) would
    change there (`plan_push`, `push_changes`) and the conflicts it would
    hit, without writing. It needs GET on both branches (``main``, which
    everyone may select, needs none, as for a push): an unknown branch and
    one the user may not GET are the same 404, before any 400 for a push the
    server would refuse (`refuse_push_between`) could describe it. Writes
    on items the user may not read are only counted."""

    def compute(self, request, key):
        with _stored_state():
            all_branches = branches()

            def known(name):
                return name in all_branches and _may_get_branch(
                    request, all_branches[name]
                )

            if not known(key):
                raise Http404("No such branch.")
            source = all_branches[key]
            name = request.query_params.get(TARGET_PARAMETER, source.parent)
            if name is not None and not known(name):
                raise Http404("No such branch.")
            # A source with no parent and no target is refused as a push
            # from a root is.
            refuse_push_between(source.name, name or source.name, all_branches)
            try:
                plan = plan_push(
                    request, source.name, name, lock=False, all_branches=all_branches
                )
                changes, unreadable = push_changes(
                    request, plan, name, all_branches=all_branches
                )
            except BranchChainError as error:  # a pinned snapshot is gone
                raise UIBranchInvalidError(str(error)) from error
            return {
                "source": source.name,
                "target": name,
                "changes": changes,
                "unreadable_changes": unreadable,
                "conflicts": plan.conflicts,
                "unreadable_conflicts": plan.hidden_conflicts,
            }


STUDIO_COMPUTED_TYPES = (
    ("UIBranchItems", UI_BRANCH_ITEMS_SLUG, BranchItemsViewSet),
    ("UIBranchStatus", UI_BRANCH_STATUS_SLUG, BranchStatusViewSet),
    ("UITypeCatalog", UI_TYPE_CATALOG_SLUG, TypeCatalogViewSet),
    ("UISelectableBranches", UI_SELECTABLE_BRANCHES_SLUG, SelectableBranchesViewSet),
    ("UIPushPreview", UI_PUSH_PREVIEWS_SLUG, PushPreviewViewSet),
)


def _document(name):
    return (
        importlib.resources.files("scrud_django.ui.studio.documents")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def studio_computed_type_urls():
    """The routes of the studio's computed types (``<slug>/<key>/``), each
    type registered first (`registration.computed_resource_type`): served
    by ``scrud_django.urls`` when ``scrud_django.ui.studio`` is installed."""
    urls = []
    for name, slug, viewset_class in STUDIO_COMPUTED_TYPES:
        urls.extend(
            registration.computed_resource_type(
                TYPE_URI_BASE + name,
                slug,
                schema_func=lambda name=name: _document(f"{name}-schema.json"),
                context_func=lambda name=name: _document(f"{name}-ld.json"),
                viewset_class=viewset_class,
            )
        )
    return urls
