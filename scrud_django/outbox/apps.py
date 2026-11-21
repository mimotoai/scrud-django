from django.apps import AppConfig


class OutboxConfig(AppConfig):
    name = "scrud_django.outbox"
    label = "scrud_django_outbox"
    verbose_name = "scrud outbox"
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        """Connect `scrud_django.outbox.receivers.store_event` to
        ``scrud_post_save`` and ``scrud_post_delete`` (one ``dispatch_uid``,
        so that a second ``ready()`` does not record twice), and register the
        ``SCRUD_OUTBOX`` check (`scrud_django.outbox.checks`)."""
        from scrud_django.core.signals import scrud_post_delete, scrud_post_save
        from scrud_django.outbox import checks  # noqa: F401 (registers its check)
        from scrud_django.outbox.receivers import store_event

        for signal in (scrud_post_save, scrud_post_delete):
            signal.connect(store_event, dispatch_uid="scrud_django.outbox")
