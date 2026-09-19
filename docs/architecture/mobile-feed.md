# Mobile Feed Architecture and Contract

## Status and scope

This document is the executable contract for Phase 3 (Mobile Feed). Sections
marked **contract** describe behavior owned by issues #40–#52; they are not a
claim that every behavior is already implemented. Issue #41 now implements the
News-owned factual read DTOs/selectors and cursor primitives. Product HTTP
endpoints, Reading models, viewer decoration, and the mobile API client remain
owned by later issues.

Phase 3 delivers authenticated factual Story reading, source navigation,
bookmarks, and qualified feed-exposure reporting. Opinion, Position,
Perspective, Pulse, Recommendation ranking, media, sharing, search, and offline
mutation queues remain future work.

## Ownership

| Boundary                | Owns                                                                                                            | Must not own                                                                                     |
| ----------------------- | --------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| News                    | Shared factual Story state; product read DTOs; eligibility, freshness, synthesis, citation and source selectors | Viewer state, feed ranking, reading interactions, request-time processing                        |
| Reading                 | Private Bookmark and FeedImpression state; feed composition and viewer decoration                               | Story facts, Article provenance, Opinion/Pulse behavior, recommendation                          |
| Accounts                | Identity and the existing JWT server contract                                                                   | Reading rules or factual content                                                                 |
| Mobile                  | Transport, credential hooks, server-state cache, presentation, navigation and exposure qualification            | Factual synthesis, durable authority, recommendation                                             |
| Recommendation (future) | Candidate ranking through explicit News/Reading interfaces                                                      | Rewriting shared Story facts or interpreting bookmarks/impressions as truth, votes, or positions |

Reading is a logical module in the modular monolith. PostgreSQL is authoritative;
Redis is not durable reading storage.

```mermaid
flowchart LR
    Mobile[Expo mobile client] --> API[DRF adapters]
    API --> Accounts
    API --> Reading
    Reading --> News
    Accounts --> PG[(PostgreSQL)]
    Reading --> PG
    News --> PG
    Worker[Existing Story worker] --> News
    Recommendation[Future Recommendation] -. explicit interfaces .-> Reading
    Recommendation -. candidates .-> News
```

## Shared facts and private state

Two users reading the same database snapshot receive equal Story facts.
`viewer.bookmarked` is the only viewer field in Story card/detail payloads and
is decorated outside News. Feed session metadata never enters News synthesis.

| Shared factual state                                  | Private/account-scoped state                |
| ----------------------------------------------------- | ------------------------------------------- |
| Story identity/status/language and publication window | Bookmark existence and `saved_at`           |
| Published synthesis and ordered cited elements        | FeedImpression and feed session identifiers |
| Topics, Entities, Articles and Sources                | `viewer.bookmarked` decoration              |
| Current membership counts                             | Future recommendation signals               |

The invariants `Article != Story`, `Opinion != Perspective`, `Position !=
Perspective`, `AI != Source`, one active position per user/Story,
`Represents me != vote`, and `Recommendation != factual personalization` apply
to every endpoint and fixture.

## Wire contract

All new database `BigAutoField` product IDs are decimal JSON strings. UTC
timestamps use ISO-8601. Nullable values remain `null`; absent collections are
empty arrays. Product responses never expose vectors, signatures, matcher
evidence, provider errors, raw payloads, or full Article bodies.

### Story element

| Field         | Wire type                        | Meaning                                         |
| ------------- | -------------------------------- | ----------------------------------------------- |
| `id`          | decimal string                   | Synthesis-element ID                            |
| `kind`        | `TITLE`, `SUMMARY`, or `CONTEXT` | Persisted element kind                          |
| `position`    | nonnegative integer              | Stable order within its kind                    |
| `text`        | string                           | Bounded generated text                          |
| `article_ids` | array of decimal strings         | Ordered citations; TITLE provenance is retained |

### StoryCard

| Field                                     | Wire type / nullability            | Persisted mapping or fallback                                |
| ----------------------------------------- | ---------------------------------- | ------------------------------------------------------------ |
| `id`, `language`, `created_at`            | string                             | `Story`                                                      |
| `first_published_at`, `last_published_at` | timestamp or `null`                | `Story` publication window                                   |
| `content_state`                           | `CURRENT`, `UPDATING`, `PREPARING` | Freshness matrix below                                       |
| `synthesis_id`, `synthesized_at`          | decimal string/timestamp or `null` | Published generation; null while preparing                   |
| `title`                                   | string                             | Cited TITLE text, or neutral “Story being prepared” fallback |
| `elements`                                | ordered Story-element array        | TITLE, SUMMARY, then CONTEXT in persisted order              |
| `topics`                                  | array of `{slug,label}`            | Current stamped Topic links; may be empty                    |
| `article_count`, `source_count`           | nonnegative integers               | Published-generation counters for normal/updating cards      |
| `viewer`                                  | `{bookmarked:boolean}`             | Reading-owned decoration, separate from facts                |

### StoryDetail

StoryDetail contains the StoryCard factual fields and adds `entities`
(`{kind,display_name}`), explicitly named `current_article_count` and
`current_source_count`, `citations`, and `sources_path`. Each citation is a
SourceArticle keyed by its decimal-string Article ID. The cited set is complete
for returned elements, even when an Article is no longer a current member.
`sources_path` is a relative approved API path, never an absolute URL.

### SourceArticle

| Field                                                  | Wire type                |
| ------------------------------------------------------ | ------------------------ |
| `id`, `source.id`                                      | decimal strings          |
| `title`, `canonical_url`, `source.name`, `source.slug` | strings                  |
| `published_at`                                         | timestamp or `null`      |
| `first_seen_at`                                        | timestamp                |
| `byline`                                               | string or `null`         |
| `duplicate_of_id`                                      | decimal string or `null` |
| `is_current_member`                                    | boolean                  |

The canonical URL must be HTTP(S), have a host, contain no credentials or
control characters, and not use literal loopback/private/link-local addresses.
No DNS lookup, fetch, proxy, WebView, token, or Authorization header is used
when opening a publisher URL.

### Pages

Pages are `{results, next_cursor}`; feed pages additionally carry
`ordering: "story_created_desc_v1"`. `next_cursor` is an opaque string or
`null`, never an absolute URL. There is no unbounded total count.

## Freshness and availability

| Read snapshot                                              | Feed                  | Detail/sources                                                                                                   | Saved                                    |
| ---------------------------------------------------------- | --------------------- | ---------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| ACTIVE, live members, CURRENT, valid published generation  | Included as `CURRENT` | 200 current facts and current sources                                                                            | Normal                                   |
| ACTIVE, live members, STALE/FAILED, valid prior generation | Excluded              | 200 prior synthesis as `UPDATING`; generation citations and separately labelled current sources remain available | Updating                                 |
| ACTIVE, live members, no usable generation                 | Excluded              | 200 `PREPARING`, neutral title, no invented summary; sources available                                           | Preparing                                |
| ARCHIVED or zero live members                              | Excluded              | 410 `story_unavailable`, no obsolete facts                                                                       | Unavailable tombstone with remove action |
| Missing ID                                                 | Excluded              | 404 `story_not_found`                                                                                            | Client handles stale links               |

A usable generation is the current `StorySynthesis` with nonblank signature
matching the Story signature, a valid cited TITLE, bounded ordered elements,
and only same-signature Topic/Entity enrichment. Legacy unstamped enrichment
is omitted. `updated_at` is never presented as publication recency.

### Implemented read coherence (#41)

Generation anchoring alone is insufficient under the current schema:

| Response data | Generation-stable? | Evidence/consequence |
| --- | --- | --- |
| `StorySynthesis`, elements and citation links | Yes, retained per synthesis ID | These rows can anchor synthesis text and Article IDs |
| Story header/counters/status | No | The single Story row is updated during later refreshes |
| Topics and Entities | No | Associations are replaced in place; there is no generation history |
| Current membership | No | StoryArticle rows are reassigned/removed independently of old synthesis |
| Article title/URL/byline/revision | No | Article is revised in place; citation stores only its Article ID |
| Source name/active flag | No | Source rows are mutable; inactivity must not erase attribution |

Therefore #41 uses the approved fallback: each public factual read owns a
short PostgreSQL `REPEATABLE READ, READ ONLY` transaction, sets isolation
before its first ORM query, materializes all DTO data inside it, and returns
only after the transaction closes. Nested/caller-owned transactions are
rejected. The transaction performs no provider work, dispatch, writes, row
locks, JSON encoding, or network work. A separate-connection concurrency test
commits a complete refresh—including replaced synthesis/enrichment, mutable
Article/Source data and changed membership—while a read is paused between
queries; the reader receives the complete earlier snapshot and the writer is
not blocked.

## HTTP contract

All product endpoints require the existing bearer authentication and use no
trailing slash.

| Method/path                           | Input                                    | Success                             | Owner / implementation status after #40 |
| ------------------------------------- | ---------------------------------------- | ----------------------------------- | --------------------------------------- |
| `GET /api/feed`                       | `cursor?`, `limit?` (20 default, 50 max) | 200 StoryCard page                  | Reading + News / planned #47            |
| `GET /api/stories/{story_id}`         | positive decimal ID                      | 200 StoryDetail                     | News + viewer decoration / planned #47  |
| `GET /api/stories/{story_id}/sources` | `cursor?`, `limit?`, `synthesis_id?`     | 200 SourceArticle page              | News / planned #47                      |
| `GET /api/bookmarks`                  | `cursor?`, `limit?`                      | 200 saved page including tombstones | Reading / planned #42/#47               |
| `PUT /api/bookmarks/{story_id}`       | empty body                               | 200 bookmark result                 | Reading / planned #42/#47               |
| `DELETE /api/bookmarks/{story_id}`    | no body                                  | 204, present or absent              | Reading / planned #42/#47               |
| `POST /api/feed-impressions`          | `{events:[...]}`                         | 200 per-event outcomes              | Reading / planned #43/#51               |

Product errors are `{code,detail}` with an optional bounded `fields` object:
400 invalid input/cursor, 401 unauthorized, 404 missing, 409 changed cursor
context or idempotency conflict, 410 unavailable, 413 oversized body, 429
rate limited (with `Retry-After`), and generic 5xx. Unknown request fields and
client-supplied `user_id` are rejected. Existing authentication error bodies
remain unchanged and are normalized by the mobile transport.

## Pagination

Feed ordering is immutable `(Story.created_at DESC, Story.id DESC)`, not
publisher recency or personalized relevance. A signed, versioned keyset cursor
carries endpoint scope, page size, a first-page upper watermark, last tuple,
and a 24-hour expiry. The watermark prevents newly inserted Stories from
appearing inside an existing chain; it does not snapshot eligibility or
content. A refresh starts a new chain.

Saved ordering is `(Bookmark.created_at DESC, Bookmark.id DESC)` and its cursor
is account-bound. Source ordering is `Article.id ASC`; its cursor is bound to
Story, current membership signature, and optional synthesis context. Context
change returns 409 `source_context_changed` so the client restarts once rather
than appending mixed pages. Cursor signatures provide integrity, not
authorization.

The #41 implementation uses `news.application.story_cursors` for the fixed
signed payload and `news.application.story_read` for feed/detail/source reads.
The feed's `(status, created_at DESC, id DESC)` index is backed by an EXPLAIN
test over 1,000 representative Story rows. Factual feed reads execute the same
bounded query count for pages of 1 and 50: 8 SQL statements including
transaction statements. The measured detail/source reads use 11/5 statements,
with enforced fixed budgets of 12/8.

## Bookmarks

Bookmark is a private, durable reference to one Story. PUT is an idempotent set
operation and preserves the original `saved_at`; DELETE is idempotent. New
saves accept ACTIVE nonempty Stories (including preparing/updating), reject an
unavailable Story with 410, and reject a missing Story with 404. An existing
bookmark survives archival and appears as a tombstone. Story rebuild, merge,
or replacement never transfers it automatically. Bookmark does not alter feed
order, facts, source counts, votes, positions, Perspectives, or Pulse.

## FeedImpression meaning

`FeedImpression` means **client-reported qualified Story-card exposure on
HOME_FEED**. It does not mean API delivery, mount, click, read completion,
agreement, vote, recommendation success, or verified human attention. Saved
and detail surfaces do not create it.

The initial v1 proposals—50% visibility for 1000 continuous ms, UUID feed
sessions, once per account/session/Story, batches of 1–20, 32 KiB request,
bounded position/timestamps, a 100-event memory queue, 10-minute queue TTL,
delivery cadence/retry limits, throttling, and 30-day retention—must be
validated by #43 and #51. They are policy values, not timeless principles.
Events contain only `event_id`, `story_id`, `feed_session_id`, zero-based
`position`, `surface`, `policy_version`, and `occurred_at`; account and
`received_at` are server-derived. No device fingerprint, advertising ID, GPS,
copied IP/User-Agent, publisher URL, article text, token, or arbitrary
properties are stored.

As decided by ADR-0011, impression rows preserve `original_story_id` and use a
nullable `SET_NULL` Story relation if a rare hard deletion occurs. This avoids
letting short-retention telemetry block Story cleanup while retaining the
event's originally reported identity. Bookmarks independently use `PROTECT`.

## Mobile boundary and retry ownership

The native fetch stack builds approved relative API routes against one base
origin, times out after 15 seconds, supports caller cancellation, handles empty
204/205 responses, normalizes errors, and validates DTOs. Credentials are
never attached to an absolute foreign URL. Tokens never enter URLs, public
Expo variables, query keys, logs, or diagnostic payloads.

TanStack Query 5.103.1 is the implemented single server-state cache; its React
18/19 peer range covers the checkout's React 19.2.3/Expo 57 stack.
Account-scoped keys isolate all viewer-decorated payloads. React state/context
remains appropriate for session and transient UI. There is no persistent query
cache.

Retry ownership is singular: TanStack Query owns bounded read retry; the auth
session owns at most one refresh/replay; the future impression queue owns its
own delivery retry. Transport does not blindly retry POST.

Issue #44 implements this boundary in `mobile/src/api/` and
`mobile/src/server-state/`. Native fetch accepts approved relative product and
authentication paths only, normalizes transport/API failures and validates the
repository contract fixtures. Runtime failures remain visible failures; test
fixtures never become fallback UI data. Session credential persistence and
single-flight refresh orchestration remain owned by #45, while exposure queue
delivery remains owned by #51.

Native Android/iOS reading behavior is the Phase 3 acceptance target. Web must
continue to compile/render, but production browser CORS and deployment are
deferred. Native refresh credentials use secure platform storage, access
tokens remain in memory, and web credentials are memory-only (ADR-0009).

## Labels and time/count meanings

- “Sources” counts distinct publisher Source IDs in the stated generation or
  current-membership scope; it never means independently verified evidence.
- “Published” is `published_at`; when absent, `first_seen_at` is labelled
  “first seen,” never silently substituted.
- “Synthesized” is generation time, not reporting time.
- “Updating” means a prior coherent synthesis is shown while current
  membership awaits refresh; “Preparing” has no usable synthesis.
- “Context” is generated CONTEXT text; Phase 3 does not promise or invent “why
  it matters,” “key points,” updates/timeline, media, or Pulse values.

## Contract fixtures and consumers

Repository fixtures live in [`../contracts/mobile-feed/`](../contracts/mobile-feed/README.md).
They cover CURRENT multi/single-source responses, missing optional fields,
UPDATING, PREPARING, unavailable/error shapes, pages, and mutation outcomes.
#41 validates News serialization against them; #42/#43 validate Reading
contracts; #44 validates mobile decoders; #47 validates HTTP adapters; #51
owns impression delivery behavior.

## Security, diagnostics, and future boundaries

Logs may carry bounded operation/error codes, durations and counts. They must
not carry credentials, synthesis/source browsing payloads, publisher URLs,
reading histories, or provider diagnostics. Client-reported telemetry is
forgeable and must not be represented as verified attention.

If a later milestone introduces application-owned images, thumbnails,
persistent third-party media caching, image proxies, uploads, or object
storage, a dedicated media/object-storage ADR must precede provider-specific
persistence or caching. Phase 3 adds none of these.
