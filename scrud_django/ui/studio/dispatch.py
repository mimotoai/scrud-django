"""The studio's in-process requests to the resource views: `call_view`,
which lives in `scrud_django.ui.dispatch` (embeds use it too) and is
re-exported here."""

from scrud_django.ui.dispatch import SAFE_METHODS, call_view

__all__ = ["SAFE_METHODS", "call_view"]
