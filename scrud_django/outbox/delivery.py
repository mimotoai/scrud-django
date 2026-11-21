"""`send_events`: the delivery of stored `OutboxEvent` rows."""

from django.db import connections, router, transaction
from django.utils import timezone

from scrud_django.outbox.models import OutboxEvent


def send_events(deliver, batch=100) -> int:
    """Deliver up to `batch` unsent events, oldest (lowest ``pk``) first:
    ``deliver(event)`` for each, then its ``sent_at`` is set. Returns how
    many were sent.

    One transaction on the outbox's database (``router.db_for_write``)
    claims the batch with ``select_for_update(skip_locked=True)`` where the
    backend supports it (PostgreSQL, MySQL 8, MariaDB 10.6+), so that
    concurrent senders take different rows; a backend with row locks but no
    ``SKIP LOCKED`` locks them (a second sender waits), and SQLite takes
    none (run one sender at a time there).

    Delivery is at least once: when `deliver` raises, the exception
    propagates and the transaction rolls back, so no event of the batch is
    marked sent, those delivered before it included; the next call delivers
    them again, in the same order. `deliver` must therefore be idempotent
    (key on the event's ``pk``), and an event it always refuses stops the
    events after it until it is fixed or removed."""
    if batch < 1:
        raise ValueError(f"batch must be at least 1, not {batch!r}.")
    using = router.db_for_write(OutboxEvent)
    features = connections[using].features
    with transaction.atomic(using=using):
        events = list(
            OutboxEvent.objects.using(using)
            .select_for_update(skip_locked=features.has_select_for_update_skip_locked)
            .filter(sent_at=None)
            .order_by("pk")[:batch]
        )
        for event in events:
            deliver(event)
            event.sent_at = timezone.now()
            event.save(using=using, update_fields=["sent_at"])
    return len(events)
