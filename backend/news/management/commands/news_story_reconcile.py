"""Process a bounded batch of Articles whose Story state is missing or stale."""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from news.application.story_processing import (
    claim_for_reconciliation,
    current_keys,
    process_article,
    reconciliation_candidates,
)
from news.tasks import reconcile_article_stories

from ._operator import story_step_line


class Command(BaseCommand):
    help = (
        "Process Articles with missing, stale, stuck or retryable Story state, "
        "in article-id order and at most --limit of them."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=None, help="Batch bound (NEWS_STORY_RECONCILE_BATCH)."
        )
        parser.add_argument(
            "--async",
            dest="run_async",
            action="store_true",
            help="Queue the reconciliation task (always uses the configured batch bound).",
        )

    def handle(self, *args, **options):
        if options["run_async"]:
            result = reconcile_article_stories.delay()
            self.stdout.write(f"Queued reconcile_article_stories task_id={result.id}")
            return
        limit = options["limit"]
        if limit is not None and limit < 1:
            raise CommandError("--limit must be a positive integer")
        with transaction.atomic():
            article_ids = reconciliation_candidates(limit=limit)
            claim_for_reconciliation(article_ids)
        self.stdout.write(f"selected={len(article_ids)} pipeline_key={current_keys().pipeline_key}")
        for article_id in article_ids:
            self.stdout.write(story_step_line(process_article(article_id)))
