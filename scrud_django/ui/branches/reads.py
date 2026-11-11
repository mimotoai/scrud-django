"""Whether a user may read a UI item: the one check every reader of item
content asks (the studio's strip, explain and live-preview mark, its
computed types and new-component action, a push's preview and
conflicts)."""

from scrud_django.permissions import may_read_in_context, resource_contexts
from scrud_django.ui.branches import ITEM_KINDS
from scrud_django.ui.layers import is_layer_row


def may_read_item(request, kind: str, pk, content) -> bool:
    """Whether the user of `request` may GET the item of `kind` (an
    ``ITEM_KINDS`` key) with `content`, stored as row `pk` (``None`` for a
    pinned snapshot's or a packaged layer's): a packaged layer's item
    (`is_layer_row`) is public (spec R1); any other as the per-row read
    check of its JSON list decides (`may_read_in_context` in the item's
    contexts)."""
    return is_layer_row(pk, content) or may_read_in_context(
        request, ITEM_KINDS[kind], content, resource_contexts(content)
    )
