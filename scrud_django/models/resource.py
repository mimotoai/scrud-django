"""
This will support queries, such as:
    GET /resource_type/schema?uri=http://schema.org/person
    GET /resource_type/context?uri=http://schema.org/person

"""

import contextvars

import reversion
from django.db import IntegrityError, models, router, transaction
from django.db.models import Q
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone
from scoped_rbac.rbac_contexts import SOME_CONTEXT

from scrud_django.mixins.resource_mixin import ResourceMixin, rbac_contexts_of
from scrud_django.models.resource_type import ResourceType

# The longest context a resource may be stored in: the ``rbac_context`` column
# of `ResourceContext`.
MAX_CONTEXT_LENGTH = 2048


# The (type, context) pairs a QuerySet.delete() of resources has removed so
# far, stamped once when it ends (`ResourceQuerySet.delete`); None outside one.
_PENDING_STAMPS = contextvars.ContextVar("scrud_pending_stamps", default=None)


class ResourceQuerySet(models.QuerySet):
    def delete(self):
        """``QuerySet.delete()``, stamping the contexts the deleted resources
        left (`stamp_removals`) once per type, in one order (by type, then
        context), after the rows are gone and in the same transaction: the
        per-instance stamps (`stamp_deletion`) would follow the order the
        resources were collected in, and two concurrent bulk deletes taking
        the stamp rows in opposite orders could deadlock."""
        with transaction.atomic(using=self.db, savepoint=False):
            pending = set()
            token = _PENDING_STAMPS.set(pending)
            try:
                result = super().delete()
            finally:
                _PENDING_STAMPS.reset(token)
            contexts_by_type = {}
            for type_id, context in pending:
                contexts_by_type.setdefault(type_id, set()).add(context)
            for type_id in sorted(contexts_by_type):
                stamp_removals(type_id, sorted(contexts_by_type[type_id]), self.db)
        return result

    delete.alters_data = True
    delete.queryset_only = True


@reversion.register()
class Resource(ResourceMixin):
    objects = ResourceQuerySet.as_manager()

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
        resource) comes through here. A raw save (``loaddata``, reversion's
        revert, which call ``Model.save_base`` past this override) is checked
        by `check_raw_save` and synced by `sync_raw_save`.
        ``QuerySet.update()``, ``bulk_create()`` and ``bulk_update()`` are not:
        rows written that way keep stale context rows, which lists filter by.

        The stored rows are always read, except for ``force_insert`` (e.g.
        ``objects.create()``): a new instance given an existing ``pk`` updates
        that row, and its old contexts must go."""
        if raw:
            # check_raw_save (pre_save) and sync_raw_save (post_save).
            return super().save_base(
                *args, raw=raw, using=using, update_fields=update_fields, **kwargs
            )
        using = using or router.db_for_write(type(self), instance=self)
        sync = update_fields is None or "content" in update_fields
        inserting = bool(kwargs.get("force_insert"))
        if sync:
            check_contexts(self.content)
        with transaction.atomic(using=using, savepoint=False):
            super().save_base(
                *args, raw=raw, using=using, update_fields=update_fields, **kwargs
            )
            if sync:
                sync_resource_contexts(self, using, inserting=inserting)


class ResourceContext(models.Model):
    """One RBAC context a `Resource` is in: one row per context of
    ``rbac_contexts_of(resource.content)`` (none for ``"rbac_context": []``),
    as stored, reserved contexts included. Lists filter resources by these rows
    in SQL (`scrud_django.filters.ResourceRbacFilter`). `Resource.save()`
    keeps them in step with the content.

    The content decides: a dump may hold these rows or leave them out
    (``dumpdata scrud_django --exclude scrud_django.ResourceContext``);
    ``loaddata`` rebuilds them from each resource's content either way
    (`sync_raw_save`, `make_room_for_raw_row`, `resync_after_raw_row`)."""

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


class ResourceRemoval(models.Model):
    """When a resource of a type last left a context: deleted, or saved with
    content no longer in it. One row per type and context, its `removed_at`
    the newest such moment (`stamp_removals`). A list's ``Last-Modified``
    takes the newest stamp of the contexts whose every resource the requester
    may list (`scrud_django.filters.readable_removal`), so that a list that
    lost a resource is not "not modified". A deleted resource in no context
    stamps `SOME_CONTEXT` (reserved: superusers' lists only)."""

    resource_type = models.ForeignKey(
        ResourceType, on_delete=models.CASCADE, related_name="removals"
    )
    rbac_context = models.CharField(max_length=MAX_CONTEXT_LENGTH)
    removed_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["resource_type", "rbac_context"],
                name="scrud_django_resourceremoval_unique",
            )
        ]

    def __str__(self):
        return f"{self.resource_type_id}: {self.rbac_context!r} at {self.removed_at}"


def check_contexts(content):
    """Raise ``ValueError`` if a context of `content` is longer than a
    `ResourceContext` row holds (`MAX_CONTEXT_LENGTH`)."""
    for context in rbac_contexts_of(content):
        if len(context) > MAX_CONTEXT_LENGTH:
            raise ValueError(
                f"An rbac_context may be at most {MAX_CONTEXT_LENGTH} characters long."
            )


def sync_resource_contexts(resource, using, *, inserting=False):
    """Make `resource`'s `ResourceContext` rows on database `using` the
    contexts of its content: delete the rows of contexts it left, add those of
    contexts it entered. A resource just inserted (`inserting`: a forced
    insert) has no rows to read."""
    wanted = set(rbac_contexts_of(resource.content))
    rows = ResourceContext.objects.using(using)
    existing = (
        set()
        if inserting
        else set(rows.filter(resource=resource).values_list("rbac_context", flat=True))
    )
    stale = existing - wanted
    if stale:
        rows.filter(resource=resource, rbac_context__in=stale).delete()
        stamp_removals(resource.resource_type_id, stale, using)
    missing = wanted - existing
    if missing:
        rows.bulk_create(
            [
                ResourceContext(resource=resource, rbac_context=context)
                for context in sorted(missing)
            ]
        )


def stamp_removals(resource_type_id, contexts, using):
    """Record on database `using`, in the caller's transaction, that a
    resource of the type `resource_type_id` left each of `contexts` now
    (`ResourceRemoval`): the stamp of each type and context only moves
    forward."""
    when = timezone.now()
    rows = ResourceRemoval.objects.using(using)
    for context in sorted(set(contexts)):
        key = {"resource_type_id": resource_type_id, "rbac_context": context}
        if rows.filter(**key, removed_at__lt=when).update(removed_at=when):
            continue
        if rows.filter(**key).exists():
            continue
        try:
            with transaction.atomic(using=using):
                rows.create(removed_at=when, **key)
        except IntegrityError:
            # A concurrent removal created it first.
            rows.filter(**key, removed_at__lt=when).update(removed_at=when)


def require_transaction(using, what):
    """Raise ``TransactionManagementError`` unless database `using` is inside
    a transaction: a raw save's rows are synced by a signal after the row is
    written, which commits them together only inside one."""
    if not transaction.get_connection(using).in_atomic_block:
        raise transaction.TransactionManagementError(
            f"{what} must run inside transaction.atomic(using={using!r}), so that "
            "the resource's ResourceContext rows commit with it (reversion's "
            "Revision.revert() and loaddata do; wrap Version.revert() yourself)."
        )


def _content_saved(update_fields):
    return update_fields is None or "content" in update_fields


@receiver(pre_save, sender=Resource, dispatch_uid="scrud_django.check_raw_save")
def check_raw_save(sender, instance, raw, using, update_fields, **kwargs):
    """Refuse a raw save of a resource (``loaddata``, reversion's revert)
    outside a transaction (`require_transaction`), or with a context longer
    than a row holds (`check_contexts`), before the row is written."""
    if raw and _content_saved(update_fields):
        require_transaction(using, "A raw save of a Resource")
        check_contexts(instance.content)


@receiver(post_save, sender=Resource, dispatch_uid="scrud_django.sync_raw_save")
def sync_raw_save(sender, instance, raw, using, update_fields, **kwargs):
    """Keep the `ResourceContext` rows of a resource saved raw
    (``Model.save_base(raw=True)``: ``loaddata``, reversion's revert) in step,
    on the database of the save, in the transaction `check_raw_save`
    required. Other saves are synced by `Resource.save_base`."""
    if raw and _content_saved(update_fields):
        sync_resource_contexts(instance, using)


@receiver(
    pre_save, sender=ResourceContext, dispatch_uid="scrud_django.load_context_row"
)
def make_room_for_raw_row(sender, instance, raw, using, **kwargs):
    """A `ResourceContext` loaded raw (``loaddata`` of a dump that holds the
    rows) replaces the row with its key and the row of its resource and
    context, so it never collides with the rows `sync_raw_save` made for the
    resources loaded before it. The resources whose rows it displaced are
    synced again after it (`resync_after_raw_row`)."""
    if not raw:
        return
    require_transaction(using, "Loading a ResourceContext")
    rows = ResourceContext.objects.using(using).filter(
        Q(pk=instance.pk)
        | Q(resource_id=instance.resource_id, rbac_context=instance.rbac_context)
    )
    instance._scrud_displaced = set(rows.values_list("resource_id", flat=True))
    rows.delete()


@receiver(
    post_save, sender=ResourceContext, dispatch_uid="scrud_django.resync_context_row"
)
def resync_after_raw_row(sender, instance, raw, using, **kwargs):
    """After a `ResourceContext` is loaded raw, make the rows of its resource,
    and of any resource whose row it displaced, the contexts of their content
    again: the content decides, whatever the dump's rows say. A resource not
    loaded yet is synced when it is (`sync_raw_save`)."""
    if not raw:
        return
    resource_ids = {instance.resource_id, *getattr(instance, "_scrud_displaced", ())}
    for resource in Resource.objects.using(using).filter(pk__in=resource_ids):
        sync_resource_contexts(resource, using)


@receiver(post_delete, sender=Resource, dispatch_uid="scrud_django.stamp_deletion")
def stamp_deletion(sender, instance, using, **kwargs):
    """A deleted resource left every context it was in (`stamp_removals`),
    in the deletion's transaction: ``Model.delete()`` and
    `ResourceRegistration` at once, ``Resource.objects`` querysets'
    ``delete()`` (`ResourceQuerySet.delete`) all together when it ends. One
    in no context stamps ``SOME_CONTEXT``, which only superusers' lists
    read."""
    contexts = rbac_contexts_of(instance.content) or [SOME_CONTEXT]
    pending = _PENDING_STAMPS.get()
    if pending is not None:
        # A ResourceQuerySet.delete() stamps them all at its end.
        pending.update((instance.resource_type_id, c) for c in contexts)
        return
    stamp_removals(instance.resource_type_id, contexts, using)
