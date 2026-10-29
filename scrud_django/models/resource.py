"""
This will support queries, such as:
    GET /resource_type/schema?uri=http://schema.org/person
    GET /resource_type/context?uri=http://schema.org/person

"""

import reversion
from django.db import models, router, transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from scrud_django.mixins.resource_mixin import ResourceMixin, rbac_contexts_of
from scrud_django.models.resource_type import ResourceType

# The longest context a resource may be stored in: the ``rbac_context`` column
# of `ResourceContext`.
MAX_CONTEXT_LENGTH = 2048


@reversion.register()
class Resource(ResourceMixin):
    class Meta:
        ordering = ["id"]

    resource_type = models.ForeignKey(
        to=ResourceType,
        on_delete=models.PROTECT,
        verbose_name='resource type',
        related_name='resource_type',
    )

    def save_base(self, *args, raw=False, using=None, update_fields=None, **kwargs):
        """Save the row and, in the same transaction on the same database, its
        `ResourceContext` rows (`sync_resource_contexts`), unless
        `update_fields` leaves ``content`` out. ``Model.save()`` (and so
        `ResourceRegistration`, the admin and any server code that saves a
        resource) comes through here; a raw save (``loaddata``, reversion's
        revert, which call ``Model.save_base`` past this override) is synced by
        `sync_raw_save`. ``QuerySet.update()``, ``bulk_create()`` and
        ``bulk_update()`` are not: rows written that way keep stale context
        rows, which lists filter by."""
        if raw:
            # sync_raw_save syncs it (post_save).
            return super().save_base(
                *args, raw=raw, using=using, update_fields=update_fields, **kwargs
            )
        using = using or router.db_for_write(type(self), instance=self)
        sync = update_fields is None or "content" in update_fields
        adding = self._state.adding
        if sync:
            check_contexts(self.content)
        with transaction.atomic(using=using, savepoint=False):
            super().save_base(
                *args, raw=raw, using=using, update_fields=update_fields, **kwargs
            )
            if sync:
                sync_resource_contexts(self, using, adding=adding)


class ResourceContext(models.Model):
    """One RBAC context a `Resource` is in: one row per context of
    ``rbac_contexts_of(resource.content)`` (none for ``"rbac_context": []``),
    as stored, reserved contexts included. Lists filter resources by these rows
    in SQL (`scrud_django.filters.ResourceRbacFilter`). `Resource.save()`
    keeps them in step with the content."""

    resource = models.ForeignKey(
        Resource, on_delete=models.CASCADE, related_name="rbac_context_rows"
    )
    rbac_context = models.CharField(max_length=MAX_CONTEXT_LENGTH, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["resource", "rbac_context"],
                name="scrud_django_resourcecontext_unique",
            )
        ]

    def __str__(self):
        return f"{self.resource_id}: {self.rbac_context!r}"


def check_contexts(content):
    """Raise ``ValueError`` if a context of `content` is longer than a
    `ResourceContext` row holds (`MAX_CONTEXT_LENGTH`)."""
    for context in rbac_contexts_of(content):
        if len(context) > MAX_CONTEXT_LENGTH:
            raise ValueError(
                f"An rbac_context may be at most {MAX_CONTEXT_LENGTH} characters long."
            )


def sync_resource_contexts(resource, using, *, adding=False):
    """Make `resource`'s `ResourceContext` rows on database `using` the
    contexts of its content: delete the rows of contexts it left, add those of
    contexts it entered. A resource just added has no rows to read."""
    wanted = set(rbac_contexts_of(resource.content))
    rows = ResourceContext.objects.using(using)
    existing = (
        set()
        if adding
        else set(rows.filter(resource=resource).values_list("rbac_context", flat=True))
    )
    stale = existing - wanted
    if stale:
        rows.filter(resource=resource, rbac_context__in=stale).delete()
    missing = wanted - existing
    if missing:
        rows.bulk_create(
            [
                ResourceContext(resource=resource, rbac_context=context)
                for context in sorted(missing)
            ]
        )


@receiver(post_save, sender=Resource, dispatch_uid="scrud_django.sync_raw_save")
def sync_raw_save(sender, instance, raw, using, update_fields, **kwargs):
    """Keep the `ResourceContext` rows of a resource saved raw
    (``Model.save_base(raw=True)``: ``loaddata``, reversion's revert) in step,
    on the database of the save. Other saves are synced by
    `Resource.save_base`."""
    if raw and (update_fields is None or "content" in update_fields):
        sync_resource_contexts(instance, using)
