"""The opt-in transactional outbox (#20): add ``"scrud_django.outbox"`` to
``INSTALLED_APPS`` and list the types to record in ``SCRUD_OUTBOX["TYPES"]``
(type URIs, or ``"__all__"``). Each ``scrud_post_save`` and
``scrud_post_delete`` of a listed type stores one `OutboxEvent`
(`scrud_django.outbox.models`) in the write's transaction, on its database
(`scrud_django.outbox.receivers.store_event`); `send_events` and
``manage.py scrud_outbox_send`` (with ``SCRUD_OUTBOX["DELIVER"]``, the dotted
path of ``deliver(event)``) deliver them, at least once.

The app imports nothing from DRF. `send_events` is imported lazily, so that
importing this package (as ``INSTALLED_APPS`` does) loads no model."""

__all__ = ["send_events"]


def __getattr__(name):
    if name == "send_events":
        from scrud_django.outbox.delivery import send_events

        return send_events
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
