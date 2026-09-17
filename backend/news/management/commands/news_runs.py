"""Inspect recent ingestion runs and detect stale RUNNING ones (issue #20)."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from news.application.operations import stale_running_cutoff
from news.models import IngestionRun

from ._operator import resolve_endpoint

DEFAULT_LAST = 20

COLUMNS = (
    ("ID", "pk", 6),
    ("ENDPOINT", "endpoint_id", 8),
    ("STATUS", "status", 10),
    ("TRIGGER", "trigger", 9),
    ("ATT", "attempt", 3),
    ("RECV", "items_received", 5),
    ("REJ", "items_rejected", 4),
    ("NEW", "raw_created", 4),
    ("CHG", "raw_changed", 4),
    ("SAME", "raw_unchanged", 4),
    ("IDDUP", "identity_duplicates", 5),
    ("CTDUP", "content_duplicates", 5),
    ("RAWREJ", "raw_rejected", 6),
    ("SRCCONF", "source_identity_conflicts", 7),
    ("FAIL", "items_failed", 4),
    ("ERROR", "error_kind", 16),
    ("MS", "duration_ms", 7),
)


def _stamp(value) -> str:
    return "-" if value is None else value.isoformat(timespec="seconds")


class Command(BaseCommand):
    help = "Show recent ingestion runs, or only stale RUNNING ones."

    def add_arguments(self, parser):
        parser.add_argument("--endpoint", default="", help="Limit to one endpoint id or exact URL.")
        parser.add_argument(
            "--last",
            type=int,
            default=DEFAULT_LAST,
            help=f"How many runs to show, newest first (default: {DEFAULT_LAST}).",
        )
        parser.add_argument(
            "--stale",
            action="store_true",
            help=(
                "Show only RUNNING runs older than "
                f"{settings.NEWS_STALE_RUNNING_SECONDS} s and exit 1 if any exist."
            ),
        )

    def handle(self, *args, **options):
        if options["last"] < 1:
            raise CommandError("--last must be a positive integer")

        # Newest first with a deterministic tie-breaker, limited in SQL.
        runs = IngestionRun.objects.order_by("-started_at", "-pk")
        if options["endpoint"]:
            runs = runs.filter(endpoint=resolve_endpoint(options["endpoint"]))
        if options["stale"]:
            runs = runs.filter(
                status=IngestionRun.Status.RUNNING, started_at__lt=stale_running_cutoff()
            )
        selected = list(runs[: options["last"]])

        if not selected:
            self.stdout.write("No stale runs." if options["stale"] else "No ingestion runs.")
            return

        self._table(selected)
        if options["stale"]:
            # A nonzero exit status makes this usable in a monitoring script.
            raise CommandError(
                f"{len(selected)} stale RUNNING run(s) older than "
                f"{settings.NEWS_STALE_RUNNING_SECONDS}s"
            )

    def _table(self, runs):
        header = "  ".join(f"{title:<{width}}" for title, _, width in COLUMNS)
        self.stdout.write(f"{header}  STARTED_AT                 FINISHED_AT")
        for run in runs:
            cells = []
            for _, field, width in COLUMNS:
                value = getattr(run, field)
                cells.append(f"{'-' if value is None else value!s:<{width}}")
            self.stdout.write(
                "  ".join(cells) + f"  {_stamp(run.started_at):<26} {_stamp(run.finished_at)}"
            )
