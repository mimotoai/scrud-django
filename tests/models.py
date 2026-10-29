from django.db import models


class Person(models.Model): ...


class Note(models.Model):
    """A model of an adopter's own, served by a ``GenericViewMixin`` view
    (``tests.urls.NoteViewSet``), with an ``rbac_context`` column."""

    text = models.TextField(default="")
    rbac_context = models.CharField(max_length=255, default="")
    etag = models.CharField(max_length=40)
    modified_at = models.DateTimeField()
