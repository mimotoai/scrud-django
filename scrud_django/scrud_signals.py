"""Moved to scrud_django.core.signals; kept as an alias."""

from scrud_django.core.signals import (
    ScrudSignalProcessor,
    WorkflowActionCall,
    action_name,
    logger,
    scrud_post_delete,
    scrud_post_save,
    write_alias,
)

__all__ = [
    "ScrudSignalProcessor",
    "WorkflowActionCall",
    "action_name",
    "logger",
    "scrud_post_delete",
    "scrud_post_save",
    "write_alias",
]
