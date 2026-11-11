"""The live preview's drafts: unsaved versions of items, layered over a branch
for one render (`using_drafts`) and never stored.

A draft is shown only if its save would be accepted (`validate_draft`): the
item's resource view is asked its own checks of the write the studio's save
would make (`writes.save_item`: a create of the branch's override, or an
update of its own row), without making it. So a preview never shows what its
user could not save, and the studio adds no rights.

The after-push preview (`after_push_drafts`) shows a target branch with a
source branch's stored overrides layered on, as the push would write them; it
needs GET on both branches only, since they are stored items, not drafts."""

import logging
from collections.abc import Mapping

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework.exceptions import APIException

from scrud_django.ui.branches import ITEM_KINDS
from scrud_django.ui.branches.effective import Draft, using_drafts
from scrud_django.ui.branches.model import Branch, branch_of, item_key
from scrud_django.ui.branches.sync import _onto
from scrud_django.ui.cascade import item_rows
from scrud_django.ui.dispatch import view_for
from scrud_django.ui.studio.writes import KIND_SLUGS, item_content, own_item

logger = logging.getLogger(__name__)

__all__ = [
    "Draft",
    "after_push_drafts",
    "draft_item",
    "using_drafts",
    "validate_draft",
]


def draft_item(branch: Branch, draft: Draft) -> tuple[dict | None, list[str]]:
    """``(item, [])``: `draft`'s content as the studio's save of it on
    `branch` sends it (`writes.item_content`); or ``(None, messages)`` when
    the draft names no item: an unknown kind, a content that is not an
    object, no name, or a stylesheet's ``applies_to`` that is not a
    context."""
    if draft.kind not in KIND_SLUGS:
        return None, [f"The kind must be one of {', '.join(KIND_SLUGS)}."]
    content = draft.content
    if not isinstance(content, dict):
        return None, ["The content must be an object."]
    name = content.get("name")
    if not isinstance(name, str) or not name:
        return None, ["A name is required."]
    target = content.get("applies_to") if draft.kind == "stylesheet" else ""
    target = "" if target is None else target
    if not isinstance(target, str):
        return None, ["applies_to must be a context."]
    return item_content(draft.kind, name, target, content, branch), []


def validate_draft(request, branch: Branch, draft: Draft) -> list[str]:
    """Why `draft` may not be saved on the working `branch` by the user of
    `request`, as the save's own answers say it (``[]``: it may). The write
    the studio's save would make (a POST of the branch's override when it
    has no row of its own for the item, else a PUT of that row) is asked of
    the item's resource view (`dispatch.view_for`), in the order the view
    runs them, and nothing is written: the request's permission check (the
    body's contexts), the object check of the row an update replaces, the
    item checks (`validate_content` of the ``UI*ViewSet``: name, branch,
    context, template, selectors, text) and the type's JSON Schema. The
    checks a write makes once saved (scoped RBAC's re-check of the stored
    row) and its preconditions (``If-Match``) are not run: nothing is
    saved. A check that fails as a server error (an ``APIException`` of
    status 500 or more) is listed too, and logged at ERROR."""
    item, messages = draft_item(branch, draft)
    if item is None:
        return messages
    target = item.get("applies_to", "")
    own = own_item(draft.kind, branch, item["name"], target)
    slug = KIND_SLUGS[draft.kind]
    if own is None:
        view, internal = view_for(request, slug, "POST", data=item)
    else:
        view, internal = view_for(request, slug, "PUT", pk=own.pk, data=item)
    try:
        view.check_permissions(internal)
        instance = None if own is None else view.get_object()
        body = internal.data
        view.validate_content(body, instance=instance)
        view.get_serializer(data=body, instance=instance).is_valid(raise_exception=True)
    except APIException as error:
        if error.status_code >= 500:
            # No answer about the draft: a check that broke.
            logger.error(
                "scrud-ui: checking the draft %s %r failed",
                draft.kind,
                item["name"],
                exc_info=True,
            )
        return _messages(error.detail)
    except Http404:
        return ["Not found."]
    except DjangoPermissionDenied:
        return ["You do not have permission to perform this action."]
    return []


def _messages(detail) -> list[str]:
    """The messages of an error's `detail` (a message, or lists and
    mappings of them), in order."""
    if isinstance(detail, Mapping):
        return [m for value in detail.values() for m in _messages(value)]
    if isinstance(detail, (list, tuple)):
        return [m for value in detail for m in _messages(value)]
    return [str(detail)]


def after_push_drafts(source: Branch, target: Branch) -> list[Draft]:
    """`source`'s own stored rows of every item kind (its deletion markers
    included), as rows of `target` (`sync._onto`, as a push writes them),
    the lowest pk's for an item stored twice: what a push of `source`
    writes into `target`, as drafts on it. Read with system rights; the
    caller checks GET on both branches."""
    rows = item_rows(ITEM_KINDS.values(), {source.name})
    drafts = []
    for kind, type_uri in ITEM_KINDS.items():
        seen = set()
        for _pk, _etag, content in rows[type_uri]:
            if (
                not isinstance(content, Mapping)
                or not isinstance(content.get("name"), str)
                or branch_of(content) != source.name
            ):
                continue
            key = item_key(kind, content, source)
            if key in seen:
                continue
            seen.add(key)
            drafts.append(
                Draft(kind, _onto(kind, content, key, target, content.get("base")))
            )
    return drafts
