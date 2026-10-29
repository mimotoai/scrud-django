from django.db import router, transaction
from django.dispatch import Signal

from .conf import workflow_actions

# Sent by ``ResourceMixin.save()`` / ``delete()`` inside the write, before it
# commits (and before the API's post-save check, which may still roll it
# back): a receiver may read or change the database in the write's
# transaction. Work that leaves the database belongs in
# ``transaction.on_commit``, as `ScrudSignalProcessor` runs the configured
# workflow actions.
scrud_post_save = Signal()
scrud_post_delete = Signal()


def write_alias(sender, kwargs):
    """The database a ``scrud_post_save`` / ``scrud_post_delete`` write went
    to: the signal's ``using``, else the instance's, else the router's."""
    instance = kwargs.get("instance")
    state = getattr(instance, "_state", None)
    return (
        kwargs.get("using")
        or getattr(state, "db", None)
        or router.db_for_write(sender, instance=instance)
    )


class ScrudSignalProcessor:
    """Runs the ``SCRUD_WORKFLOW_ACTIONS`` of a resource's type on
    ``scrud_post_save`` (``on_save``) and ``scrud_post_delete``
    (``on_delete``), once the write commits (``transaction.on_commit`` on its
    database): a write rolled back (a post-save 403, any error) runs none of
    them, and outside a transaction they run at once. An action that raises
    is logged (``robust``), as the write has committed."""

    workflow_actions = {}
    post_save_literal = 'on_save'
    post_delete_literal = 'on_delete'
    resource_type_uri_literal = 'resource_type_uri'

    def __init__(self) -> None:
        scrud_post_save.connect(self.post_save_trigger)
        scrud_post_delete.connect(self.post_delete_trigger)
        self.workflow_actions = workflow_actions()

    def post_save_trigger(self, sender, **kwargs):
        action = self.determine_action(
            self.post_save_literal, kwargs.get(self.resource_type_uri_literal)
        )
        if action:
            self.run_on_commit(action, sender, kwargs)

    def post_delete_trigger(self, sender, **kwargs):
        action = self.determine_action(
            self.post_delete_literal, kwargs.get(self.resource_type_uri_literal)
        )
        if action:
            self.run_on_commit(action, sender, kwargs)

    def run_on_commit(self, action, sender, kwargs):
        """Call ``action(sender, **kwargs)`` when the write's transaction
        commits."""
        transaction.on_commit(
            lambda: action(sender, **kwargs),
            using=write_alias(sender, kwargs),
            robust=True,
        )

    def determine_action(self, post_literal, type_uri):
        actions = self.workflow_actions.get(type_uri, None)
        if actions is not None:
            if post_literal in actions:
                return actions[post_literal]
        return None
