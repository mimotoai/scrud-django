"""Whether a user may read a UI item: the one check every reader of item
content asks (the studio's strip, explain and live-preview mark, its
computed types, a push's preview and conflicts)."""

from scrud_django.permissions import may_read_in_context, resource_contexts
from scrud_django.ui.branches import ITEM_KINDS
from scrud_django.ui.branches.model import branch_of
from scrud_django.ui.layers import LAYER_BRANCH_NAME


def may_read_item(request, kind: str, content) -> bool:
    """Whether the user of `request` may GET the item of `kind` (an
    ``ITEM_KINDS`` key) stored with `content`: a packaged layer's item is
    public (spec R1); any other as the per-row read check of its JSON list
    decides (`may_read_in_context` in the item's contexts)."""
    return branch_of(content) == LAYER_BRANCH_NAME or may_read_in_context(
        request, ITEM_KINDS[kind], content, resource_contexts(content)
    )
