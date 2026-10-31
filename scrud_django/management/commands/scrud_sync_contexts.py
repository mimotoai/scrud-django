"""``manage.py scrud_sync_contexts``: rebuild ``ResourceContext`` rows that
disagree with their resource's content."""

from django.core.management.base import BaseCommand, CommandError
from django.db import router

from scrud_django.models import Resource
from scrud_django.models.resource import find_context_drift, resync_contexts


class Command(BaseCommand):
    help = (
        "Compare every resource's ResourceContext rows (which lists filter by) "
        "with the contexts its content names, and re-sync those that differ: a "
        "write that bypassed Resource.save() (a 0.11 process still writing "
        "after migrate, QuerySet.update(), bulk_create()) leaves them stale. "
        "It reads every resource. A resource that differs is read again, and "
        "repaired, in its own transaction with its row locked, so it may run "
        "while the API serves and reports only drift that holds under the lock. "
        "Safe to run again: a resource in step is left alone."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Report the resources whose rows differ and change nothing; "
            "exit with status 1 if there are any.",
        )
        parser.add_argument(
            "--database",
            default=None,
            help="The database to check and repair (default: the one the router "
            "gives for Resource writes).",
        )

    def handle(self, *args, check=False, database=None, **options):
        using = database or router.db_for_write(Resource)
        verbosity = options.get("verbosity", 1)
        drifted = []
        for pk, _rows, _wanted in find_context_drift(using):
            # Read again under the row lock: the scan's two reads can see a
            # write half done; a resource in step (or deleted) since is skipped.
            confirmed = resync_contexts(pk, using, repair=not check)
            if confirmed is None:
                continue
            rows, wanted = confirmed
            drifted.append(pk)
            if verbosity >= 1:
                self.stdout.write(
                    f"pk {pk}: rows {sorted(rows)!r}, content {sorted(wanted)!r}"
                )
        if not drifted:
            if verbosity >= 1:
                self.stdout.write(
                    f"Every resource's ResourceContext rows on database {using!r} "
                    "are in step with its content."
                )
            return
        if check:
            raise CommandError(
                f"{len(drifted)} resources on database {using!r} have "
                "ResourceContext rows that disagree with their content; run "
                "manage.py scrud_sync_contexts without --check to re-sync them."
            )
        if verbosity >= 1:
            self.stdout.write(
                f"Re-synced {len(drifted)} resources on database {using!r}."
            )
