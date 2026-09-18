"""Process or reprocess one Article's Story state (no HTTP endpoint)."""

from django.core.management.base import BaseCommand

from news.application.story_processing import current_keys, process_article, reprocess_article
from news.tasks import embed_article_story

from ._operator import resolve_article, story_step_line


class Command(BaseCommand):
    help = "Embed and match one Article now, or rebuild its derived Story state."

    def add_arguments(self, parser):
        parser.add_argument("--article", required=True, help="Article id.")
        parser.add_argument(
            "--reprocess",
            action="store_true",
            help=(
                "Delete this Article's embeddings, primary Story association and "
                "processing record first. The Article itself is never changed."
            ),
        )
        parser.add_argument(
            "--async",
            dest="run_async",
            action="store_true",
            help="Queue the Celery task instead of running in this process (not with --reprocess).",
        )

    def handle(self, *args, **options):
        article_id = resolve_article(options["article"])
        if options["run_async"] and not options["reprocess"]:
            result = embed_article_story.delay(article_id)
            self.stdout.write(
                f"Queued embed_article_story for article {article_id} "
                f"pipeline_key={current_keys().pipeline_key} task_id={result.id}"
            )
            return
        step = reprocess_article if options["reprocess"] else process_article
        self.stdout.write(story_step_line(step(article_id)))
