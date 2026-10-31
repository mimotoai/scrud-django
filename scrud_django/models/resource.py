"""
This will support queries, such as:
    GET /resource_type/schema?uri=http://schema.org/person
    GET /resource_type/context?uri=http://schema.org/person

"""

import contextvars
import functools
from contextlib import nullcontext
from uuid import uuid4

import reversion
from django.db import IntegrityError, models, router, transaction
from django.db.models import Q
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone
from scoped_rbac.rbac_contexts import SOME_CONTEXT

from scrud_django.mixins.resource_mixin import ResourceMixin, rbac_contexts_of
from scrud_django.models.resource_type import ResourceType

# The ``rbac_context`` column of `ResourceContext` and `ResourceRemoval`, in
# characters.
MAX_CONTEXT_LENGTH = 2048

# The longest context a resource may be stored in, in bytes of UTF-8: its
# `ResourceContext` and `ResourceRemoval` rows must fit PostgreSQL's btree
# indexes (about 2700 bytes an entry), which a context of 2048 characters
# may not.
MAX_CONTEXT_BYTES = 2048


def context_fits(context):
    """Whether `context` (a string) is at most `MAX_CONTEXT_BYTES` long in
    UTF-8 (a lone surrogate counts its 3 bytes), so that its rows can be
    stored."""
    return len(context.encode("utf-8", "surrogatepass")) <= MAX_CONTEXT_BYTES


def lock_for_delete(queryset, using):
    """Lock the `Resource` rows `queryset` selects, on database `using`, in
    primary key order (``select_for_update``), until the caller's
    transaction there ends. A delete takes it before Django's ``Collector``
    runs: the API's locked writes lock a resource's row before its
    ``ResourceContext`` rows (`sync_resource_contexts`, the ``Collector``'s
    cascade), and a delete that took the context rows first deadlocked with
    them on PostgreSQL. Sorted, two deletes of overlapping sets cannot
    deadlock on the resource rows either."""
    keys = queryset.using(using).values("pk")
    keys.query.select_for_update = False
    keys.query.select_related = False
    keys.query.clear_ordering(force=True)
    list(
        Resource._base_manager.using(using)
        .filter(pk__in=keys)
        .order_by("pk")
        .select_for_update()
        .values_list("pk", flat=True)
    )


def in_transaction(using):
    """A block in a transaction on database `using`: the caller's, if it is in
    one (no savepoint, nothing to roll back to on an error the caller catches,
    as Django's own ``delete()`` takes none), else a new one."""
    if transaction.get_connection(using).in_atomic_block:
        return nullcontext()
    return transaction.atomic(using=using)


# ``(database, pairs)``: the (type, context) pairs a QuerySet.delete() of
# resources on that database has removed so far, stamped once when it ends
# (`ResourceQuerySet.delete`); None outside one.
_PENDING_STAMPS = contextvars.ContextVar("scrud_pending_stamps", default=None)


class ResourceQuerySet(models.QuerySet):
    def delete(self):
        """``QuerySet.delete()``, stamping the contexts the deleted resources
        left (`stamp_removals`) once per type, in one order (by type, then
        context), after the rows are gone and in the same transaction: the
        per-instance stamps (`stamp_deletion`) would follow the order the
        resources were collected in, and two concurrent bulk deletes taking
        the stamp rows in opposite orders could deadlock. It is the base
        manager's queryset too (``Meta.base_manager_name``), so Django's own
        bulk paths use it. A deletion on another database while it runs (a
        signal receiver's) is stamped there at once. Only a deletion of many
        resources through a ``Collector`` built by hand, outside a queryset,
        still stamps them one by one.

        It first locks the rows it deletes, in primary key order
        (`lock_for_delete`), as an API write locks its row first.

        It runs on the database Django's ``delete()`` deletes on: the
        queryset's ``using()`` alias, else the router's ``db_for_write`` (not
        ``self.db``, which is ``db_for_read`` there: a read replica)."""
        db = self._db or router.db_for_write(self.model, **self._hints)
        if (
            self.query.is_sliced
            or self.query.distinct_fields
            or self.query.combinator
            or self._fields is not None
        ):
            return super().delete()  # Django refuses these: TypeError.
        with transaction.atomic(using=db, savepoint=False):
            lock_for_delete(self, db)
            pending = set()
            token = _PENDING_STAMPS.set((db, pending))
            try:
                result = super().delete()
            finally:
                _PENDING_STAMPS.reset(token)
            contexts_by_type = {}
            for type_id, context in pending:
                contexts_by_type.setdefault(type_id, set()).add(context)
            for type_id in sorted(contexts_by_type):
                stamp_removals(type_id, sorted(contexts_by_type[type_id]), db)
        return result

    delete.alters_data = True
    delete.queryset_only = True


@reversion.register()
class Resource(ResourceMixin):
    # The base manager too, which Django's own deletion paths (cascades,
    # related managers) and Resource._base_manager use: every queryset
    # delete() of resources stamps once, in one order.
    objects = ResourceQuerySet.as_manager()

    class Meta:
        ordering = ["id"]
        base_manager_name = "objects"

    resource_type = models.ForeignKey(
        to=ResourceType,
        on_delete=models.PROTECT,
        verbose_name='resource type',
        related_name='resource_type',
    )

    def delete(self, using=None, keep_parents=False):
        """``Model.delete()``, with the row locked first (`lock_for_delete`),
        in the caller's transaction or one of its own: the admin's delete,
        and server code's, take the locks in the order the API's locked
        writes do."""
        if self.pk is None:
            return super().delete(using=using, keep_parents=keep_parents)
        db = using or router.db_for_write(type(self), instance=self)
        with in_transaction(db):
            lock_for_delete(Resource._base_manager.filter(pk=self.pk), db)
            return super().delete(using=using, keep_parents=keep_parents)

    delete.alters_data = True

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
    """Raise ``ValueError`` if a context of `content` is longer than its rows
    can hold (`context_fits`: `MAX_CONTEXT_BYTES` of UTF-8)."""
    for context in rbac_contexts_of(content):
        if not context_fits(context):
            raise ValueError(
                f"An rbac_context may be at most {MAX_CONTEXT_BYTES} bytes long "
                "in UTF-8."
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
    forward. A context too long for a row (`context_fits`: one stored before
    0.12.0, which has no `ResourceContext` row and is listed to superusers
    only) is stamped as ``SOME_CONTEXT``, which only superusers' lists
    read."""
    when = timezone.now()
    rows = ResourceRemoval.objects.using(using)
    contexts = {c if context_fits(c) else SOME_CONTEXT for c in contexts}
    for context in sorted(contexts):
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


# True while reversion reverts a version (`Version.revert`, which
# `Revision.revert` and the admin's revert and recover call): see
# `mark_reversion_reverts`.
_REVERTING = contextvars.ContextVar("scrud_reverting", default=False)


def mark_reversion_reverts():
    """Wrap reversion's ``Version.revert`` so that a raw save of a `Resource`
    knows it is a revert (`_REVERTING`): `check_raw_save` then gives it a
    fresh ``etag`` and ``modified_at``. reversion saves the version as
    ``loaddata`` saves a fixture (``Model.save_base(raw=True)``), with the
    version's validators. Installed once, by the app's ``ready()``."""
    from reversion.models import Version

    original = Version.revert
    if getattr(original, "_scrud_marks_reverts", False):
        return

    @functools.wraps(original)
    def revert(self, *args, **kwargs):
        token = _REVERTING.set(True)
        try:
            return original(self, *args, **kwargs)
        finally:
            _REVERTING.reset(token)

    revert._scrud_marks_reverts = True
    Version.revert = revert


def _content_saved(update_fields):
    return update_fields is None or "content" in update_fields


@receiver(pre_save, sender=Resource, dispatch_uid="scrud_django.check_raw_save")
def check_raw_save(sender, instance, raw, using, update_fields, **kwargs):
    """Refuse a raw save of a resource (``loaddata``, reversion's revert)
    outside a transaction (`require_transaction`), or with a context longer
    than a row holds (`check_contexts`), before the row is written. A revert
    (`mark_reversion_reverts`) is a new write of old content: it gets a fresh
    ``etag`` and ``modified_at``, never the version's (older) ones, so that a
    client revalidating with ``If-Modified-Since``, and a list it is the
    newest resource of, or is back in, sees the change. ``loaddata`` keeps the
    dump's."""
    if not raw:
        return
    if _content_saved(update_fields):
        require_transaction(using, "A raw save of a Resource")
        check_contexts(instance.content)
    if _REVERTING.get():
        instance.etag = uuid4().hex
        instance.modified_at = timezone.now()


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
    deferred = _PENDING_STAMPS.get()
    if deferred is not None and deferred[0] == using:
        # A ResourceQuerySet.delete() on this database stamps them all at
        # its end; a deletion on another database is stamped there now.
        deferred[1].update((instance.resource_type_id, c) for c in contexts)
        return
    stamp_removals(instance.resource_type_id, contexts, using)
