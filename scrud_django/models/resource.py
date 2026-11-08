"""
This will support queries, such as:
    GET /resource_type/schema?uri=http://schema.org/person
    GET /resource_type/context?uri=http://schema.org/person

"""

import contextvars
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
from scrud_django.models.fields import ContextField
from scrud_django.models.resource_type import ResourceType
from scrud_django.scrud_signals import scrud_post_delete

# The ``rbac_context`` column of `ResourceContext` and `ResourceRemoval`
# (`ContextField`): in characters, or in bytes on MySQL and MariaDB.
MAX_CONTEXT_LENGTH = 2048

# The longest context a resource may be stored in, in bytes of UTF-8: its
# `ResourceContext` and `ResourceRemoval` rows must fit PostgreSQL's btree
# indexes (about 2700 bytes an entry), which a context of 2048 characters
# may not, and the ``VARBINARY(2048)`` column on MySQL and MariaDB.
MAX_CONTEXT_BYTES = 2048


def context_fits(context):
    """Whether `context` (a string) is at most `MAX_CONTEXT_BYTES` long in
    UTF-8 (a lone surrogate counts its 3 bytes), so that its rows can be
    stored."""
    return len(context.encode("utf-8", "surrogatepass")) <= MAX_CONTEXT_BYTES


def lock_for_delete(queryset, using, type_uris=None):
    """Lock the `Resource` rows `queryset` selects, on database `using`, in
    primary key order (``select_for_update``), until the caller's
    transaction there ends. A delete takes it before Django's ``Collector``
    runs: the API's locked writes lock a resource's row before its
    ``ResourceContext`` rows (`sync_resource_contexts`, the ``Collector``'s
    cascade), and a delete that took the context rows first deadlocked with
    them on PostgreSQL. Sorted, two deletes of overlapping sets cannot
    deadlock on the resource rows either. Returns the keys it locked, in
    order: a delete must delete those and no others (`delete_locked_keys`),
    since its own query, run again, would also find rows that came to match
    since, without their lock. A `type_uris` dict is given the type URI of
    each row read, by key (read with the keys, for `ResourceQuerySet.delete`'s
    ``scrud_post_delete``).

    The keys are read first, without a lock, and then locked by key, at most
    `DELETE_BATCH_SIZE` a statement: a locking read of `queryset`'s own
    query would lock every row it examines on MySQL and MariaDB (InnoDB), a
    joined ``ResourceType`` row too (``filter(resource_type__slug=...)``),
    which blocks every insert of a resource of that type. A row deleted in
    between is not locked (nor deleted); one that came to match in between
    is left alone, as by a delete that ran a moment earlier.

    When a ``WorkflowPolicy`` resource is among them, the ``WorkflowPolicy``
    type lock (`scrud_django.workflow.lock_workflow_policy_type`) is taken
    first, as the API's writes of a policy take it before its row: deleting
    a policy stamps a ``ResourceRemoval`` of its type, whose foreign key
    check on MySQL and MariaDB waits for that lock, and a delete holding the
    policy's row while it waited deadlocked with an API write of it."""
    from scrud_django.workflow import (
        WORKFLOW_POLICY_TYPE_URI,
        lock_workflow_policy_type,
    )

    rows = queryset.using(using).values_list("pk", "resource_type__type_uri")
    rows.query.select_for_update = False
    rows.query.select_related = False
    rows.query.clear_ordering(force=True)
    found = dict(rows)
    if type_uris is not None:
        type_uris.update(found)
    if WORKFLOW_POLICY_TYPE_URI in found.values():
        lock_workflow_policy_type(using)
    keys = sorted(found)
    locked = []
    for start in range(0, len(keys), DELETE_BATCH_SIZE):
        locked.extend(
            Resource._base_manager.using(using)
            .filter(pk__in=keys[start : start + DELETE_BATCH_SIZE])
            .order_by("pk")
            .select_for_update()
            .values_list("pk", flat=True)
        )
    return locked


# The most keys one ``Collector`` pass of `delete_locked_keys` deletes (a
# query parameter each; SQLite allows 32766 in one statement).
DELETE_BATCH_SIZE = 1000


def delete_locked_keys(queryset, keys, using):
    """Django's ``QuerySet.delete()`` of the rows of `queryset` among `keys`
    (the rows `lock_for_delete` locked for it) on database `using`,
    `DELETE_BATCH_SIZE` at a time; ``(deleted, {label: count})`` over them
    all. The queryset's own filter is applied again, after every lock is
    held: the lock's ``id IN (...)`` can take a row that a write it waited for
    moved out of the filter (PostgreSQL re-checks only the ``IN`` on the new
    row version), and such a row is left alone, as by a delete that ran a
    moment later. Only locked rows can match. Several batches run in a
    transaction (a savepoint inside the caller's) so that an error in a later
    one (a ``ProtectedError``) undoes the earlier ones, as one ``delete()``
    deletes nothing then."""
    rows = queryset.using(using)
    batches = [
        keys[start : start + DELETE_BATCH_SIZE]
        for start in range(0, len(keys), DELETE_BATCH_SIZE)
    ]
    if len(batches) <= 1:
        return models.QuerySet.delete(rows.filter(pk__in=keys))
    total, per_model = 0, {}
    with transaction.atomic(using=using):
        for batch in batches:
            deleted, counts = models.QuerySet.delete(rows.filter(pk__in=batch))
            total += deleted
            for label, count in counts.items():
                per_model[label] = per_model.get(label, 0) + count
    return total, per_model


def in_transaction(using):
    """A block in a transaction on database `using`: the caller's, if it is in
    one (no savepoint, nothing to roll back to on an error the caller catches,
    as Django's own ``delete()`` takes none), else a new one."""
    if transaction.get_connection(using).in_atomic_block:
        return nullcontext()
    return transaction.atomic(using=using)


# ``(database, pairs, deleted)``: the (type, context) pairs a QuerySet.delete()
# of resources on that database has removed so far, stamped once when it ends,
# and the instances it deleted, by primary key, sent scrud_post_delete then
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
        still stamps them one by one. It runs in the caller's transaction, or
        one of its own (`in_transaction`).

        Then, in that transaction, it sends ``scrud_post_delete`` for each
        resource it deleted, in primary key order, as ``Resource.delete()``
        does (`ScrudSignalProcessor` runs their ``on_delete`` actions when it
        commits): ``instance`` is Django's deleted instance, ``pk`` its
        primary key, ``using`` the database, ``resource_type_uri`` its type's
        URI (read with the keys `lock_for_delete` locks, not a query per row)
        and ``action`` ``DELETE``. ``QuerySet.update()``, ``bulk_create()``
        and ``bulk_update()`` send no scrud signal.

        It first locks the rows it deletes, in primary key order
        (`lock_for_delete`), as an API write locks its row first, and then
        deletes those that still match its filter (`delete_locked_keys`): a
        row that came to match after the lock is left alone, as by a delete
        that ran a moment earlier, and so is a row a write the lock waited for
        moved out of the filter.

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
        with in_transaction(db):
            type_uris = {}
            locked = lock_for_delete(self, db, type_uris)
            pending = set()
            deleted = {}
            token = _PENDING_STAMPS.set((db, pending, deleted))
            try:
                result = delete_locked_keys(self, locked, db)
            finally:
                _PENDING_STAMPS.reset(token)
            contexts_by_type = {}
            for type_id, context in pending:
                contexts_by_type.setdefault(type_id, set()).add(context)
            for type_id in sorted(contexts_by_type):
                stamp_removals(type_id, sorted(contexts_by_type[type_id]), db)
            for pk in locked:
                if pk in deleted:
                    scrud_post_delete.send(
                        sender=self.model,
                        instance=deleted[pk],
                        pk=pk,
                        using=db,
                        resource_type_uri=type_uris[pk],
                        action='DELETE',
                    )
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
        that row, and its old contexts must go.

        A ``WorkflowPolicy`` resource takes the ``WorkflowPolicy`` type lock
        first (`lock_workflow_policy_type_for`), as the API's writes of one do."""
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
            if sync:
                lock_workflow_policy_type_for(self, using)
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
    rbac_context = ContextField(max_length=MAX_CONTEXT_LENGTH, db_index=True)

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
    rbac_context = ContextField(max_length=MAX_CONTEXT_LENGTH)
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


def context_rows_of(content):
    """The contexts a resource with `content` has a `ResourceContext` row
    for: `rbac_contexts_of` its content, as a set, without a context too long
    for a row (`context_fits`), which only a resource stored before 0.12.0
    can have (``scrud_django.W003``)."""
    return {c for c in rbac_contexts_of(content) if context_fits(c)}


def find_context_drift(using, batch_size=1000):
    """``(pk, rows, wanted)`` for each `Resource` on database `using`, in
    primary key order, whose `ResourceContext` rows (`rows`, a set) are not
    `context_rows_of` its content (`wanted`): a write that bypassed
    ``Resource.save()`` (a 0.11 process still writing after ``migrate``,
    ``QuerySet.update()``, ``bulk_create()``) left them. It reads every
    resource, `batch_size` at a time."""
    resources = Resource.objects.using(using).order_by("pk")
    last = None
    while True:
        batch = resources if last is None else resources.filter(pk__gt=last)
        batch = list(batch.values_list("pk", "content")[:batch_size])
        if not batch:
            return
        last = batch[-1][0]
        rows = {}
        for pk, context in (
            ResourceContext.objects.using(using)
            .filter(resource_id__in=[pk for pk, _ in batch])
            .values_list("resource_id", "rbac_context")
        ):
            rows.setdefault(pk, set()).add(context)
        for pk, content in batch:
            wanted = context_rows_of(content)
            if rows.get(pk, set()) != wanted:
                yield pk, rows.get(pk, set()), wanted


def resync_contexts(pk, using, *, repair=True):
    """Check the resource `pk` on database `using` for drift again, in a
    transaction of its own with its row locked first, as a write locks it:
    `find_context_drift` reads content and rows in two statements, so a write
    that committed between them looks like drift. Every save holds that lock
    while it syncs the rows, so under it the two agree unless the drift is
    real. Returns ``(rows, wanted)`` if it is (and, with `repair`, makes the
    rows its content's, `sync_resource_contexts`, stamping the contexts it
    left), else ``None`` (in step, or deleted since).

    A repair of a ``WorkflowPolicy`` resource takes the ``WorkflowPolicy``
    type lock first (`scrud_django.workflow.lock_workflow_policy_type`), as
    the API's writes of a policy do: the removal stamp it may insert checks
    its foreign key against that type's row, which on MySQL and MariaDB
    waits for that lock while the policy's row is held."""
    from scrud_django.workflow import (
        WORKFLOW_POLICY_TYPE_URI,
        lock_workflow_policy_type,
    )

    with transaction.atomic(using=using):
        if repair and (
            Resource.objects.using(using)
            .filter(pk=pk)
            .values_list("resource_type__type_uri", flat=True)
            .first()
            == WORKFLOW_POLICY_TYPE_URI
        ):
            lock_workflow_policy_type(using)
        resource = (
            Resource.objects.using(using).select_for_update().filter(pk=pk).first()
        )
        if resource is None:
            return None
        rows = set(
            ResourceContext.objects.using(using)
            .filter(resource_id=pk)
            .values_list("rbac_context", flat=True)
        )
        wanted = context_rows_of(resource.content)
        if rows == wanted:
            return None
        if repair:
            sync_resource_contexts(resource, using)
        return rows, wanted


def sync_resource_contexts(resource, using, *, inserting=False):
    """Make `resource`'s `ResourceContext` rows on database `using` the
    contexts of its content (`context_rows_of`): delete the rows of contexts
    it left, add those of contexts it entered. A resource just inserted
    (`inserting`: a forced insert) has no rows to read."""
    wanted = context_rows_of(resource.content)
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


def stamp_removals(resource_type_id, contexts, using, when=None):
    """Record on database `using`, in the caller's transaction, that a
    resource of the type `resource_type_id` left each of `contexts` at `when`
    (now by default) (`ResourceRemoval`): the stamp of each type and context
    only moves forward. A context too long for a row (`context_fits`: one
    stored before 0.12.0, which has no `ResourceContext` row and is listed to
    superusers only) is stamped as ``SOME_CONTEXT``, which only superusers'
    lists read.

    On MySQL and MariaDB each stamp is one ``INSERT ... ON DUPLICATE KEY
    UPDATE`` (`upsert_removal`): there, of concurrent first stamps of one
    type and context, each INSERT that lost kept a shared lock on the stamp
    after its savepoint rolled back, and their UPDATEs deadlocked. Elsewhere
    a stamp is an UPDATE, else an INSERT in a savepoint (a concurrent first
    stamp's duplicate key falls back to the UPDATE)."""
    when = when or timezone.now()
    rows = ResourceRemoval.objects.using(using)
    connection = transaction.get_connection(using)
    contexts = {c if context_fits(c) else SOME_CONTEXT for c in contexts}
    for context in sorted(contexts):
        if connection.vendor == "mysql":
            upsert_removal(connection, resource_type_id, context, when)
            continue
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


def upsert_removal(connection, resource_type_id, context, when):
    """Stamp `context` of the type `resource_type_id` at `when` on
    `connection` (MySQL or MariaDB), keeping the later of the stored and the
    new ``removed_at``: one ``INSERT ... ON DUPLICATE KEY UPDATE removed_at =
    GREATEST(...)``, which takes the stamp's exclusive lock at once. MySQL
    names the new row with an alias (it deprecates ``VALUES()`` there);
    MariaDB has ``VALUES()`` and no alias."""
    meta = ResourceRemoval._meta
    quote = connection.ops.quote_name
    table = quote(meta.db_table)
    type_column, context_column, when_column = (
        quote(meta.get_field(name).column)
        for name in ("resource_type", "rbac_context", "removed_at")
    )
    if connection.mysql_is_mariadb:
        alias, new = "", f"VALUES({when_column})"
    else:
        alias, new = " AS incoming", f"incoming.{when_column}"
    sql = (
        f"INSERT INTO {table} ({type_column}, {context_column}, {when_column}) "
        f"VALUES (%s, %s, %s){alias} ON DUPLICATE KEY UPDATE "
        f"{when_column} = GREATEST({table}.{when_column}, {new})"
    )
    params = [
        resource_type_id,
        meta.get_field("rbac_context").get_db_prep_save(context, connection),
        meta.get_field("removed_at").get_db_prep_save(when, connection),
    ]
    with connection.cursor() as cursor:
        cursor.execute(sql, params)


def lock_workflow_policy_type_for(resource, using):
    """Take the ``WorkflowPolicy`` type lock
    (`scrud_django.workflow.lock_workflow_policy_type`) on database `using`,
    in the caller's transaction, when `resource` is a ``WorkflowPolicy``,
    before its row is written. The API's writes of a policy take that lock
    before the policy's row; a write that left a context stamps its removal,
    a ``ResourceRemoval`` of the ``WorkflowPolicy`` type, whose foreign key
    check on MySQL and MariaDB waits for that lock: a write that held the
    row first deadlocked with an API write of the policy. The type is the
    one cached on `resource`, else read on `using`."""
    from scrud_django.workflow import (
        WORKFLOW_POLICY_TYPE_URI,
        lock_workflow_policy_type,
    )

    field = Resource.resource_type.field
    if field.is_cached(resource):
        cached = field.get_cached_value(resource)
        type_uri = cached.type_uri if cached is not None else None
    else:
        type_uri = (
            ResourceType.objects.using(using)
            .filter(pk=resource.resource_type_id)
            .values_list("type_uri", flat=True)
            .first()
        )
    if type_uri == WORKFLOW_POLICY_TYPE_URI:
        lock_workflow_policy_type(using)


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
    than a row holds (`check_contexts`), before the row is written. Every raw
    save is a new write of stored content (a reverted version, a restored
    backup): it gets a fresh ``etag`` and ``modified_at``, never the saved
    (older) ones, so that a client revalidating with ``If-Modified-Since``,
    and a list it is the newest resource of, or is back in after a deletion
    (whose removal stamp is older), sees the change. Clients refetch it
    once. A ``WorkflowPolicy`` takes the type lock first
    (`lock_workflow_policy_type_for`), as `Resource.save_base` does."""
    if not raw:
        return
    if _content_saved(update_fields):
        require_transaction(using, "A raw save of a Resource")
        check_contexts(instance.content)
        lock_workflow_policy_type_for(instance, using)
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


@receiver(
    pre_save, sender=ResourceRemoval, dispatch_uid="scrud_django.load_removal_row"
)
def make_room_for_raw_removal(sender, instance, raw, using, **kwargs):
    """A `ResourceRemoval` loaded raw (``loaddata`` into a database that
    already holds stamps, say a staging copy or a partial restore) replaces
    the stamp of its type and context, keeping the later ``removed_at`` (a
    stamp only moves forward), and the row with its key, whose stamp is
    stamped again after it (`restamp_after_raw_removal`): so it never
    collides with a stamp that loading a resource made (`sync_raw_save`
    stamps the contexts it leaves) or that was already there."""
    if not raw:
        return
    require_transaction(using, "Loading a ResourceRemoval")
    same_key = Q(
        resource_type_id=instance.resource_type_id,
        rbac_context=instance.rbac_context,
    )
    rows = ResourceRemoval.objects.using(using).filter(
        same_key if instance.pk is None else same_key | Q(pk=instance.pk)
    )
    displaced = []
    for row in rows:
        if (row.resource_type_id, row.rbac_context) == (
            instance.resource_type_id,
            instance.rbac_context,
        ):
            instance.removed_at = max(instance.removed_at, row.removed_at)
        else:
            displaced.append((row.resource_type_id, row.rbac_context, row.removed_at))
    instance._scrud_displaced = displaced
    rows.delete()


@receiver(
    post_save, sender=ResourceRemoval, dispatch_uid="scrud_django.restamp_removal"
)
def restamp_after_raw_removal(sender, instance, raw, using, **kwargs):
    """After a `ResourceRemoval` is loaded raw, stamp again the stamps whose
    row it displaced (`make_room_for_raw_removal`), at their own time."""
    if not raw:
        return
    for type_id, context, when in getattr(instance, "_scrud_displaced", ()):
        stamp_removals(type_id, [context], using, when=when)


@receiver(post_delete, sender=Resource, dispatch_uid="scrud_django.stamp_deletion")
def stamp_deletion(sender, instance, using, **kwargs):
    """A deleted resource left every context it was in (`stamp_removals`),
    in the deletion's transaction: ``Model.delete()`` and
    `ResourceRegistration` at once, ``Resource.objects`` querysets'
    ``delete()`` (`ResourceQuerySet.delete`) all together when it ends (which
    also takes the instance, its pk not yet cleared, for its
    ``scrud_post_delete``). One in no context stamps ``SOME_CONTEXT``, which
    only superusers' lists read."""
    contexts = rbac_contexts_of(instance.content) or [SOME_CONTEXT]
    deferred = _PENDING_STAMPS.get()
    if deferred is not None and deferred[0] == using:
        # A ResourceQuerySet.delete() on this database stamps them all at
        # its end; a deletion on another database is stamped there now.
        deferred[1].update((instance.resource_type_id, c) for c in contexts)
        deferred[2][instance.pk] = instance
        return
    stamp_removals(instance.resource_type_id, contexts, using)
