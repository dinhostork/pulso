"""Load the Story matching evaluation corpus into real News persistence.

The corpus (`tests/fixtures/news/stories/corpus.json`, catalogued in that
directory's README) is ground truth for event grouping: `expected_event` says
which Articles describe the same event. It holds no matcher output, similarity
or threshold. `read_corpus` parses and validates the file without a database;
`load_corpus` materializes it as ordinary `Source`, `SourceEndpoint`,
`RawArticle` and `Article` rows so Story matching runs against real rows.

Fixture ids are stable across runs; database primary keys are not, so tests
refer to Articles through `LoadedCorpus.article_ids`.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType

from news.domain.fingerprints import content_fingerprint
from news.models import Article, RawArticle, Source, SourceEndpoint

CORPUS_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "news" / "stories" / "corpus.json"

REQUIRED_SCENARIOS = (
    "same_event_different_source",
    "same_event_different_wording",
    "same_topic_different_event",
    "same_entities_different_event",
    "same_event_later_reporting",
    "temporally_distant_similar_event",
    "syndicated_duplicate_publication",
    "high_lexical_overlap_different_event",
    "clearly_unrelated_events",
    "story_drift_boundary",
    "same_conflict_different_event",
    "same_war_technology_different_event",
)
REQUIRED_FIELDS = frozenset(
    {
        "id",
        "source_slug",
        "title",
        "body",
        "language",
        "published_offset_minutes",
        "expected_event",
        "note",
    }
)
# `syndicated_from` records publication provenance (a copy of another record),
# never event truth; the loader turns it into News Core's `duplicate_of`.
OPTIONAL_FIELDS = frozenset({"rationale", "syndicated_from"})


class CorpusError(ValueError):
    """The corpus file breaks its documented schema."""


@dataclass(frozen=True)
class CorpusArticle:
    id: str
    source_slug: str
    title: str
    body: str
    language: str
    published_offset_minutes: int
    expected_event: str
    note: str
    rationale: str = ""
    syndicated_from: str = ""

    @property
    def canonical_url(self) -> str:
        return f"https://{self.source_slug}.example/stories/{self.id}"


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    article_ids: tuple[str, ...]


@dataclass(frozen=True)
class Corpus:
    anchor: datetime
    scenarios: Mapping[str, Scenario]
    articles: tuple[CorpusArticle, ...]

    def published_at(self, article: CorpusArticle) -> datetime:
        """The only source of fixture time: the declared anchor plus the offset."""

        return self.anchor + timedelta(minutes=article.published_offset_minutes)

    def article(self, fixture_id: str) -> CorpusArticle:
        return next(article for article in self.articles if article.id == fixture_id)


def _article(record: object, position: int) -> CorpusArticle:
    if not isinstance(record, dict):
        raise CorpusError(f"articles[{position}] must be an object")
    keys = set(record)
    missing = sorted(REQUIRED_FIELDS - keys)
    unknown = sorted(keys - REQUIRED_FIELDS - OPTIONAL_FIELDS)
    if missing or unknown:
        raise CorpusError(f"articles[{position}] missing {missing} or unknown {unknown} fields")
    for key, value in record.items():
        expected_type = int if key == "published_offset_minutes" else str
        if type(value) is not expected_type or (expected_type is str and not value.strip()):
            raise CorpusError(
                f"articles[{position}].{key} must be a nonempty {expected_type.__name__}"
            )
    return CorpusArticle(**record)


def read_corpus(path: Path = CORPUS_PATH) -> Corpus:
    """Parse and validate the corpus file; no database access."""

    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise CorpusError("unsupported corpus schema_version")
    anchor = datetime.fromisoformat(document["anchor"])
    if anchor.utcoffset() is None:
        raise CorpusError("anchor must carry an explicit UTC offset")
    articles = tuple(
        _article(record, position) for position, record in enumerate(document["articles"])
    )
    ids = [article.id for article in articles]
    if len(set(ids)) != len(ids):
        raise CorpusError("article ids must be unique")
    earlier: set[str] = set()
    for article in articles:
        if article.syndicated_from and article.syndicated_from not in earlier:
            raise CorpusError(f"{article.id} must be syndicated from an earlier record")
        earlier.add(article.id)
    known = set(ids)
    scenarios = {}
    for name, entry in document["scenarios"].items():
        members = tuple(entry["article_ids"])
        unknown = sorted(set(members) - known)
        if unknown:
            raise CorpusError(f"scenario {name} lists unknown articles {unknown}")
        scenarios[name] = Scenario(name, entry["description"], members)
    return Corpus(anchor=anchor, scenarios=MappingProxyType(scenarios), articles=articles)


@dataclass(frozen=True)
class LoadedCorpus:
    """Database ids of a loaded corpus, keyed and ordered by stable fixture id."""

    corpus: Corpus
    article_ids: Mapping[str, int]

    @property
    def expected_events(self) -> dict[int, str]:
        """Ground truth for `story_metrics.evaluate`: Article pk -> event label."""

        return {
            self.article_ids[article.id]: article.expected_event for article in self.corpus.articles
        }

    @property
    def names(self) -> dict[int, str]:
        """Article pk -> fixture id, so metric diagnostics read in fixture terms."""

        return {pk: fixture_id for fixture_id, pk in self.article_ids.items()}


def _source(slug: str) -> Source:
    source, _ = Source.objects.get_or_create(
        slug=slug, defaults={"name": slug.replace("-", " ").title()}
    )
    return source


def _endpoint(source: Source) -> SourceEndpoint:
    # A loopback IP literal passes endpoint validation without DNS under the
    # test settings; the endpoint is inactive and never fetched.
    url = f"http://127.0.0.1/story-corpus/{source.slug}.xml"
    endpoint = SourceEndpoint.objects.filter(url=url).first()
    if endpoint is None:
        endpoint = SourceEndpoint.objects.create(
            source=source, kind=SourceEndpoint.Kind.RSS, url=url, is_active=False
        )
    return endpoint


def _create_article(corpus: Corpus, record: CorpusArticle, duplicate_of: Article | None) -> Article:
    source = _source(record.source_slug)
    endpoint = _endpoint(source)
    published_at = corpus.published_at(record)
    fingerprint = content_fingerprint(record.title, record.body, "")
    payload = {"id": record.id, "title": record.title, "body": record.body}
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    raw = RawArticle.objects.create(
        endpoint=endpoint,
        external_key_kind=RawArticle.ExternalKeyKind.EXTERNAL_ID,
        external_key=record.id,
        external_id=record.id,
        url=record.canonical_url,
        payload=payload,
        payload_hash=hashlib.sha256(payload_bytes).hexdigest(),
        fetched_at=published_at,
        status=RawArticle.Status.PROCESSED,
        outcome=(
            RawArticle.Outcome.CONTENT_DUPLICATE
            if duplicate_of is not None
            else RawArticle.Outcome.ARTICLE_CREATED
        ),
        processed_at=published_at,
    )
    article = Article.objects.create(
        source=source,
        endpoint=endpoint,
        raw_article=raw,
        external_id=record.id,
        canonical_url=record.canonical_url,
        title=record.title,
        body_text=record.body,
        published_at=published_at,
        language=record.language,
        content_fingerprint=fingerprint,
        duplicate_of=duplicate_of,
        first_seen_at=published_at,
    )
    raw.article = article
    raw.save(update_fields=["article"])
    return article


def load_corpus(corpus: Corpus | None = None) -> LoadedCorpus:
    """Materialize the corpus; loading again returns the same rows unchanged."""

    corpus = corpus or read_corpus()
    articles: dict[str, Article] = {}
    for record in corpus.articles:
        existing = Article.objects.filter(canonical_url=record.canonical_url).first()
        if existing is None:
            origin = articles.get(record.syndicated_from) if record.syndicated_from else None
            existing = _create_article(corpus, record, origin)
        articles[record.id] = existing
    return LoadedCorpus(
        corpus=corpus,
        article_ids=MappingProxyType(
            {record.id: articles[record.id].pk for record in corpus.articles}
        ),
    )
