"""Replay one rejected RawArticle through the ordinary processing path."""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from news.application.process import process_raw_article as process_raw_article_app
from news.models import RawArticle


class Command(BaseCommand):
    help = "Reset a REJECTED RawArticle to PENDING and process it again."

    def add_arguments(self, parser):
        parser.add_argument("--raw", required=True, type=int, help="RawArticle id to reprocess.")

    def handle(self, *args, **options):
        raw_id = options["raw"]
        if not RawArticle.objects.filter(pk=raw_id).exists():
            raise CommandError(f"No RawArticle with id {raw_id}")

        with transaction.atomic():
            # One conditional statement resets exactly the processing result
            # columns: receipt provenance (payload, hash, endpoint, run,
            # identity, fetched_at, supersedes) is never rewritten. Filtering
            # on REJECTED also makes a concurrent reset a no-op rather than a
            # second reprocessing.
            reset = RawArticle.objects.filter(pk=raw_id, status=RawArticle.Status.REJECTED).update(
                status=RawArticle.Status.PENDING,
                outcome=RawArticle.Outcome.NONE,
                rejection_reason="",
                processed_at=None,
                article=None,
            )
        if not reset:
            raise CommandError(
                f"RawArticle {raw_id} is not REJECTED; only a rejected revision may be reset"
            )

        # The reset is committed before processing, so the application's own
        # select_for_update owns the row lock for exactly its own transaction.
        outcome = process_raw_article_app(raw_id)
        self.stdout.write(
            f"raw_id={outcome.raw_id} state={outcome.state} outcome={outcome.outcome or '-'} "
            f"rejection_reason={outcome.rejection_reason or '-'} "
            f"article_id={outcome.article_id if outcome.article_id else '-'}"
        )
