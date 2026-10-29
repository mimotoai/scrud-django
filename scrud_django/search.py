"""haystack integration (the ``search`` extra).

`OnCommitSignalProcessor` updates the search index when a write commits, so a
write rolled back (a post-save 403, any error) leaves the index as it was.
Set ``HAYSTACK_SIGNAL_PROCESSOR = "scrud_django.search.OnCommitSignalProcessor"``.
"""

from django.db import models, transaction
from haystack.exceptions import NotHandled
from haystack.signals import BaseSignalProcessor
from haystack.utils import get_identifier


class OnCommitSignalProcessor(BaseSignalProcessor):
    """haystack's ``RealtimeSignalProcessor``, deferred: a saved or deleted
    instance of an indexed model is updated in, or removed from, the index
    once its write commits (``transaction.on_commit`` on the database of the
    save), or at once outside a transaction. A deleted instance is named
    (``get_identifier``) when the delete happens, while it still has its pk.
    An index error is logged (``robust``), as the write has committed."""

    def setup(self):
        models.signals.post_save.connect(self.enqueue_save)
        models.signals.post_delete.connect(self.enqueue_delete)

    def teardown(self):
        models.signals.post_save.disconnect(self.enqueue_save)
        models.signals.post_delete.disconnect(self.enqueue_delete)

    def indexes_for(self, sender, instance):
        """``(backend alias, index)`` of each search backend that indexes
        `sender`."""
        found = []
        for using in self.connection_router.for_write(instance=instance):
            try:
                index = self.connections[using].get_unified_index().get_index(sender)
            except NotHandled:
                continue
            found.append((using, index))
        return found

    def enqueue_save(self, sender, instance, using=None, **kwargs):
        if not self.indexes_for(sender, instance):
            return

        def update():
            for backend, index in self.indexes_for(sender, instance):
                index.update_object(instance, using=backend)

        transaction.on_commit(update, using=using, robust=True)

    def enqueue_delete(self, sender, instance, using=None, **kwargs):
        if not self.indexes_for(sender, instance):
            return
        identifier = get_identifier(instance)

        def remove():
            for backend, index in self.indexes_for(sender, instance):
                index.remove_object(identifier, using=backend)

        transaction.on_commit(remove, using=using, robust=True)
