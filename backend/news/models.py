"""Publication provenance, normalized Article and derived Story persistence."""

import json
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import models
from django.db.models import F, Func, Q
from django.utils import timezone
from pgvector.django import VectorField

from news.adapters.targets import assert_allowed_target
from news.application.ports import FetchError
from news.application.story_ports import MAX_EMBEDDING_DIMENSION, MAX_MODEL_KEY_LENGTH


class Source(models.Model):
    """Identity of a publisher, independent of its ingestion endpoints."""

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=255)
    homepage_url = models.URLField(blank=True)
    default_language = models.CharField(max_length=35, default="en")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


_SECRET_KEY = re.compile(r"token|key|secret|password|authorization", re.IGNORECASE)
_HTTP_URL = URLValidator(schemes=["http", "https"])


def _secret_keys(value, path=""):
    """Yield config key paths only; values must never reach error messages."""

    if isinstance(value, dict):
        for key, nested in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if _SECRET_KEY.search(str(key)):
                yield key_path
            else:
                yield from _secret_keys(nested, key_path)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _secret_keys(nested, f"{path}[{index}]")


class SourceEndpoint(models.Model):
    """One concrete RSS or JSON Feed location for a publisher."""

    class Kind(models.TextChoices):
        RSS = "RSS", "RSS"
        JSON_FEED = "JSON_FEED", "JSON Feed"

    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="endpoints")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    url = models.URLField(unique=True)
    is_active = models.BooleanField(default=True)
    fetch_interval_seconds = models.PositiveIntegerField(default=900)
    adapter_config = models.JSONField(default=dict, blank=True)
    etag = models.CharField(max_length=255, blank=True)
    last_modified = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(fetch_interval_seconds__gt=0), name="news_endpoint_interval_gt_zero"
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        try:
            _HTTP_URL(self.url)
        except ValidationError:
            errors["url"] = "Endpoint URL must use http or https."
        else:
            try:
                assert_allowed_target(
                    self.url, allow_private=settings.NEWS_FETCH_ALLOW_PRIVATE_NETWORKS
                )
            except FetchError as error:
                errors["url"] = f"Endpoint target rejected: {error.kind.value}."
        if not isinstance(self.adapter_config, dict):
            errors["adapter_config"] = "Adapter configuration must be an object."
        else:
            keys = list(_secret_keys(self.adapter_config))
            if keys:
                errors["adapter_config"] = [
                    f"Secret-like configuration key is forbidden: {key}" for key in keys
                ]
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


class IngestionRun(models.Model):
    """Operational record of one endpoint fetch and raw-material intake."""

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        PARTIAL = "PARTIAL", "Partial"
        NO_CHANGE = "NO_CHANGE", "No change"
        FAILED = "FAILED", "Failed"

    endpoint = models.ForeignKey(
        SourceEndpoint, on_delete=models.PROTECT, related_name="ingestion_runs"
    )
    trigger = models.CharField(max_length=32)
    attempt = models.PositiveIntegerField(default=0)
    task_id = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    error_kind = models.CharField(max_length=32, blank=True)
    error_message = models.CharField(max_length=512, blank=True)
    will_retry = models.BooleanField(default=False)
    items_received = models.PositiveIntegerField(default=0)
    items_rejected = models.PositiveIntegerField(default=0)
    raw_created = models.PositiveIntegerField(default=0)
    raw_unchanged = models.PositiveIntegerField(default=0)
    raw_changed = models.PositiveIntegerField(default=0)
    items_processed = models.PositiveIntegerField(default=0)
    items_failed = models.PositiveIntegerField(default=0)
    # Deduplication results of the processing step (ADR-0010). `raw_rejected`
    # counts RawArticles rejected while processing, which is distinct from
    # `items_rejected` (adapter/intake rejections).
    identity_duplicates = models.PositiveIntegerField(default=0)
    content_duplicates = models.PositiveIntegerField(default=0)
    raw_rejected = models.PositiveIntegerField(default=0)
    source_identity_conflicts = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveBigIntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["endpoint", "-started_at"], name="news_run_endpoint_recent_idx"),
            models.Index(fields=["status", "started_at"], name="news_run_status_started_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(attempt__gte=0), name="news_run_attempt_nonnegative"
            ),
            models.CheckConstraint(
                condition=(
                    Q(items_received__gte=0)
                    & Q(items_rejected__gte=0)
                    & Q(raw_created__gte=0)
                    & Q(raw_unchanged__gte=0)
                    & Q(raw_changed__gte=0)
                    & Q(items_processed__gte=0)
                    & Q(items_failed__gte=0)
                    & Q(identity_duplicates__gte=0)
                    & Q(content_duplicates__gte=0)
                    & Q(raw_rejected__gte=0)
                    & Q(source_identity_conflicts__gte=0)
                ),
                name="news_run_counters_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(duration_ms__isnull=True) | Q(duration_ms__gte=0),
                name="news_run_duration_nonnegative",
            ),
        ]


class RawArticle(models.Model):
    """A received payload revision, distinct from a normalized Article."""

    class ExternalKeyKind(models.TextChoices):
        EXTERNAL_ID = "EXTERNAL_ID", "External ID"
        CANONICAL_URL = "CANONICAL_URL", "Canonical URL"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSED = "PROCESSED", "Processed"
        REJECTED = "REJECTED", "Rejected"

    class Outcome(models.TextChoices):
        NONE = "", "No outcome"
        ARTICLE_CREATED = "ARTICLE_CREATED", "Article created"
        ARTICLE_UPDATED = "ARTICLE_UPDATED", "Article updated"
        IDENTITY_DUPLICATE = "IDENTITY_DUPLICATE", "Identity duplicate"
        CONTENT_DUPLICATE = "CONTENT_DUPLICATE", "Content duplicate"
        IDENTITY_CONFLICT = "IDENTITY_CONFLICT", "Identity conflict"
        SOURCE_IDENTITY_CONFLICT = "SOURCE_IDENTITY_CONFLICT", "Source identity conflict"

    endpoint = models.ForeignKey(
        SourceEndpoint, on_delete=models.PROTECT, related_name="raw_articles"
    )
    ingestion_run = models.ForeignKey(
        IngestionRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="raw_articles",
    )
    external_key_kind = models.CharField(max_length=16, choices=ExternalKeyKind.choices)
    external_key = models.TextField()
    external_id = models.TextField(blank=True)
    url = models.TextField(blank=True)
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=64)
    supersedes = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="revisions"
    )
    fetched_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    outcome = models.CharField(max_length=32, choices=Outcome.choices, blank=True, default="")
    rejection_reason = models.CharField(max_length=64, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    article = models.ForeignKey(
        "Article", on_delete=models.SET_NULL, null=True, blank=True, related_name="raw_revisions"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["endpoint", "external_key_kind", "external_key", "payload_hash"],
                name="news_raw_identity_revision_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=["endpoint", "external_key_kind", "external_key"],
                name="news_raw_identity_idx",
            ),
            models.Index(fields=["status", "created_at"], name="news_raw_status_created_idx"),
        ]


class Article(models.Model):
    """One normalized publication attributed to one Source."""

    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="articles")
    endpoint = models.ForeignKey(SourceEndpoint, on_delete=models.PROTECT, related_name="articles")
    raw_article = models.ForeignKey(
        RawArticle, on_delete=models.PROTECT, related_name="normalized_articles"
    )
    external_id = models.TextField(blank=True)
    canonical_url = models.URLField(unique=True)
    title = models.TextField()
    description = models.TextField(blank=True)
    body_text = models.TextField(blank=True)
    byline = models.CharField(max_length=512, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    language = models.CharField(max_length=35)
    content_fingerprint = models.CharField(max_length=64, db_index=True)
    duplicate_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="duplicates"
    )
    first_seen_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_id"],
                condition=~Q(external_id=""),
                name="news_article_source_external_id_unique",
            ),
            models.CheckConstraint(
                condition=~Q(canonical_url=""), name="news_article_canonical_url_nonempty"
            ),
            models.CheckConstraint(
                condition=Q(duplicate_of__isnull=True) | ~Q(duplicate_of_id=F("id")),
                name="news_article_not_self_duplicate",
            ),
        ]


class Story(models.Model):
    """One event described by one or more Articles (ADR-0003: Article != Story).

    `status` is the Story lifecycle:

    - `ACTIVE`: eligible to receive new Articles and to be returned as a
      matching candidate.
    - `ARCHIVED`: ineligible for both; candidate retrieval filters on it.

    Zero-member rule: a Story whose last `StoryArticle` is removed by
    reprocessing or reassignment must not remain an `ACTIVE` candidate. It is
    archived rather than deleted, so its id stays stable for diagnostics. The
    schema therefore allows a Story with no members; the transition itself is
    not performed here.
    """

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ARCHIVED = "ARCHIVED", "Archived"

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    language = models.CharField(max_length=35)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["status"], name="news_story_status_idx")]


EVIDENCE_MAX_BYTES = 4096
_EVIDENCE_CONTENT_KEYS = frozenset({"title", "body_text", "description", "payload"})


def _content_keys(value, path=""):
    """Yield evidence key paths only; values must never reach error messages."""

    if isinstance(value, dict):
        for key, nested in value.items():
            key_path = f"{path}.{key}" if path else str(key)
            if str(key) in _EVIDENCE_CONTENT_KEYS:
                yield key_path
            else:
                yield from _content_keys(nested, key_path)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _content_keys(nested, f"{path}[{index}]")


class StoryArticle(models.Model):
    """Derived, reprocessable association between one Story and one Article.

    Deleting and rebuilding these rows must always be safe: the Story side
    cascades, while the Article side is protected so derived state never
    removes publication provenance.

    At most one primary Story association per Article is a v0.3 persistence
    and assignment invariant, not a permanent one-Story-per-Article domain
    invariant. It records which Story currently leads an Article's event
    assignment under the v0.3 matcher policy; it is not a claim that an Article
    belongs to exactly one event. Non-primary associations are unconstrained, so
    the many-to-many model of ADR-0003 stays representable.

    `evidence` is bounded operator-facing diagnostic metadata: it may not
    exceed `EVIDENCE_MAX_BYTES` nor carry publication text.
    """

    class Method(models.TextChoices):
        CREATED_STORY = "CREATED_STORY", "Created story"
        MATCHED = "MATCHED", "Matched"
        MANUAL = "MANUAL", "Manual"

    # The unique (story, article) and (story, associated_at) indexes both lead
    # with `story`, so the implicit FK index would be redundant.
    story = models.ForeignKey(
        Story, on_delete=models.CASCADE, related_name="story_articles", db_index=False
    )
    article = models.ForeignKey(Article, on_delete=models.PROTECT, related_name="story_articles")
    is_primary = models.BooleanField(default=False)
    associated_at = models.DateTimeField(default=timezone.now)
    method = models.CharField(max_length=16, choices=Method.choices)
    # Cosine similarity (1 - pgvector cosine distance) to the matched Story;
    # NULL when the Article created the Story and nothing was compared (#28).
    similarity = models.FloatField(null=True, blank=True)
    matcher_key = models.CharField(max_length=128, blank=True)
    evidence = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["story", "article"], name="news_storyarticle_story_article_unique"
            ),
            models.UniqueConstraint(
                fields=["article"],
                condition=Q(is_primary=True),
                name="news_storyarticle_one_primary_per_article",
            ),
        ]
        indexes = [
            models.Index(fields=["story", "associated_at"], name="news_storyart_membership_idx"),
        ]

    def clean(self):
        super().clean()
        if not isinstance(self.evidence, dict):
            raise ValidationError({"evidence": "Evidence must be an object."})
        errors = [
            f"Publication content key is forbidden: {key}" for key in _content_keys(self.evidence)
        ]
        size = len(
            json.dumps(self.evidence, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if size > EVIDENCE_MAX_BYTES:
            errors.append(f"Evidence exceeds {EVIDENCE_MAX_BYTES} bytes.")
        if errors:
            raise ValidationError({"evidence": errors})

    def save(self, *args, **kwargs):
        self.clean()
        return super().save(*args, **kwargs)


def _embedding_constraints(prefix):
    """Checks shared by every stored vector: a nonempty model key and an exact dimension."""

    return [
        models.CheckConstraint(condition=~Q(model_key=""), name=f"{prefix}_model_key_nonempty"),
        models.CheckConstraint(
            condition=Q(dimension__gte=1) & Q(dimension__lte=MAX_EMBEDDING_DIMENSION),
            name=f"{prefix}_dimension_range",
        ),
        # The column is dimension-agnostic because models differ; this keeps
        # each row's vector exactly as long as its recorded dimension.
        models.CheckConstraint(
            condition=Q(
                dimension=Func(
                    F("vector"), function="vector_dims", output_field=models.IntegerField()
                )
            ),
            name=f"{prefix}_vector_matches_dimension",
        ),
    ]


class ArticleEmbedding(models.Model):
    """Derived semantic vector of one Article under one embedding model.

    Rebuildable at any time (ADR-0004): deleting these rows loses nothing that
    cannot be recomputed, and generating them never writes to `Article`.
    Vectors are only comparable within one `model_key`.
    """

    # The unique (article, model_key) index leads with `article`, so the
    # implicit FK index would be redundant.
    article = models.ForeignKey(
        Article, on_delete=models.PROTECT, related_name="embeddings", db_index=False
    )
    model_key = models.CharField(max_length=MAX_MODEL_KEY_LENGTH)
    dimension = models.PositiveSmallIntegerField()
    vector = VectorField()
    input_chars = models.PositiveIntegerField()
    generated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["article", "model_key"], name="news_articleemb_article_model_unique"
            ),
            *_embedding_constraints("news_articleemb"),
        ]
        indexes = [models.Index(fields=["model_key"], name="news_articleemb_model_key_idx")]


class StoryEmbedding(models.Model):
    """Derived semantic vector of one Story under one embedding model.

    `member_count` is how many member Articles the vector was computed from,
    so a later refresh can tell a stale representation from a current one.
    """

    story = models.ForeignKey(
        Story, on_delete=models.CASCADE, related_name="embeddings", db_index=False
    )
    model_key = models.CharField(max_length=MAX_MODEL_KEY_LENGTH)
    dimension = models.PositiveSmallIntegerField()
    vector = VectorField()
    member_count = models.PositiveIntegerField()
    generated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["story", "model_key"], name="news_storyemb_story_model_unique"
            ),
            models.CheckConstraint(
                condition=Q(member_count__gte=1), name="news_storyemb_member_count_positive"
            ),
            *_embedding_constraints("news_storyemb"),
        ]
        indexes = [models.Index(fields=["model_key"], name="news_storyemb_model_key_idx")]


class ArticleStoryProcessing(models.Model):
    """Derived Story-processing state of one Article (#29).

    A sibling of `Article` rather than columns on it: the Article is
    provenance, and whether it has found its Story yet is not part of that
    fact. Deleting this row loses nothing that reprocessing cannot rebuild.

    `embedding_model_key` and `matcher_key` name the pipeline the row is
    processing or has processed. Freshness compares that pair with the
    configured one (`pipeline_key` in `news.application.story_processing`),
    and keeping the two halves separate shows an operator which one moved.
    `attempts` counts failed executions for the recorded pair.
    """

    class State(models.TextChoices):
        PENDING = "PENDING", "Pending"
        EMBEDDED = "EMBEDDED", "Embedded"
        MATCHED = "MATCHED", "Matched"
        FAILED = "FAILED", "Failed"

    article = models.OneToOneField(
        Article, on_delete=models.PROTECT, related_name="story_processing"
    )
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    error_kind = models.CharField(max_length=32, blank=True)
    error_message = models.CharField(max_length=512, blank=True)
    embedding_model_key = models.CharField(max_length=MAX_MODEL_KEY_LENGTH, blank=True)
    matcher_key = models.CharField(max_length=128, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["state", "embedding_model_key", "matcher_key"],
                name="news_storyproc_reconcile_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(state="MATCHED") | (~Q(embedding_model_key="") & ~Q(matcher_key="")),
                name="news_storyproc_matched_has_keys",
            ),
        ]


class Topic(models.Model):
    """Reusable Topic vocabulary, keyed by a normalized slug (#30).

    A label, not a fact: it carries no description, claim or truth flag and
    no Source, so an extracted Topic can never be presented as a source.
    """

    slug = models.SlugField(max_length=100, unique=True)
    label = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)


class Entity(models.Model):
    """A named thing mentioned in source text, keyed by kind and normalized name.

    A derived observation, not an independent assertion about the world
    (ADR-0004): no description, claim, truth flag, external identifier or
    Source. Normalization merges trivial variants only; there is no entity
    resolution.
    """

    class Kind(models.TextChoices):
        PERSON = "PERSON", "Person"
        ORGANIZATION = "ORGANIZATION", "Organization"
        PLACE = "PLACE", "Place"
        OTHER = "OTHER", "Other"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    normalized_key = models.CharField(max_length=200)
    display_name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "normalized_key"], name="news_entity_kind_key_unique"
            ),
        ]


def _enrichment_constraints(prefix):
    return [
        models.CheckConstraint(condition=~Q(model_key=""), name=f"{prefix}_model_key_nonempty"),
        models.CheckConstraint(
            condition=Q(score__gte=0) & Q(score__lte=1), name=f"{prefix}_score_range"
        ),
    ]


class StoryTopic(models.Model):
    """Derived, disposable link from a Story to a Topic, stamped with its extractor."""

    # The unique (story, topic) index leads with `story`.
    story = models.ForeignKey(
        Story, on_delete=models.CASCADE, related_name="story_topics", db_index=False
    )
    topic = models.ForeignKey(Topic, on_delete=models.PROTECT, related_name="story_topics")
    score = models.FloatField()
    model_key = models.CharField(max_length=MAX_MODEL_KEY_LENGTH)
    generated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["story", "topic"], name="news_storytopic_unique"),
            *_enrichment_constraints("news_storytopic"),
        ]


class StoryEntity(models.Model):
    """Derived, disposable link from a Story to an Entity, stamped with its extractor."""

    # The unique (story, entity) index leads with `story`.
    story = models.ForeignKey(
        Story, on_delete=models.CASCADE, related_name="story_entities", db_index=False
    )
    entity = models.ForeignKey(Entity, on_delete=models.PROTECT, related_name="story_entities")
    score = models.FloatField()
    model_key = models.CharField(max_length=MAX_MODEL_KEY_LENGTH)
    generated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["story", "entity"], name="news_storyentity_unique"),
            *_enrichment_constraints("news_storyentity"),
        ]
