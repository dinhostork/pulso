"""Delete FeedImpressions received before a fixed retention cutoff (#43).

Nothing schedules this command: retention holds only while an operator runs it.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from reading.application.impressions import (
    MAX_PRUNE_BATCH,
    prune_feed_impressions,
    retention_cutoff,
)

DEFAULT_BATCH_SIZE = 1000


class Command(BaseCommand):
    help = (
        "Count FeedImpression rows past retention; with --apply, delete them in "
        "bounded batches. Dry run by default."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete eligible rows. Without it, nothing is deleted.",
        )
        parser.add_argument(
            "--days",
            type=int,
            default=settings.READING_IMPRESSION_RETENTION_DAYS,
            help=(
                "Retention in days, measured from received_at "
                f"(default: {settings.READING_IMPRESSION_RETENTION_DAYS})."
            ),
        )
        parser.add_argument(
            "--before",
            help=(
                "Fixed ISO-8601 cutoff printed by an earlier run, so a dry run can be "
                "applied or an interrupted run resumed on the same rows. It may not "
                "be later than the --days cutoff."
            ),
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=f"Rows deleted per transaction, 1-{MAX_PRUNE_BATCH} (default: {DEFAULT_BATCH_SIZE}).",
        )
        parser.add_argument(
            "--max-batches",
            type=int,
            help="Stop after this many batches; re-run with the same --before to continue.",
        )

    def handle(self, *args, **options):
        try:
            limit = retention_cutoff(days=options["days"])
        except ValueError:
            raise CommandError("--days must be a positive integer") from None
        cutoff = limit
        if options["before"] is not None:
            try:
                cutoff = parse_datetime(options["before"])
            except ValueError:
                cutoff = None
            if cutoff is None or cutoff.tzinfo is None:
                raise CommandError("--before must be an ISO-8601 timestamp with a time zone")
            if cutoff > limit:
                raise CommandError(
                    "--before is inside the retention window; it may not be later than "
                    f"{limit.isoformat()}"
                )
        try:
            result = prune_feed_impressions(
                cutoff=cutoff,
                batch_size=options["batch_size"],
                max_batches=options["max_batches"],
                apply=options["apply"],
            )
        except ValueError as error:
            raise CommandError(str(error)) from None

        stamp = result.cutoff.isoformat()
        if not result.applied:
            self.stdout.write(
                f"Dry run: {result.eligible} FeedImpression row(s) received before {stamp} "
                "are eligible; nothing was deleted. Delete them with: "
                f"reading_prune_impressions --apply --before {stamp}"
            )
            return
        remaining = result.eligible - result.deleted
        self.stdout.write(
            f"Deleted {result.deleted} FeedImpression row(s) received before {stamp} "
            f"in {result.batches} batch(es); {max(remaining, 0)} eligible row(s) remain."
        )
