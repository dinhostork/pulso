# ADR-0010 — Article Identity and Deduplication

- **Status:** Accepted
- **Date:** 2026-09-17
- **Decision owners:** Pulso maintainers
- **Related:**
  - [ADR-0002 — PostgreSQL with pgvector for Relational and Vector Data](0002-postgresql-pgvector.md) — semantic similarity belongs to the Story Engine, not to this decision
  - [ADR-0003 — Article != Story](0003-article-not-equal-story.md) — an Article is one publication from one Source; deduplication is not Story clustering
  - [ADR-0004 — AI Is Not a Source of Truth](0004-ai-is-not-a-source.md) — the decision is deterministic and inspectable

## Context

News ingestion (#16) stores every received payload revision as a `RawArticle`
and normalization (#17) turns one revision into normalized publication
material. What was still undecided is the question this ADR answers:

> when does incoming normalized material *become* an existing Article, and when
> is it a different publication?

Several independent pressures make this non-obvious.

**Re-ingestion.** Endpoints are polled repeatedly and feeds carry the same
entries for days. Most deliveries are neither new publications nor changes.

**Provider revisions.** A publisher corrects a headline, appends a paragraph or
moves an article to a new URL while keeping its feed `guid`. The publication is
the same; its text and even its canonical URL may differ.

**Canonical URL ownership.** A canonical URL identifies a publication on the
public web. Aggregators, partner feeds and misconfigured endpoints deliver
entries whose `link` points at *another publisher's* URL. Deciding this by
ingestion order would silently re-attribute a publication to whichever Source
happened to be polled first.

**Syndicated copies.** Wire services are republished verbatim by many outlets,
each under its own URL. The text is identical; the publications are not. Source
counting and evidence weighting need to know that these are not independent.

**Same event, different publications.** Three outlets covering one vote produce
three different texts. ADR-0003 already forbids collapsing them, but a
deduplication rule that reasoned about similarity would do exactly that.

**Concurrency.** Two workers may process two revisions that resolve to the same
publication at the same time, on separate database connections.

```text
NormalizedArticle
        ↓
identity candidates
        ↓
pure deterministic decide(...)
        ↓
CREATE | UPDATE | IDENTITY_DUPLICATE
CONTENT_DUPLICATE | IDENTITY_CONFLICT | SOURCE_IDENTITY_CONFLICT
```

## Decision

Deduplication is an explicit, deterministic, database-backed decision. It lives
in one pure function, `news/domain/dedup.py::decide`, which receives the
normalized revision and up to three immutable candidate snapshots and returns a
`Decision` value. The application layer performs the lookups and applies the
result; it holds no second copy of the rules.

### Invariants

```text
Publication identity is a provider external ID or a canonical URL.

Title, date and content hashes are never identity.

Within a Source, an (source, external_id) match takes precedence over a
canonical URL match.

canonical_url is globally unique; a canonical URL has exactly one owning
Source.

A delivery from another Source using that URL is recorded as
SOURCE_IDENTITY_CONFLICT. It never re-attributes the Article.

Content fingerprint equality under different canonical URLs does not merge
Articles.

Eligible exact-content duplicates remain separate Articles linked with
duplicate_of, pointing at the earliest matching Article.

Tier 2 requires at least 200 normalized fingerprint-input characters
(NEWS_CONTENT_FINGERPRINT_MIN_CHARS).

Semantic similarity is not part of News Core deduplication.

Same Story is never sufficient evidence of duplicate publication.

An existing Article's Source, endpoint, revision and publication fields are
never changed by another Source's delivery, including after a race.
```

### Precedence

The order is the decision. Content equality is consulted only when no
publication identity candidate exists, so it can never override a conflict.

| # | Candidate | Condition | Decision |
| --- | --- | --- | --- |
| 1 | `(source, external_id)` | no canonical-URL match, or the same Article; URL and fingerprint unchanged | `IDENTITY_DUPLICATE(existing)` |
| 1 | `(source, external_id)` | no canonical-URL match, or the same Article; URL **or** fingerprint changed | `UPDATE(existing)` |
| 1 | `(source, external_id)` = A | canonical URL owned by a different Article B of the same Source | `IDENTITY_CONFLICT(A, B)` |
| 1 | `(source, external_id)` = A | canonical URL owned by Article B of another Source | `SOURCE_IDENTITY_CONFLICT(B)` |
| 2 | `canonical_url` | owner belongs to another Source | `SOURCE_IDENTITY_CONFLICT(existing)` |
| 2 | `canonical_url` | same Source, fingerprint unchanged | `IDENTITY_DUPLICATE(existing)` |
| 2 | `canonical_url` | same Source, fingerprint changed | `UPDATE(existing)` |
| 3 | `content_fingerprint` | different canonical URL and `fingerprint_input_length >= 200` | `CONTENT_DUPLICATE(earliest)` |
| 4 | — | nothing above matched | `CREATE` |

A provider id delivered with a new canonical URL is an update even when the
text is byte-identical: the publication moved, it was not republished.

### Persistence

| Decision | Article | RawArticle |
| --- | --- | --- |
| `CREATE` | created, `duplicate_of = NULL`, `first_seen_at = fetched_at` | `PROCESSED` / `ARTICLE_CREATED`, `article` → new |
| `UPDATE` | updated in place; `id`, `source`, `first_seen_at`, `created_at`, `duplicate_of` unchanged | `PROCESSED` / `ARTICLE_UPDATED`, `article` → existing |
| `IDENTITY_DUPLICATE` | untouched, including `updated_at` | `PROCESSED` / `IDENTITY_DUPLICATE`, `article` → existing |
| `CONTENT_DUPLICATE` | new Article with its own Source and URL, `duplicate_of` → earliest | `PROCESSED` / `CONTENT_DUPLICATE`, `article` → new |
| `IDENTITY_CONFLICT` | both untouched, none created | `REJECTED` / `IDENTITY_CONFLICT`, reason `IDENTITY_CONFLICT` |
| `SOURCE_IDENTITY_CONFLICT` | untouched, none created | `REJECTED` / `SOURCE_IDENTITY_CONFLICT`, reason `SOURCE_IDENTITY_CONFLICT`, `article` → owner |

The `article` link on a rejected cross-Source delivery is deliberate: it is the
evidence an operator needs, not an attribution.

### Concurrency

The database decides races; there is no Redis lock, endpoint lock or
application mutex. `Article.canonical_url` is globally unique and
`(source, external_id)` is unique when non-empty, so a concurrent insert fails.
The insert runs inside a nested `transaction.atomic()` savepoint, so catching
`IntegrityError` leaves the outer transaction usable; candidates are then
re-read and `decide` is called exactly **once** more. If that second decision
still wants to insert, the error was not the expected identity race and is
re-raised rather than retried or swallowed.

Under a same-Source race exactly one Article exists and the loser records
`IDENTITY_DUPLICATE`; under a cross-Source race exactly one Article exists,
owned by whichever insert the database accepted, and the loser records
`SOURCE_IDENTITY_CONFLICT`. Nothing re-attributes the winner afterwards.

Row locking is narrowed to the revision being processed
(`select_for_update(skip_locked=True, of=("self",))`) so that revisions of one
Source are not serialized against each other through their shared Source row.

### Observability

`IngestionRun` counts `identity_duplicates`, `content_duplicates`,
`raw_rejected` and `source_identity_conflicts`. A run with any processing
rejection finishes `PARTIAL`. `raw_rejected` counts RawArticles rejected during
processing, including normalization rejections; `items_rejected` keeps its #16
meaning of adapter/intake rejections. Both conflicts log exactly one WARNING
carrying identifiers, Source slugs, endpoint ids and the canonical URL — never
a title, description, body, payload or adapter configuration.

## Alternatives considered

### URL-only identity

Using the canonical URL as the sole identity is simpler and needs no provider
id handling.

Rejected: publishers move articles between URLs while keeping their feed
`guid`. Under URL-only identity every move creates a second Article for one
publication, permanently splitting its provenance, and a corrected URL can
never be recognized as the same publication.

### Title/date fingerprint identity

Hashing title plus publication date is attractive because every feed carries
both.

Rejected: it is neither sound nor complete. Generic headlines ("Market
update") collide across unrelated publications and Sources, while a corrected
headline or a re-dated entry breaks the identity of one publication. It also
makes identity depend on derived text rather than on what the publisher
actually declares. Title and date therefore never take part in identity.

### First-delivering-Source attribution

The Source that delivers a canonical URL first could simply own it, and later
deliveries could be attached to that Article.

Rejected: attribution would then depend on polling order. An aggregator polled
before the publisher would own the publisher's article, and provenance —
Pulso's core promise under ADR-0003 and ADR-0004 — would be decided by a
scheduler. Conflicts are recorded instead, so a human decides.

### Per-Source canonical URL uniqueness

Making `canonical_url` unique per Source would let several Sources hold the
same URL, avoiding conflicts entirely.

Rejected: it would duplicate one publication across Sources and inflate the
source count for a Story, which is exactly the signal ADR-0003 wants to keep
honest. It also removes the database's ability to arbitrate races on the one
identifier that is globally meaningful.

### Merging exact content duplicates into one Article

An exact-content republication could be folded into the existing Article.

Rejected: an Article is one publication from one Source. A syndicated copy is a
real publication with its own URL, Source, byline and timestamp; merging
destroys that provenance and makes the republication invisible. Linking with
`duplicate_of` keeps both and lets the Story Engine discount non-independent
evidence.

### Vector/semantic near-duplicate detection in News Core

pgvector is already available (ADR-0002), so near-duplicate detection could run
during ingestion.

Rejected: similarity answers "same event?", not "same publication?". Three
outlets covering one vote are highly similar and are three publications
(ADR-0003). Embedding-based matching is also non-deterministic across model
versions, cannot be reviewed by an operator from a row, and would make the
identity of a publication depend on a model. Similarity stays in the Story
Engine, where the question is event membership.

## Consequences

### Positive

- provenance is strong: an Article's Source, endpoint and revision are never
  rewritten by another Source's delivery;
- `RawArticle` history stays append-only, so every decision is replayable and
  auditable from stored payloads;
- provider revisions update one Article in place instead of accumulating
  near-identical rows;
- syndicated publications are preserved with their own URL and Source, and
  linked, so non-independent evidence is visible rather than lost;
- Source conflicts are operator-visible as rejected RawArticles, counters and
  identifier-only WARNINGs;
- global canonical ownership gives the database a single arbiter for races, so
  no distributed lock is needed;
- the decision is a pure function, so the whole table is testable without a
  database and cannot drift from the application code.

### Negative

- a canonical URL delivered by two Sources always costs one rejected
  RawArticle and needs review to resolve;
- the dedup rules and the threshold are additional behavior to understand
  before reading `process_raw_article`;
- `duplicate_of` points at the earliest matching Article, which may itself be a
  content duplicate, so consumers must not assume a single-level chain;
- an Article update is a visible mutation downstream consumers must tolerate
  (the #17 handoff contract).

## Risks

**Feeds that link to another publisher's canonical URL.** Partner feeds,
aggregators and misconfigured endpoints produce `SOURCE_IDENTITY_CONFLICT`
deliveries that require operator review. This is the accepted cost of never
re-attributing a publication; the WARNING carries both Source slugs and the URL
so the cause is identifiable without reading content.

**Threshold false negatives.** Short publications (headline-only entries, brief
alerts) never link as content duplicates even when they genuinely are
republications, because 200 characters of material is the minimum evidence this
decision accepts.

**Threshold false positives.** Two Sources publishing identical boilerplate
longer than the threshold — a shared press release printed verbatim — link as
content duplicates although each is an independent publication. Both Articles
survive, so the cost is a wrong link, not lost data.

**Canonicalization sensitivity.** Identity is only as good as the URL
canonicalizer. An over-aggressive rule merges distinct publications; an
under-aggressive one splits one publication across URLs. Canonicalization stays
deliberately conservative for that reason.

## Evolution criteria

This decision may need extension when:

- Tier 3 near-duplicate detection becomes necessary because exact fingerprints
  demonstrably miss real republications (measured, not assumed);
- a conflict-resolution or operator workflow is needed because
  `SOURCE_IDENTITY_CONFLICT` volume stops being reviewable by hand;
- evidence shows 200 normalized characters is the wrong threshold, in either
  direction;
- publisher identity changes (a Source splits, merges or transfers a domain)
  require canonical ownership to move deliberately;
- canonicalization rules prove insufficient for real feeds and identity needs
  additional provider-declared signals.

None of these are implemented now.

## Non-goals

ADR-0010 does not define:

- Story clustering or Article → Story membership;
- semantic event matching or similarity thresholds;
- embedding generation or storage;
- source credibility, ranking or editorial weighting;
- recommendation behavior;
- the operator conflict-resolution interface.

## Testing

Every rule above is covered by at least one of these tests.

Pure decision table — `backend/tests/news/test_dedup_decisions.py`:

- `test_no_candidates_creates`
- `test_external_id_with_unchanged_url_and_content_is_identity_duplicate`
- `test_external_id_with_changed_content_updates`
- `test_external_id_with_new_url_and_unchanged_content_updates`
- `test_external_id_with_new_url_and_changed_content_updates`
- `test_external_id_match_precedes_url_match`
- `test_external_id_and_other_url_article_of_same_source_is_identity_conflict`
- `test_external_id_and_other_url_article_of_other_source_is_source_identity_conflict`
- `test_canonical_url_of_same_source_with_same_content_is_identity_duplicate`
- `test_canonical_url_of_same_source_with_changed_content_updates`
- `test_cross_source_canonical_url_is_conflict`
- `test_eligible_fingerprint_under_a_different_url_is_content_duplicate`
- `test_fingerprint_on_the_same_url_is_not_a_content_duplicate`
- `test_fingerprint_below_threshold_creates`
- `test_fingerprint_threshold_boundary`
- `test_threshold_is_an_injected_primitive`
- `test_fingerprint_never_precedes_publication_identity`
- `test_same_story_without_identity_or_content_equality_creates`
- `test_decisions_and_matches_are_immutable`
- `test_dedup_domain_imports_no_framework`

Persistence and counters on PostgreSQL — `backend/tests/news/test_deduplication.py`:

- `test_same_external_id_new_url_updates_existing_article`
- `test_same_external_id_new_url_against_same_source_url_is_identity_conflict`
- `test_same_external_id_new_url_against_other_source_url_is_source_conflict`
- `test_same_canonical_other_endpoint_is_identity_duplicate`
- `test_cross_source_canonical_url_rejects_the_delivery`
- `test_source_conflict_never_reattributes_the_article`
- `test_syndicated_copy_links_duplicate_of`
- `test_content_duplicate_links_earliest_article`
- `test_short_identical_titles_are_not_content_duplicates`
- `test_same_story_publications_stay_separate_articles`
- `test_identical_payload_creates_no_new_raw_article`
- `test_distinct_revision_with_unchanged_publication_is_identity_duplicate`
- `test_processing_rejection_makes_the_run_partial`
- `test_unexplained_integrity_error_is_reraised`

Race arbitration on PostgreSQL — `backend/tests/news/test_concurrency.py`:

- `test_same_source_race_creates_one_article_and_one_identity_duplicate`
- `test_cross_source_race_records_source_identity_conflict`
- `test_loser_updates_nothing_on_the_winning_article`
- `test_two_workers_never_process_one_revision_twice`

Normalization and identity behavior this decision must not regress —
`backend/tests/news/test_process_raw_article.py`:

- `test_changed_revision_updates_in_place_and_preserves_invariants`
- `test_identity_duplicate_links_raw_without_mutating_article`
- `test_same_fingerprint_different_urls_remain_separate`
- `test_cross_source_canonical_owner_is_unchanged`
- `test_same_source_conflicting_candidates_are_rejected`
- `test_tracking_urls_share_canonical_identity_without_content_dedup`
- `test_normalization_rejections_preserve_raw_payload`

## Decision summary

```text
Deduplication != Story clustering
```

An Article is one publication from one Source. Identity is what the publisher
declares — a provider id or a canonical URL — never what the text resembles. A
canonical URL has one owner, and a second Source claiming it is a recorded
conflict, not a transfer. Identical content under a different URL is a real
republication: kept, linked, never merged. Everything else is a new Article.
