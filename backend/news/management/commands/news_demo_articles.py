"""Add a small fictional publication set for a local demo (#52). Dry run by default.

Nothing runs this automatically. It only creates what is missing, matched by
natural keys (Source slug, Article canonical URL), and never updates or
deletes an existing row, so repeated runs are harmless. It writes News Core
provenance only: Stories come from the normal Story pipeline afterwards, with
the configured embedding provider, exactly as for ingested Articles.
"""

import hashlib
import json
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from news.domain.fingerprints import content_fingerprint
from news.models import Article, RawArticle, Source, SourceEndpoint

DATA = Path(__file__).resolve().parents[2] / "demo_data" / "fictional_articles.json"
MAX_SOURCES = 5
MAX_ARTICLES = 20
SLUG_PREFIX = "fictional-"


def _load() -> dict:
    data = json.loads(DATA.read_text())
    sources, articles = data["sources"], data["articles"]
    if not 0 < len(sources) <= MAX_SOURCES or not 0 < len(articles) <= MAX_ARTICLES:
        raise CommandError("The demo data set exceeds its bounds")
    slugs = {source["slug"] for source in sources}
    for source in sources:
        if not source["slug"].startswith(SLUG_PREFIX):
            raise CommandError(f"Demo Source slugs must start with {SLUG_PREFIX!r}")
    for article in articles:
        if article["source"] not in slugs:
            raise CommandError(f"Demo Article {article['id']} names an unknown Source")
    return data


def _host(source: dict) -> str:
    return source["homepage_url"].split("/")[2]


def _canonical_url(sources: dict, article: dict) -> str:
    return f"https://{_host(sources[article['source']])}/stories/{article['id']}"


def _endpoint(source: Source, host: str) -> SourceEndpoint:
    url = f"https://{host}/feed.xml"
    endpoint = SourceEndpoint.objects.filter(url=url).first()
    if endpoint is not None:
        return endpoint
    # `.example` never resolves, so save() would refuse this target. The
    # endpoint is created inactive and is never fetched; activating it through
    # the normal operator path validates it again and fails, as it should.
    (endpoint,) = SourceEndpoint.objects.bulk_create(
        [SourceEndpoint(source=source, kind=SourceEndpoint.Kind.RSS, url=url, is_active=False)]
    )
    return endpoint


class Command(BaseCommand):
    help = (
        "Add fictional demo publications and Articles that do not exist yet. "
        "Dry run by default; --apply writes. Stories come from the Story pipeline."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Create the missing rows. Without it, nothing is written.",
        )

    def handle(self, *args, **options):
        data = _load()
        sources = {source["slug"]: source for source in data["sources"]}
        existing_sources = set(
            Source.objects.filter(slug__in=sources).values_list("slug", flat=True)
        )
        urls = {article["id"]: _canonical_url(sources, article) for article in data["articles"]}
        existing_urls = set(
            Article.objects.filter(canonical_url__in=urls.values()).values_list(
                "canonical_url", flat=True
            )
        )
        missing_sources = [slug for slug in sources if slug not in existing_sources]
        missing_articles = [a for a in data["articles"] if urls[a["id"]] not in existing_urls]
        self.stdout.write(
            f"fictional sources: {len(sources)} ({len(missing_sources)} missing); "
            f"articles: {len(urls)} ({len(missing_articles)} missing)"
        )
        if not options["apply"]:
            self.stdout.write("Dry run: nothing was written. Re-run with --apply to create them.")
            return

        now = timezone.now()
        with transaction.atomic():
            rows = {}
            for slug, source in sources.items():
                rows[slug], _ = Source.objects.get_or_create(
                    slug=slug,
                    defaults={"name": source["name"], "homepage_url": source["homepage_url"]},
                )
            for article in missing_articles:
                source = rows[article["source"]]
                endpoint = _endpoint(source, _host(sources[article["source"]]))
                published_at = now - timedelta(minutes=article["minutes_ago"])
                payload = {"id": article["id"], "title": article["title"], "body": article["body"]}
                payload_hash = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                raw = RawArticle.objects.create(
                    endpoint=endpoint,
                    external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
                    external_key=article["id"],
                    external_id=article["id"],
                    url=urls[article["id"]],
                    payload=payload,
                    payload_hash=payload_hash,
                    fetched_at=published_at,
                    status=RawArticle.Status.PROCESSED,
                    outcome=RawArticle.Outcome.ARTICLE_CREATED,
                    processed_at=published_at,
                )
                created = Article.objects.create(
                    source=source,
                    endpoint=endpoint,
                    raw_article=raw,
                    external_id=article["id"],
                    canonical_url=urls[article["id"]],
                    title=article["title"],
                    body_text=article["body"],
                    published_at=published_at,
                    language="en",
                    content_fingerprint=content_fingerprint(article["title"], article["body"], ""),
                    first_seen_at=published_at,
                )
                raw.article = created
                raw.save(update_fields=["article"])
        self.stdout.write(
            f"Created {len(missing_sources)} sources and {len(missing_articles)} articles. "
            "Next: python manage.py news_story_reconcile, then "
            "python manage.py news_story_refresh --stale-failed (see backend/README.md)."
        )
