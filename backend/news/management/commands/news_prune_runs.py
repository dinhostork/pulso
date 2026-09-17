"""Delete finalized ingestion-run history past the retention window (issue #20)."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from news.application.operations import prune_ingestion_runs


class Command(BaseCommand):
    help = "Delete finalized IngestionRun rows finished more than N days ago."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=settings.NEWS_RUN_RETENTION_DAYS,
            help=(
                "Retention in days, measured from finished_at "
                f"(default: {settings.NEWS_RUN_RETENTION_DAYS})."
            ),
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days < 1:
            raise CommandError("--days must be a positive integer")

        # The rule lives in the application layer, shared with the weekly task.
        deleted = prune_ingestion_runs(days=days)
        self.stdout.write(
            f"Deleted {deleted} finalized IngestionRun row(s) finished more than "
            f"{days} day(s) ago; RawArticle and Article rows are untouched."
        )
