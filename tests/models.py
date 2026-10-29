from django.db import models


class Person(models.Model): ...


class Note(models.Model):
    """A model of an adopter's own, served by a ``GenericViewMixin`` view
    (``tests.urls.NoteViewSet``), with an ``rbac_context`` column."""

    text = models.TextField(default="")
    rbac_context = models.CharField(max_length=255, default="")
    etag = models.CharField(max_length=40)
    modified_at = models.DateTimeField()
    # Nullable, so that a queryset may select_related it (an outer join). A
    # model of this unmigrated app: it cannot refer to a migrated one.
    author = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL)
