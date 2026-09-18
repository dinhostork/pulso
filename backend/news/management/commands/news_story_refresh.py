"""Refresh one Story or a bounded set of stale and failed Stories."""

from django.core.management.base import BaseCommand, CommandError

from news.application.story_refresh import refresh_candidates, refresh_story
from news.models import Story
from news.tasks import refresh_story_task


class Command(BaseCommand):
    help = "Refresh Story-derived state synchronously or queue it after commit."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--story", type=int, help="Story id")
        target.add_argument(
            "--stale-failed", action="store_true", help="Select stale and failed Stories"
        )
        parser.add_argument(
            "--limit", type=int, default=100, help="Positive batch bound, at most 1000"
        )
        parser.add_argument("--async", dest="run_async", action="store_true")

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 1 <= limit <= 1000:
            raise CommandError("--limit must be between 1 and 1000")
        if options["story"] is not None:
            story_id = options["story"]
            if story_id < 1 or not Story.objects.filter(pk=story_id).exists():
                raise CommandError(f"No Story with id {story_id}")
            ids = [story_id]
        else:
            ids = refresh_candidates(limit=limit)
        for story_id in ids:
            if options["run_async"]:
                task = refresh_story_task.delay(story_id, reason="operator")
                self.stdout.write(f"story_id={story_id} queued task_id={task.id}")
            else:
                result = refresh_story(story_id, reason="operator")
                self.stdout.write(
                    f"story_id={story_id} outcome={result.outcome} "
                    f"article_count={result.article_count} source_count={result.source_count} "
                    f"error_kind={result.error_kind or '-'}"
                )
