from collections.abc import Mapping

from django.core.management.base import BaseCommand, CommandError
from django.utils.module_loading import import_string

from scrud_django.outbox.conf import outbox_settings
from scrud_django.outbox.delivery import send_events


class Command(BaseCommand):
    help = (
        "Deliver up to --batch unsent outbox events, oldest first, with "
        'SCRUD_OUTBOX["DELIVER"] (the dotted path of deliver(event)), and print '
        "how many were sent. Run it again (from cron or a loop) to send more."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch",
            type=int,
            default=100,
            help="The most events to send (default 100).",
        )

    def handle(self, *args, batch, **options):
        settings = outbox_settings()
        path = settings.get("DELIVER") if isinstance(settings, Mapping) else None
        if not path:
            raise CommandError(
                'Set SCRUD_OUTBOX["DELIVER"] to the dotted path of deliver(event).'
            )
        try:
            deliver = import_string(path)
        except ImportError as error:
            raise CommandError(
                f'SCRUD_OUTBOX["DELIVER"] ({path!r}) cannot be imported: {error}'
            ) from error
        if batch < 1:
            raise CommandError(f"--batch must be at least 1, not {batch}.")
        sent = send_events(deliver, batch=batch)
        self.stdout.write(f"Sent {sent} outbox event{'' if sent == 1 else 's'}.")
