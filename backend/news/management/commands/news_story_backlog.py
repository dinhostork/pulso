"""List Articles whose Story processing is not complete (#33)."""

from django.core.management.base import BaseCommand, CommandError

from news.application.story_processing import MISSING, incomplete_articles, process_article
from news.models import ArticleStoryProcessing

from ._operator import bounded_limit, stamp, story_step_line

DEFAULT_LIMIT = 50
FAILED = ArticleStoryProcessing.State.FAILED


class Command(BaseCommand):
    help = (
        "List Articles with missing (never attempted), PENDING, EMBEDDED, FAILED, "
        "UNASSIGNED or STALE Story processing, in article-id order. Exits 1 when the "
        "listing contains failures."
    )

    def add_arguments(self, parser):
        view = parser.add_mutually_exclusive_group()
        view.add_argument("--failed", action="store_true", help="Only FAILED Articles.")
        view.add_argument(
            "--pending", action="store_true", help="Only incomplete Articles that have not failed."
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_LIMIT,
            help=f"How many Articles to list, 1-1000 (default: {DEFAULT_LIMIT}).",
        )
        parser.add_argument(
            "--process",
            action="store_true",
            help="Process every listed Article now, through the Story processing service.",
        )

    def handle(self, *args, **options):
        limit = bounded_limit(options["limit"], "--limit")
        view = "failed" if options["failed"] else "pending" if options["pending"] else "all"
        rows = incomplete_articles(view=view, limit=limit)
        if not rows:
            self.stdout.write("No incomplete Story processing.")
            return

        self.stdout.write(
            f"{'ARTICLE':<8}  {'STATUS':<10}  {'ATT':<3}  {'FAILED_STEP':<17}  "
            f"{'ERROR':<24}  {'UPDATED_AT':<25}  PIPELINE_KEY"
        )
        for row in rows:
            self.stdout.write(
                f"{row.article_id:<8}  {row.category:<10}  {row.attempts:<3}  "
                f"{row.failed_step or '-':<17}  "
                f"{row.error_kind or '-':<24}  {stamp(row.updated_at):<25}  {row.pipeline_key}"
            )
        missing = sum(1 for row in rows if row.category == MISSING)
        failed = sum(1 for row in rows if row.category == FAILED)
        self.stdout.write(f"listed={len(rows)} missing={missing} failed={failed} limit={limit}")

        if options["process"]:
            results = [process_article(row.article_id) for row in rows]
            for result in results:
                self.stdout.write(story_step_line(result))
            failed = sum(1 for result in results if result.state == FAILED)
            if failed:
                raise CommandError(f"{failed} Article(s) still FAILED after processing")
            return
        if failed:
            # A nonzero exit status makes this usable in a monitoring script.
            raise CommandError(f"{failed} FAILED Story processing record(s)")
