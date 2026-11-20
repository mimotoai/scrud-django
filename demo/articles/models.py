from django.db import models


class Article(models.Model):
    """A model of the demo's own, governed by a two-state workflow
    (`articles.workflow`) through ``scrud_django.ninja.workflow_check``
    (`articles.api`)."""

    title = models.CharField(max_length=200, default="")
    state = models.CharField(max_length=20, default="draft")
    rbac_context = models.CharField(max_length=255, default="")
