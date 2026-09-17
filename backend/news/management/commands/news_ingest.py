"""Trigger one endpoint ingestion from the operator surface (no HTTP endpoint)."""

from django.core.management.base import BaseCommand

from news.application.ingest import ingest_endpoint as ingest_endpoint_app
from news.tasks import TRIGGER_MANUAL
from news.tasks import ingest_endpoint as ingest_endpoint_task

from ._operator import resolve_endpoint


class Command(BaseCommand):
    help = "Ingest one News endpoint now, synchronously or through the worker."

    def add_arguments(self, parser):
        parser.add_argument("--endpoint", required=True, help="Endpoint id or exact endpoint URL.")
        parser.add_argument(
            "--async",
            dest="run_async",
            action="store_true",
            help="Dispatch the Celery task instead of running in this process.",
        )

    def handle(self, *args, **options):
        endpoint = resolve_endpoint(options["endpoint"])
        if options["run_async"]:
            # The first execution of a manually queued run is still MANUAL;
            # only its retries are RETRY (news/tasks.py).
            result = ingest_endpoint_task.delay(endpoint.pk, trigger=TRIGGER_MANUAL)
            self.stdout.write(
                f"Queued ingest_endpoint for endpoint {endpoint.pk} "
                f"({endpoint.source.slug}) task_id={result.id}"
            )
            return

        summary = ingest_endpoint_app(endpoint.pk, trigger=TRIGGER_MANUAL)
        self.stdout.write(
            f"run_id={summary.run_id} status={summary.status} endpoint={endpoint.pk} "
            f"source={endpoint.source.slug}"
        )
        self.stdout.write(
            f"items_received={summary.items_received} "
            f"items_rejected={summary.items_rejected} "
            f"raw_created={summary.raw_created} "
            f"raw_changed={summary.raw_changed} "
            f"raw_unchanged={summary.raw_unchanged} "
            f"items_processed={summary.items_processed} "
            f"items_failed={summary.items_failed}"
        )
        self.stdout.write(
            f"identity_duplicates={summary.identity_duplicates} "
            f"content_duplicates={summary.content_duplicates} "
            f"raw_rejected={summary.raw_rejected} "
            f"source_identity_conflicts={summary.source_identity_conflicts}"
        )
        if summary.error_kind:
            self.stdout.write(f"error_kind={summary.error_kind} will_retry={summary.will_retry}")
