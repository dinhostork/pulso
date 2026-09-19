# Backend module boundaries

Pulso has one backend codebase and one primary PostgreSQL database. Domain
modules express logical ownership inside that modular monolith; they are not
independent deployable services. This maps
[ADR-0001](../adr/0001-modular-monolith-with-workers.md) into the initial code layout.

## Current packages

| Path | Responsibility |
| --- | --- |
| `backend/config/` | Shared application wiring (`common`), separate development/test settings, URL composition and ASGI/WSGI entry points |
| `backend/api/` | Product HTTP route registry; currently mounts authentication routes under `/api/auth/` |
| `backend/health/` | Unauthenticated liveness/readiness endpoints and bounded dependency probes (issue #5); not part of the product API |
| `backend/accounts/` | Account identity, Django model integration, initial migration and JWT login/logout/refresh/current-user endpoints (issue #6, ADR-0009) |
| `backend/database/` | Shared PostgreSQL extension migration; no product models |
| `backend/diagnostics/` | Temporary Celery/Redis infrastructure diagnostic (issue #4); no product models or domain rules |
| `backend/news/` | News-owned publication persistence (Source, SourceEndpoint, IngestionRun, RawArticle, Article) and the Story Engine (Story, StoryArticle, ArticleEmbedding, StoryEmbedding, ArticleStoryProcessing, Topic, Entity, StoryTopic, StoryEntity and the StorySynthesis tables); `adapters/` fetch and parse feeds and implement the embedding, extraction and synthesis ports, `application/` runs ingestion, processing, Story matching, refresh and operations, `domain/` holds pure rules, `tasks.py` orchestrates Celery work, `logging.py` supplies context, and management commands provide the operator surface. See the [News Core architecture](news-core.md) and the [Story Engine architecture](story-engine.md). |

Accounts uses Django's `AbstractUser` and a database-generated `BigAutoField`
primary key. Future relationships use `settings.AUTH_USER_MODEL` in model fields
and `get_user_model()` at runtime. Username/password and permissions retain
Django behavior. `accounts/views.py` and `accounts/serializers.py` are thin
DRF adapters around `django.contrib.auth.authenticate()` and
`djangorestframework-simplejwt`, not a separate application-service layer:
there is still no product use case beyond authenticating an existing
account, so no additional indirection was introduced for its own sake. No
profile, position or registration contract is introduced.

News's module-local `application/` package is the boundary for its ingestion
use cases: `ingest.py` fetches an endpoint and stores raw revisions, and
`process.py` normalizes one revision and applies the deduplication decision.
The rules those services coordinate stay in `domain/` as pure functions —
`urls.py`, `fingerprints.py`, `identity.py`, `normalization.py` and `dedup.py`
import no Django, no models and no settings. `tasks.py` implements Celery
orchestration, `management/commands/` provides operator commands, and
`logging.py` supplies structured context. The Story Engine follows the same
layering: `story_ports.py` defines the embedding, extractor and synthesizer
protocols; `embeddings.py`, `story_candidates.py`, `story_verification.py`,
`story_matching.py`, `story_processing.py`, `story_refresh.py`,
`story_enrichment.py`, `story_synthesis.py` and `story_inspection.py` coordinate
the use cases; `domain/stories.py`, `story_matching.py`, `event_anchors.py`,
`embeddings.py` and `enrichment.py` hold its pure rules; `adapters/` supplies
the local and deterministic embedding providers, the rule-based extractor and
the extractive synthesizer. See the [Story Engine architecture](story-engine.md).
The route registry is the integration point for future HTTP adapters; it must
not accumulate domain rules. No Story HTTP API exists yet.

## Dependency direction

```text
HTTP / worker adapters → application use cases → domain rules / ports
                                               ↑
                                    infrastructure implements ports
```

HTTP adapters parse requests and translate results. Application services
coordinate use cases and transactions. Domain rules remain independent of DRF,
Celery, Redis and provider SDKs. Infrastructure supplies persistence and external
integrations through ports when the use case needs that separation.

The current Account model is a Django persistence/authentication integration;
it is not a framework-independent domain layer. Future product rules should not
be placed in serializers, views, tasks or settings merely because those files
are convenient. Do not add wrapper repositories around Django without a concrete
boundary to protect. Cross-module writes go through the owner's application
interface rather than another module's internal models.

## Logical ownership

| Module | Owns | Current state |
| --- | --- | --- |
| Accounts | Stable user identity and account authentication foundation | User model plus JWT login/logout/refresh/current-user endpoints ([ADR-0009](../adr/0009-jwt-mobile-authentication.md)); no registration or profile fields |
| News | Source publications, Articles, Stories and source-grounded factual synthesis | News Core: RSS/JSON Feed ingestion, deterministic normalization and deterministic deduplication ([ADR-0010](../adr/0010-article-identity-and-deduplication.md)). Story Engine: versioned Article/Story embeddings, pgvector candidate retrieval, deterministic matcher v2, Article → Story associations, snapshot/compare-and-swap Story refresh, Topics and Entities, extractive source-grounded synthesis, Celery processing, reconciliation, reprocessing and operator commands ([Story Engine architecture](story-engine.md)). Story-side state is derived and rebuildable and never cascades into Article provenance. No Story feed or detail API |
| Opinion | Human Opinions, declared positions, derived Perspectives and Pulse | Planned; no package or models |
| Recommendation | Discovery ranking, interests and ranking signals | Planned; no package or models |
| Moderation | Moderation decisions and eligibility policies, coordinated with content owners | Planned; policies and interfaces remain undecided |
| Notifications | Notification delivery coordination and provider integration | Planned; no package or models |

These ownership boundaries preserve the accepted decisions:

- [ADR-0002](../adr/0002-postgresql-pgvector.md): PostgreSQL is the shared primary
  datastore; pgvector lives alongside relational data and holds the Story
  Engine's Article and Story embeddings. Sharing a database
  does not transfer table ownership. The root Compose stack provides local
  PostgreSQL/pgvector; the `database` infrastructure app enables `vector`
  through a Django migration.
- [ADR-0003](../adr/0003-article-not-equal-story.md): News keeps Articles distinct
  from Stories and preserves source provenance.
- [ADR-0004](../adr/0004-ai-is-not-a-source.md): generated output is derived;
  Articles and human Opinions remain authoritative inputs. Provider integrations
  must not become sources of factual truth.
- [ADR-0006](../adr/0006-opinion-not-equal-perspective.md): Opinion owns the
  distinction between human-authored Opinions and derived Perspectives.
- [ADR-0007](../adr/0007-pulse-counts-unique-users.md): stable account references
  support one active position per user per Story later. Account identity alone
  does not implement positions, unique-person verification or Pulse counting.
- [ADR-0008](../adr/0008-recommend-stories-not-truth.md): Recommendation consumes
  domain data to rank discovery; it cannot rewrite Story facts, source evidence,
  Perspectives or Pulse state.
- [ADR-0010](../adr/0010-article-identity-and-deduplication.md): News owns
  publication identity. A canonical URL is globally unique and belongs to one
  Source; another Source's delivery of it is recorded as a conflict and never
  re-attributes the Article. Exact-content republications stay separate
  Articles linked with `duplicate_of`. Deduplication is not Story clustering,
  and no semantic similarity takes part in it.

## Background execution

Under [ADR-0005](../adr/0005-asynchronous-processing-with-celery.md), the API,
Celery worker and optional Celery Beat scheduler share the same backend
codebase and image (`config/celery.py`, wired from `config/__init__.py`) as
separate Compose services/processes. Task adapters invoke application
services, dispatch after successful database commit where needed, and account
for repeat execution: `CELERY_TASK_ACKS_LATE`/`CELERY_TASK_REJECT_ON_WORKER_LOST`
are both enabled, so persistent task effects must be idempotent. Redis and
task-result metadata are not authoritative domain storage. `diagnostics/` is
the issue #4 diagnostic app proving this infrastructure with a harmless task;
News adds product tasks and Beat schedules; `diagnostics/` itself adds no product
behavior. See the worker infrastructure section of [backend/README.md](../../backend/README.md).

## Test isolation

The opt-in `postgres-test` Compose service is a disposable PostgreSQL/pgvector
instance, separate from the development database and volume. pytest-django
applies the same migrations in `test_pulso` and destroys it after each default
session. This verifies persistence without creating additional domain modules.
