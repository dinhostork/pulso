# Backend bootstrap

> Setting up the whole project (backend + mobile) for the first time? Start
> at [`docs/development.md`](../docs/development.md) instead; come back here
> for backend-specific depth.

## Runtime and dependencies

Use Python **3.14.4** (`.python-version`) and **uv 0.12.13**. The supported Python
minor is 3.14. Django is constrained to the 5.2 LTS series, with DRF and Psycopg 3.
`pyproject.toml` declares dependencies; `uv.lock` records exact resolved versions
and distribution hashes. Both files belong in version control. The backend is
an application, not a separately built Python distribution.

Install uv using the [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/).
From the repository root:

```bash
cd backend
uv sync --locked
cp .env.example .env
uv run --locked --env-file .env python manage.py check
uv run --locked --env-file .env python manage.py makemigrations --check --dry-run
```

`uv sync --locked` checks that the lockfile matches the manifest and installs it
without updating resolution. See the [uv lock workflow](https://docs.astral.sh/uv/concepts/projects/sync/).
Use `uv lock` only for intentional dependency changes and review the resulting
lockfile. Python support for Django 5.2 is described in its
[release notes](https://docs.djangoproject.com/en/5.2/releases/5.2/).

## Configuration

Django reads process environment variables; it does not load `.env` implicitly.
The commands above use uv to load the local file. ASGI/WSGI launchers must receive
the same environment. Existing process variables take precedence over values
from `--env-file`; unset conflicting exported variables before using the example
(e.g. `env -u DEBUG uv run --locked --env-file .env python manage.py check`).
`.env.example` contains public local-only values and is
safe to version; `.env` is ignored.

| Setting | Contract |
| --- | --- |
| `SECRET_KEY` | Required; never logged by configuration errors |
| `DEBUG` | Optional, defaults to `false`; accepts only `true` or `false` (case insensitive) |
| `ALLOWED_HOSTS` | Required comma-separated explicit hosts; no wildcard or empty entries |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST` | Required, nonempty values |
| `POSTGRES_PORT` | Required integer from 1 to 65535 |

Local development must explicitly set `DEBUG=true`. With debug disabled,
short/low-diversity and recognized example secret keys are rejected, and session
and CSRF cookies require HTTPS. Generate a secret key before any deployment;
the example values are not deployment credentials. These settings are a
bootstrap, not a complete production deployment configuration.

## News ingestion

See the [News Core architecture](../docs/architecture/news-core.md) for the
model, lifecycle and boundaries.

### Configuration

News source endpoints are validated against all resolved IP addresses when
saved, and the shared HTTP fetcher checks each request and redirect again.
Only HTTP(S) targets and RSS/XML/JSON/text feed media types are accepted;
`text/html` is refused. RSS/Atom and JSON Feed syndication use the same bounded
fetcher. JSON Feed needs no credentials in v0.2.0.

`adapter_config["credential_env"]` is reserved for future credentialed source
adapters. It stores the **name** of an environment variable (for example,
`NEWS_PROVIDER_API_TOKEN`), never the secret value. The JSON Feed adapter ignores
this setting and sends no credential header.

| Setting | Default | Behavior |
| --- | --- | --- |
| `NEWS_FETCH_ALLOW_PRIVATE_NETWORKS` | `false` | Only environment override. `true` permits private/loopback targets for controlled local tests; normal development and deployment should keep it `false`. |
| `NEWS_FETCH_MAX_RESPONSE_BYTES` | 5 MiB | Rejects oversized `Content-Length` before reading; decoded gzip/deflate output is bounded during decompression. Unknown-length or compressed streams are refused when decoded content reaches the cap. |
| `NEWS_FETCH_MAX_REDIRECTS` | 5 | Each hop is checked; HTTPS-to-HTTP downgrade is refused. |
| `NEWS_FETCH_CONNECT_TIMEOUT_SECONDS` | 5 | Connection timeout. |
| `NEWS_FETCH_READ_TIMEOUT_SECONDS` | 15 | Per-read timeout. |
| `NEWS_FETCH_WRITE_TIMEOUT_SECONDS` | 5 | Write timeout. |
| `NEWS_FETCH_POOL_TIMEOUT_SECONDS` | 5 | Connection-pool timeout. |
| `NEWS_CONTENT_FINGERPRINT_MIN_CHARS` | 200 | Minimum normalized fingerprint-input characters before exact content equality links a republication (ADR-0010). Shorter material always creates a separate Article. |

The last six fetch settings are fixed application constants, not environment
knobs, and so is the fingerprint threshold.
`config.settings_test` enables the private-network exception for the
loopback fixture server; policy tests exercise both flag values with fake DNS. The override does not disable scheme, redirect, size,
media-type or timeout checks. DNS answers are checked before connecting, but
the HTTP client resolves again at connection time. DNS rebinding in that gap
remains a known risk; no IP pinning is implemented here.

### Pipeline

Each endpoint fetch creates an `IngestionRun`: `RUNNING` while executing, then
`SUCCEEDED`, `PARTIAL`, `NO_CHANGE`, or `FAILED`. Expected fetch failures
record safe error kind, HTTP status, and whether a future caller may retry
them. Its counters are:

| Counter | Meaning |
| --- | --- |
| `items_received` | Items the adapter returned, including the ones it rejected. |
| `items_rejected` | Adapter/intake rejections: unparseable entries, missing identity, and items over the per-run limit. |
| `raw_created` / `raw_changed` / `raw_unchanged` | New raw revisions, superseding revisions, and deliveries whose payload hash already existed. |
| `items_processed` | Processing invocations that returned without raising. |
| `items_failed` | Processing invocations isolated after an unexpected exception. |
| `identity_duplicates` | Revisions that resolved to an existing Article with nothing to change (ADR-0010). |
| `content_duplicates` | New Articles linked with `duplicate_of` to an earlier Article with the same content fingerprint. |
| `raw_rejected` | RawArticles rejected while processing, including normalization rejections. Distinct from `items_rejected`. |
| `source_identity_conflicts` | Rejected deliveries whose canonical URL is already owned by another Source. |

A run with any `items_rejected`, `items_failed` or `raw_rejected` finishes
`PARTIAL`. A cross-Source canonical URL conflict never changes the existing
Article: the delivery is kept as a rejected `RawArticle` linked to that Article
for review, and one WARNING carries both Source slugs, both endpoint ids and
the canonical URL with no title or body text.

One run accepts at most 500 fetched items. Each stored `RawArticle.payload` is
valid canonical JSON capped at 256 KiB; oversized content and metadata are
reduced deterministically before its hash is calculated. Pending rows from an
earlier interrupted run are replayed without changing their receipt provenance.
Each pending row is then normalized and deduplicated in place. The application
service does not schedule work or execute retries; `news.tasks` owns those steps.

### Worker and Beat

Ingestion runs through the existing Celery worker and the optional Beat
scheduler (ADR-0001, ADR-0005). `news/tasks.py` holds only orchestration: each
task resolves identifiers, calls one application function and translates the
operational metadata it returns. No normalization, canonicalization, identity
or deduplication rule lives in the task layer.

| Task | Responsibility | Options |
| --- | --- | --- |
| `news.tasks.ingest_endpoint(endpoint_id, trigger="SCHEDULE")` | Fetch one endpoint, store raw revisions, process them | `bind=True`, `max_retries=3`, `soft_time_limit=150`, `time_limit=180` |
| `news.tasks.process_raw_article(raw_id)` | Process one stored revision | `bind=True`, `max_retries=3` |
| `news.tasks.poll_due_endpoints()` | Dispatch ingestion for every due, active endpoint | no retry |
| `news.tasks.reconcile_pending_raw_articles()` | Re-dispatch processing for rows left `PENDING` | no retry |

The global 30 s soft / 60 s hard task limits stay as they are for every other
task; a fetch plus one batch of processing needs more, so `ingest_endpoint`
raises its own limits to 150 s / 180 s.

### Retry policy

The task never inspects HTTP status codes or transport exceptions. The fetcher
decides whether a failure is retryable and parses `Retry-After`; the
application records the run and returns `will_retry`; the task decides when an
allowed retry is scheduled:

| Failure | Behavior |
| --- | --- |
| `TIMEOUT`, `NETWORK`, 5xx, 408 | Up to 3 retries, countdown `30 · 2^n` seconds plus up to 10% jitter, capped at 600 s |
| `RATE_LIMITED` with `Retry-After` | One retry per attempt using the server's delay (the fetcher already bounds it to 900 s, so a rate-limit delay may legitimately exceed the 600 s cap that applies to our own backoff) |
| `HTTP_STATUS` 404, `BLOCKED_TARGET`, `MALFORMED`, … | No retry; one `FAILED` run |

Four executions are therefore possible at most. `attempt` 0 records the
original `trigger` (`SCHEDULE` or `MANUAL`); every later attempt records
`RETRY`. `IngestionRun.will_retry` is only true when a retry will actually
happen, so the last attempt of an exhausted budget is stored as `false`: the
task passes its remaining budget to the application as `retry_allowed`, and
`RunSummary.retry_after` carries the rate-limit delay transiently, without a
database column. Each scheduled retry logs one WARNING with `endpoint_id`,
`task_id`, `attempt`, `countdown`, `error_kind` and `run_id` — identifiers
only, never payloads, headers or content.

### Scheduling

| Setting | Default | Behavior |
| --- | --- | --- |
| `NEWS_INGESTION_ENABLED` | `true` | Gates all three News Beat entries. Independent of `CELERY_DIAGNOSTIC_BEAT_ENABLED`; neither flag affects the other's entries. |
| `NEWS_POLL_DISPATCH_INTERVAL_SECONDS` | `300` | How often Beat runs `poll_due_endpoints`. Must be a positive integer; `0`, negatives and non-integers are rejected with `ImproperlyConfigured`. |
| `LOG_LEVEL` | `INFO` | Level of the `pulso` JSON log tree (see [observability](#observability-and-run-history)); Django and Celery loggers are unaffected. |

With the defaults, `CELERY_BEAT_SCHEDULE` contains `news-poll-due-endpoints`
(every `NEWS_POLL_DISPATCH_INTERVAL_SECONDS`), `news-reconcile-pending`
(every 3600 s) and `news-prune-runs` (weekly, see
[run retention](#run-retention)). `config/settings_test.py` fixes both flags off and keeps
`CELERY_BEAT_SCHEDULE = {}`, so no test run can schedule recurring work
regardless of the developer's environment.

`poll_due_endpoints` dispatches an endpoint when it is active, its Source is
active, and either it has never run or its latest run started at least
`fetch_interval_seconds` ago. It skips an endpoint whose latest run is
`RUNNING` and younger than 180 s; an older `RUNNING` row does not block the
endpoint once its interval has elapsed. One annotated query reads the latest
run per endpoint, and dispatch happens in `transaction.on_commit`, never
inside the selecting transaction. Each poll logs
`dispatched`, `skipped_not_due`, `skipped_running` and `skipped_inactive`;
`skipped_inactive` counts endpoints that are themselves inactive **or** whose
Source is inactive.

`reconcile_pending_raw_articles` dispatches `process_raw_article` for `PENDING`
rows older than 10 minutes, at most 1000 per run, oldest receipt first
(`created_at`, then `pk`). It never touches a row's payload, hash, endpoint,
run or timestamps: only processing is retried, through the same application
function the ingestion loop uses.

Beat's schedule file (`/tmp/celerybeat-schedule` in the container) is
disposable, and losing it on restart is harmless: `poll_due_endpoints` decides
what is due from persisted PostgreSQL state, and re-running ingestion for an
endpoint is idempotent (identical payloads produce no new revision and no new
Article). Nothing depends on Beat's own bookkeeping surviving.

### Operator commands

The operator surface is `manage.py`; there is no HTTP endpoint for triggering
ingestion and Django admin is deliberately not enabled.

```bash
# Sources
python manage.py news_source add --slug example --name "Example News" \
    [--homepage-url https://example.com] [--default-language en]
python manage.py news_source list
python manage.py news_source enable <slug>
python manage.py news_source disable <slug>

# Endpoints
python manage.py news_endpoint add --source example --kind RSS \
    --url https://example.com/feed.xml [--interval 900] [--adapter-config '{}']
python manage.py news_endpoint list [--source <slug>]
python manage.py news_endpoint enable <id|url>
python manage.py news_endpoint disable <id|url>

# Ingestion
python manage.py news_ingest --endpoint <id|url>            # runs in this process
python manage.py news_ingest --endpoint <id|url> --async     # queues the task

# Reprocessing
python manage.py news_reprocess --raw <id>
```

`--kind` accepts `RSS` and `JSON_FEED`. `news_endpoint add` goes through the
model's own `full_clean()`/`save()`, so an endpoint can only be created if it
passes the same rules the model enforces everywhere: http(s) only, the target
policy of #13, a JSON-object `adapter_config`, and rejection of secret-like
configuration keys (`adapter_config` stores the *name* of an environment
variable, never a secret value). `enable`/`disable` change only `is_active`
and deliberately do not re-resolve or re-validate the URL.

`--endpoint` takes a numeric id or an exact endpoint URL; a partial URL never
matches, and an unknown identifier is a `CommandError`. The synchronous path
prints `run_id`, `status` and every counter and records `trigger=MANUAL`; the
`--async` path prints the queued task id and its first execution is still
`MANUAL` (only its retries become `RETRY`).

`news_reprocess` accepts only a `REJECTED` revision. It resets exactly the
processing result (`status`, `outcome`, `rejection_reason`, `processed_at`,
`article`) in one committed statement, then calls the ordinary application
processor; receipt provenance — payload, hash, endpoint, run, external
identity, `fetched_at`, `supersedes` — is never rewritten.

### Optional demo data (requires external network)

`news/fixtures/demo_sources.json` configures three public, credential-free
endpoints for manual exploration: BBC News World (RSS), The Django weblog
(RSS) and Daring Fireball (JSON Feed). It is **optional**, depends on the
external network, and is never used by the automated tests; those third-party
endpoints' availability and formats are outside Pulso's control.

```bash
python manage.py loaddata news/fixtures/demo_sources.json
python manage.py news_source list
python manage.py news_endpoint list
python manage.py news_ingest --endpoint <id>
```

### Observability and run history

Ingestion is diagnosable from two places only: the `IngestionRun`/`RawArticle`
rows in PostgreSQL and the structured logs. There is no metrics platform in
this milestone — counters live in the database, and the log format is chosen so
a future log shipper needs no change.

**Logs are operational metadata, not a copy of article content.**

### JSON-lines format

Every record in the `pulso` logger tree is one JSON object on one line, written
by `config/logging.py::JsonLinesFormatter` (a stdlib `logging.Formatter`
subclass; no logging dependency). Base fields on every record:

| Field | Meaning |
| --- | --- |
| `timestamp` | UTC ISO-8601, from the record's creation time |
| `level` | `DEBUG` … `CRITICAL` |
| `logger` | e.g. `pulso.news.ingest`, `pulso.news.process`, `pulso.news.stories`, `pulso.news.tasks`, `pulso.diagnostics` |
| `message` | the human-readable message, with `%`-parameters already applied |

Beyond those, the formatter emits **only** the fields in its `SAFE_FIELDS`
allowlist: identifiers, statuses, counts and reasons. `record.__dict__` is
never serialized wholesale, so an accidental
`extra={"payload": ...}` at some future call site cannot leak publication
content, HTTP headers, credentials or `adapter_config` into the logs. Values
are primitives (anything else is reduced to its type name) and bounded to 512
characters. An exception contributes its class name only — never its arguments
or traceback, which may carry transport data; Django and Celery keep their own
loggers, levels and tracebacks untouched.

Story processing (#33) adds identifiers, decisions, numbers and states only:
the context fields `article_id`, `story_id`, `model_key`, `task_id`, `attempt`
and `trigger`, and the per-step fields `step`, `failed_step`,
`candidate_count`, `chosen_story_id`, `distance`, `threshold`, `decision`,
`match_reason`, `match_rule`, `matcher_key`, `duration_ms`, `member_count`,
`article_count`, `source_count`, `topic_count`, `entity_count`,
`synthesis_source_count`, `refresh_state`, `refresh_reason` and `error_kind`.
Their meaning per step is in [Story observability](#story-observability).
Titles, descriptions, bodies, payloads, synthesis text, prompts, provider
responses and vectors are never allowlisted.

`LOG_LEVEL` (default `INFO`, one of `DEBUG`, `INFO`, `WARNING`, `ERROR`,
`CRITICAL`) sets the level of the `pulso` tree; an unrecognized value is an
`ImproperlyConfigured` error at startup rather than silently dropped records.
The tree uses `propagate=False`, so structured records are not duplicated as
plain text through the root logger.

### Stable ingestion context

`news/logging.py::ingestion_logger(**context)` returns a `LoggerAdapter` that
merges its context into every record it emits, so one ingestion execution can
be followed without correlating by timestamp. A call site's own fields are kept
and win on a key collision, so a per-item `position` is never masked by the run
context. The context is a snapshot of primitives: formatting a record never
queries the database.

| Field | Source |
| --- | --- |
| `source_id`, `source_slug` | the endpoint's Source |
| `endpoint_id`, `adapter` | the `SourceEndpoint` and its kind |
| `run_id`, `task_id`, `attempt`, `trigger` | the `IngestionRun` being executed |

Every record of one `ingest_endpoint` execution carries all eight: run start,
adapter and intake rejections, payload truncation, each per-item processing
outcome, processing failures and the run summary.

The ingestion loop passes its own logger into `process_raw_article`, so a
`PENDING` row replayed from an interrupted earlier run is logged against the
run that is **processing** it now. `RawArticle.ingestion_run` remains untouched
receipt provenance — the two are deliberately different facts. A task-driven
`process_raw_article` call (reconciliation) has no current run, so it logs the
Source/endpoint context without inventing run fields.

Key events: `News ingestion run started`, `News item rejected by adapter`,
`News item rejected during intake`, `News ingestion item limit applied`,
`News item payload truncated`, `Raw article processed`,
`News raw processing failed`, `News ingestion retry scheduled` and
`News ingestion run finalized`. The summary record mirrors the stored result:
`status`, `error_kind`, `http_status`, `will_retry`, `duration_ms` and all
eleven counters.

`Raw article processed` is the uniform per-revision event — `state`
(`PROCESSED`/`REJECTED`/`LOCKED`/`SKIPPED`), `outcome`, `rejection_reason` and
`article_id` — emitted for every row whatever the result. A rejection or an
identity conflict additionally keeps its own WARNING from #16/#17, so an
operator can alert on those without parsing every per-item event.

### Inspecting runs

```bash
python manage.py news_runs                      # 20 most recent, newest first
python manage.py news_runs --last 5
python manage.py news_runs --endpoint 12
python manage.py news_runs --endpoint https://example.com/feed.xml
python manage.py news_runs --stale
```

Rows are ordered `started_at` descending with `pk` descending as a
deterministic tie-breaker, and limited in SQL. `--endpoint` takes a numeric id
or an exact endpoint URL, never a partial match. The table shows status,
trigger, attempt, every counter, error kind, duration and both timestamps.

A run is **stale** when it is still `RUNNING` and started more than
`NEWS_STALE_RUNNING_SECONDS` (180 s) ago. That is one fixed operational
constant shared with the poll dispatcher's in-flight guard, so
`news_runs --stale` and scheduling can never disagree. Staleness is decided by
`started_at`, and a finalized run is never stale whatever its age.
`--stale` exits **1** when it finds any (after printing them) and **0** when it
finds none, so it can be used directly in a monitoring script.

### Run retention

```bash
python manage.py news_prune_runs                # 30-day default
python manage.py news_prune_runs --days 7
```

Only **finalized** runs are deleted: a run with a real `finished_at` that is
older than the window. Retention is measured from `finished_at`, never from
`started_at`, so a long execution is never expired while it is still running,
and a `RUNNING` run is never deleted whatever its age — those are reported by
`news_runs --stale` and an operator decides. The command prints the number of
rows deleted and the window used.

`RawArticle` and `Article` rows are never deleted. `RawArticle.ingestion_run`
becomes `NULL` through the existing `on_delete=SET_NULL` rule, in one bulk
`UPDATE`: the link to discarded operational history goes away, receipt
provenance and publications stay.

The rule lives in `news/application/operations.py::prune_ingestion_runs`, so
the command and the optional weekly schedule apply exactly the same logic. When
`NEWS_INGESTION_ENABLED` is true, Beat also runs `news-prune-runs` every
604800 s (weekly) with the same 30-day default.

## Story Engine

The v0.3 Story Engine groups committed Articles into event-level Stories and
keeps each Story's derived state coherent with its membership. The
[Story Engine architecture](../docs/architecture/story-engine.md) is the design
reference: persistence, invariants, failure, concurrency and security model,
and the corpus evidence behind every threshold. The sections below are the
operational reference, in pipeline order:

| Stage | Section | Entry point |
| --- | --- | --- |
| Article vector | [Story embeddings](#story-embeddings) | `embed_article` |
| Nearby Stories | [Story candidate retrieval](#story-candidate-retrieval) | `find_candidates` |
| Join or create (matcher v3) | [Story matching](#story-matching) | `match_article` |
| Tasks, reconciliation, reprocessing | [Story processing](#story-processing) | `embed_article_story`, `match_article_story`, `reconcile_article_stories` |
| Topics and Entities | [Story Topics and Entities](#story-topics-and-entities) | `compute_story_enrichment` |
| Synthesis | [Story synthesis](#story-synthesis) | `compute_story_synthesis` |
| Coherent derived generation | [Story refresh](#story-refresh) | `refresh_story`, `refresh_story_task` |
| Logs and operator commands | [Story observability](#story-observability) | `news_stories`, `news_story`, `news_story_explain`, `news_story_backlog` |

Automatic processing is off unless `NEWS_STORY_PROCESSING_ENABLED=true`; every
stage can also be run synchronously from `manage.py`. The quality gates and
real-worker smoke are described under
[backend quality and isolated tests](#backend-quality-and-isolated-tests) and
[automated real-broker smoke check](#automated-real-broker-smoke-check).

## Story embeddings

Story matching compares semantic vectors of Articles and Stories (issue #25).
They are **derived data** ([ADR-0004](../docs/adr/0004-ai-is-not-a-source.md)):
stored in PostgreSQL/pgvector as `ArticleEmbedding` and `StoryEmbedding`,
rebuildable at any time, and generating them never writes to `Article`,
`RawArticle`, `Story` or `StoryArticle`. Deleting every embedding row and
running the services again reproduces them.

The application sees only the `EmbeddingProvider` protocol
(`news/application/story_ports.py`); `news/application/embeddings.py`
provides `embed_article`, `embed_articles` and `embed_story`, which
[Story processing](#story-processing) calls for each Article, and
`compute_story_embedding`, which [Story refresh](#story-refresh) uses to rebuild
a Story vector from its members' current text.

### `model_key`

Every stored vector records the identity of the model that produced it as
`model_key = provider:model@revision`, plus its `dimension`. Vectors are only
comparable within one `model_key`. A row is created once per
(`article`/`story`, `model_key`) and later `embed_*` calls return it
unchanged; only [Story refresh](#story-refresh) updates a Story's row for the
configured model in place. A new model or revision gets its own rows and
never replaces the old ones. A vector
whose length differs from the model's dimension, or from the dimension already
stored for that `model_key`, fails with an explicit error naming both
dimensions: nothing is truncated, padded or stored. Error messages never
include Article text.

A Story vector is the unit-length mean of its member Articles' vectors, and
`member_count` records how many members it was computed from.

### Settings and input bounds

| Setting | Value | Meaning |
| --- | --- | --- |
| `NEWS_EMBEDDING_PROVIDER` | `local` (tests: `deterministic`) | Provider name only; it takes no options, so no credential can be configured for it. |
| `NEWS_EMBEDDING_MAX_BATCH` | `32` | Most texts sent in one provider call. |
| `NEWS_EMBEDDING_MAX_INPUT_CHARS` | `2000` | Most characters embedded per Article. |

These are fixed application constants in `config/common.py`, not environment
variables. An Article's input is its title, then description, then body text,
each with whitespace collapsed, blank parts omitted and parts separated by a
blank line. The result is cut to `NEWS_EMBEDDING_MAX_INPUT_CHARS` code points
with trailing whitespace removed, so the title always comes first
(`news/domain/embeddings.py`).

### Providers

- `deterministic`: an in-repository double that hashes words with SHA-256 into
  64 dimensions. It needs no network or model file and returns identical
  vectors in every process. The default test suite uses only this provider.
- `local`: [fastembed](https://github.com/qdrant/fastembed) running
  `BAAI/bge-small-en-v1.5` (384 dimensions, MIT licence) on the CPU through
  ONNX Runtime. It is free and sends nothing anywhere. The weights (about 65 MB)
  are pinned to one Hugging Face revision, so its `model_key` is
  `fastembed:BAAI/bge-small-en-v1.5@52398278842e`.

The local model is **optional**. `fastembed` lives in the `embeddings`
dependency group, which `uv sync --locked`, CI and the Docker image do not
install. Importing Django or starting the backend never loads or downloads it.
The adapter only loads files already on disk; without the group or the files,
embedding fails with a clear error instead of downloading. Story processing
records that as a permanent `PROVIDER_FAILED`, so a stack that enables
`NEWS_STORY_PROCESSING_ENABLED` needs the group and the one-off download below.

To opt in, from `backend/`:

```bash
uv sync --locked --group embeddings
# One-off download into the Hugging Face cache (HF_HOME, default ~/.cache/huggingface),
# then an offline check that prints the model_key and dimension:
uv run --locked --group embeddings python -m news.adapters.local_embeddings
# The opt-in test runs with the public-DNS guard active, proving offline use:
uv run --locked --group embeddings pytest -m local_embedding
```

The `local_embedding` test is excluded from the default `pytest` run, just as
`celery_smoke` is. A later plain `uv sync --locked` removes the group again.

## Story candidate retrieval

`news.application.story_candidates.find_candidates(article_id, model_key)`
returns, nearest first, the existing Stories that could plausibly be the same
event as one Article (issue #27). It does not decide a match, and it only
reads: it creates no Story, association or embedding and changes no status.

A candidate is a frozen `StoryCandidate` holding `story_id`, `distance`
(pgvector cosine distance), `member_count`, `last_article_published_at`,
`language` and `status`. It never holds a model instance or any text. An
Article with no stored embedding for `model_key` raises
`MissingArticleEmbedding`. An empty tuple always means "no Story within
bounds", never "not embedded".

| Setting | Value | Retrieval bound |
| --- | --- | --- |
| `NEWS_STORY_CANDIDATE_LIMIT` | `10` | Most candidates returned. |
| `NEWS_STORY_CANDIDATE_MAX_DISTANCE` | `0.5` | Largest cosine distance returned. |
| `NEWS_STORY_CANDIDATE_WINDOW_HOURS` | `168` | A Story's member publication range must overlap this many hours on either side of the Article's publication time. |

These are **recall bounds, not the match threshold**. They are deliberately
generous so the right Story is never lost, and they cap the work and memory
one Article can cause. The matching decision applies its own, stricter
threshold to the returned distances. If changing a retrieval bound changes
which Story an Article joins, the threshold has leaked into retrieval.

Filters: same `model_key` (vectors from different models are never compared),
`ACTIVE` Stories only, the same primary language subtag (`en` matches
`en-GB`), and at least one member. Times are publication times, using
`first_seen_at` when `published_at` is missing, for the Article and every
member alike. `Story.created_at` and the current clock are never used.

The query is a single PostgreSQL statement: it computes the distance, applies
the filters, orders by (`distance`, `story_id`) and applies the limit. No
vector is loaded into Python. The scan is **exact**: pgvector's approximate
indexes (HNSW/IVFFlat) need a fixed vector dimension, which the
multi-model `StoryEmbedding.vector` column does not have. They would also make
results approximate and tie order unstable. The selective predicates already
have ordinary indexes: `StoryEmbedding.model_key`, `Story.status` and the
StoryArticle membership index. Revisit a per-model approximate index only
when Story volume makes the exact scan measurably slow.

## Story matching

`news.application.story_matching.match_article(article_id)` places one
embedded Article in a Story (issues #28, #36, #38). It retrieves candidates
(above), passes an immutable snapshot and the candidates to the pure rule
`news.domain.story_matching.decide_story_match`, and persists the result in
one transaction:

- `MATCH`: one primary `StoryArticle` with `method=MATCHED`.
- `CREATE_NEW_STORY`: one `ACTIVE` Story, its primary `StoryArticle`
  (`method=CREATED_STORY`) and its first `StoryEmbedding`. One call creates at
  most one Story.

A candidate is **compatible** when it is `ACTIVE`, has the same primary
language subtag, and the Article's event time lies within
`NEWS_STORY_MATCH_MAX_TIME_GAP_HOURS` of the candidate's member publication
range (zero inside it). Compatible candidates are ordered by
(`distance`, Story id), then:

1. **Primary rule** (`match_rule=PRIMARY_DISTANCE`, reason
   `WITHIN_THRESHOLD`): the nearest compatible candidate is joined when its
   cosine distance is at most `NEWS_STORY_MATCH_MAX_DISTANCE`. Nothing else is
   read.
2. **Secondary event verifier** (`match_rule=SECONDARY_EVENT_VERIFY`, reason
   `VERIFIED_SAME_EVENT`): otherwise each compatible candidate up to
   `NEWS_STORY_MATCH_SECONDARY_MAX_DISTANCE` is verified in that order, and the
   first verified one is joined. Verification requires:
   - English (`ANCHOR_LANGUAGES`); other languages go straight to rule 3;
   - one of the candidate's `NEWS_STORY_MATCH_VERIFY_MAX_MEMBERS` most recent
     members itself within `NEWS_STORY_MATCH_SECONDARY_MAX_MEMBER_DISTANCE`
     (0.22) of the Article, so a Story vector cannot pull in a report that no
     member resembles closely. The candidate bound and this member bound are
     separate since revision 3 (#38): a drifted Story vector may be far out in
     the band while one member is a close report of the event, and a member
     that only shares the event's war or topic sits near the top of the band;
   - at least one proper name in common. `news/domain/event_anchors.py`
     derives names from capitalization alone: words capitalized at every
     occurrence and used mid-sentence in the body, excluding English function
     words and calendar names. No named-entity model and no word lists of
     places or topics are involved.
3. Otherwise a new Story is created, with reason `NO_CANDIDATES`,
   `NO_COMPATIBLE_CANDIDATE`, `ABOVE_THRESHOLD` (nothing within the secondary
   bound) or `VERIFICATION_REJECTED` (every candidate in the band failed
   verification). v0.3 still prefers splitting one event over merging two.

The matcher never reads `content_fingerprint`, `duplicate_of`, Source identity
or Topic/Entity rows: it works when enrichment has never run. The secondary
evidence costs three queries per decision, only when the primary rule leaves
it unresolved, and at most 10 candidates × 20 members × 2000 characters;
distances are computed in PostgreSQL.

| Setting | Value |
| --- | --- |
| `NEWS_STORY_MATCH_MAX_DISTANCE` | `0.18` |
| `NEWS_STORY_MATCH_MAX_TIME_GAP_HOURS` | `48` |
| `NEWS_STORY_MATCH_SECONDARY_MAX_DISTANCE` | `0.25` |
| `NEWS_STORY_MATCH_SECONDARY_MAX_MEMBER_DISTANCE` | `0.22` |
| `NEWS_STORY_MATCH_VERIFY_MAX_MEMBERS` | `20` |

Every value and the rule revision are part of `MATCHER_KEY`:

```text
story-match-v3;max_distance=0.18;max_time_gap_hours=48.0;secondary_max_distance=0.25;secondary_max_member_distance=0.22;min_anchors=1;max_members=20
```

Each association stores it, so #29 reconciliation treats every revision 1
(`story-match-v1;max_distance=0.18;max_time_gap_hours=48.0`) and revision 2
(`story-match-v2;max_distance=0.18;max_time_gap_hours=48.0;secondary_max_distance=0.25;min_anchors=1;max_members=20`)
assignment as stale and rebuilds it once through `match_step`. The removed
association's Story vector is first rebuilt without the Article, and the
Article stays in that Story while the current policy still accepts it
(`kept_current_story` in the evidence); otherwise it is decided afresh, which
is what lets reconciliation undo a revision 2 false merge. No data migration
is involved; `0011_widen_matcher_key` only widens the `matcher_key` columns to
255 characters, because revision 3's key no longer fits in 128.

**Why two stages.** On the #26 corpus one cosine threshold cannot separate the
events. Same-event reports reach 0.215 (the Almen flood). Different-event
reports sit at 0.192 (two templated earthquake bulletins for different
places) and at 0.217–0.230 (the dam inquiry against the merged flood Story).
Proper names reject the lookalikes. The member bound rejects the inquiry: it
shares the river's name, but no flood report is closer than 0.268. The
rationale and margins are next to the values in `config/common.py`.

**Why a separate member bound (#38).** A real-world smoke test (BBC World,
Guardian, Al Jazeera English) merged two different events of one war twice
through the secondary verifier: member distance 0.243 and 0.246, one shared
name each. The corpus now holds synthetic equivalents; under revision 2 they
merge too. Over every secondary verification of the expanded corpus, the
farthest same-event nearest member is 0.2152 and the nearest different-event
member sharing a name is 0.2267, so every member bound in [0.2153, 0.2266]
scores 1.000 / 1.000 and 0.22 was chosen. The initial 0.23 hypothesis keeps
the drone merge; lowering the candidate bound instead splits the Almen flood;
two anchors instead of one splits the Varrow, harbor and flood pairs.

**Measured on the #26/#38 corpus** (synthetic regression corpus, local model
`fastembed:BAAI/bge-small-en-v1.5@52398278842e`; not a production quality
estimate). The 26-Article corpus had 12 events; #38 added six Articles and
four events (32 Articles, 16 events, 21 same-event pairs):

| | Articles | precision | recall | false merges | false splits | unassigned | Stories |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Revision 1, matcher alone (#28) | 26 | 1.000 | 0.632 | 0 | 7 pairs | 0 | — |
| Revision 1, full pipeline with refresh (#34 diagnostics) | 26 | 1.000 | 0.789 | 0 | 4 pairs | 0 | 15 |
| Revision 2, matcher alone | 26 | 1.000 | 1.000 | 0 | 0 | 0 | 12 |
| Revision 2, full pipeline with refresh | 26 | 1.000 | 1.000 | 0 | 0 | 0 | 12 |
| Revision 2, matcher alone | 32 | 0.870 | 0.952 | 3 pairs | 1 pair | 0 | 15 |
| Revision 2, full pipeline with refresh | 32 | 0.840 | 1.000 | 4 pairs | 0 | 0 | 14 |
| Revision 3, matcher alone | 32 | 1.000 | 1.000 | 0 | 0 | 0 | 16 |
| Revision 3, full pipeline with refresh | 32 | 1.000 | 1.000 | 0 | 0 | 0 | 16 |
| Revision 3, full pipeline, then every Article reprocessed | 32 | 1.000 | 1.000 | 0 | 0 | 0 | 16 active (+4 archived empties) |

`tests/news/test_story_matching_corpus.py` enforces the matcher-alone numbers,
including convergence of revision 1 splits and of revision 2's false merges
through reconciliation, and their stability under reprocessing. The default suite replays the local model's
recorded corpus vectors offline (`tests/news/recorded_embeddings.py`), because
the hashing test double cannot express the corpus semantics. The opt-in
`local_embedding` run checks the recording against the live model.

Each association records `matcher_key`, the `similarity` (cosine similarity
to the chosen Story, `1 - distance`, whichever rule accepted it; NULL for
`CREATED_STORY`) and an `evidence` object of identifiers, enums and numbers:
the `reason`, the `rule`, the deciding distance, the candidate count, the
nearest five candidates, every secondary `verification` (Story id, distance,
`result`, nearest member distance, members checked, shared-name count; at most
five), the thresholds and the embedding `model_key`. Names themselves are
publication text and are never stored or logged; only their count is.

Replays return the existing primary association. Races are settled by
PostgreSQL uniqueness and a re-read. A chosen Story found `ARCHIVED` under its
row lock is re-decided once. Two same-event Articles matched at the same moment
with no Story yet create two Stories; v0.3 accepts that and leaves convergence
to later reprocessing. Refreshing a Story's embedding as members join, and
archiving Stories left empty, belong to the Story refresh lifecycle, not to
matching.

## Story processing

After News Core commits an Article, Story processing embeds it and matches it
asynchronously (issue #29). It is **derived work**: its failures never change
an `IngestionRun`, its counters, a `RawArticle` outcome or the Article itself.

| Task | Responsibility |
| --- | --- |
| `news.tasks.embed_article_story(article_id)` | Store the Article's embedding for the configured model, then queue matching. |
| `news.tasks.match_article_story(article_id)` | Assign the Article's primary Story (retrieval and matching as above). |
| `news.tasks.reconcile_article_stories()` | Re-dispatch a bounded batch of Articles whose Story state is missing, stale, stuck or retryable. |

Tasks take an Article id only and delegate to
`news/application/story_processing.py`. That module records each Article's
`ArticleStoryProcessing` row: `state` (`PENDING`, `EMBEDDED`, `MATCHED`,
`FAILED`), `attempts`, `error_kind`, a bounded `error_message`, and the two
halves of its pipeline, `embedding_model_key` and `matcher_key`.

**`pipeline_key`** is `<embedding_model_key>|<matcher_key>`. It is computed for
freshness checks, logs and operator output, and is never stored. An Article is
fresh only when it is `MATCHED` under the configured pair and still has its
primary association. Changing the embedding model or the matching policy
(`matcher_key`) makes it stale on its own, and the stored halves show which one
moved. A stale association is replaced when the Article is matched again. A
Story left without members is never a retrieval candidate; marking it for
refresh and archiving it belong to the Story refresh lifecycle.

**Dispatch.** `news/application/process.py` queues `embed_article_story` with
a robust `transaction.on_commit` callback when an Article is created or
updated. A rolled-back transaction queues nothing, and a broker failure is
logged but never reaches ingestion. This dispatch and the Beat entry below are
gated by `NEWS_STORY_PROCESSING_ENABLED` (environment; default `false`; always
`false` in test settings).

**Retries.** The application classifies each failure:

- Transient: `DATABASE_UNAVAILABLE`, `PROVIDER_UNAVAILABLE` (provider timeout
  or connection error), `STORY_NO_LONGER_ACTIVE`. Retried up to 3 times with
  the ingestion backoff (30 s · 2^n, capped at 600 s, up to 10% jitter).
- Permanent: a missing local model or optional group, invalid input, invalid
  output, dimension mismatch, or `MISSING_EMBEDDING`. Recorded `FAILED` and
  not retried by the task.

Unexpected errors are recorded as `UNEXPECTED` with the exception class only.
Every failed execution counts towards `attempts`. Time limits are 120/150 s
for embedding, 30/60 s for matching and 60/90 s for reconciliation (soft/hard).

**Reconciliation** (Beat `news-story-reconcile`, every
`NEWS_STORY_RECONCILE_INTERVAL_SECONDS` = 900 s when enabled) selects, in
article-id order and at most `NEWS_STORY_RECONCILE_BATCH` (200) per sweep:

- Articles with no processing row;
- rows older than `NEWS_STORY_RECONCILE_AFTER_SECONDS` (600 s) that are stale,
  stuck in `PENDING`/`EMBEDDED`, `MATCHED` without an association, or `FAILED`
  with fewer than `NEWS_STORY_PROCESSING_MAX_ATTEMPTS` (6) failed executions.

A `FAILED` row at the cap is no longer dispatched but stays visible. Selected
rows are claimed (created `PENDING` or touched) before dispatch, so a queued
Article is not dispatched again before the cutoff. A fully processed database
dispatches nothing.

**Operator commands** (no HTTP surface; output holds identifiers, states and
keys only):

```bash
uv run --locked --env-file .env python manage.py news_story_process --article 42
uv run --locked --env-file .env python manage.py news_story_process --article 42 --reprocess
uv run --locked --env-file .env python manage.py news_story_process --article 42 --async
uv run --locked --env-file .env python manage.py news_story_reconcile --limit 50
uv run --locked --env-file .env python manage.py news_story_reconcile --async
```

`--reprocess` deletes that Article's embeddings, primary association and
processing row, then rebuilds them. It never writes to `Article`,
`RawArticle`, `IngestionRun`, `Source` or `SourceEndpoint`.

The opt-in `celery_smoke` run includes
`tests/news/test_story_celery_smoke.py`: a separately running worker embeds and
matches an Article over Redis. It uses the same worker command as the News
smoke check below.

## Story Topics and Entities

`news.application.story_enrichment.extract_story_enrichment(story_id)`
describes a Story with reusable `Topic` and `Entity` rows (issue #30). It
returns an `EnrichmentSummary` (counts and `model_key`s). The Story refresh
lifecycle also uses its compute and persistence seams.

Extracted data is **derived**. An `Entity` is a machine reading of source
text, not an independent fact (ADR-0004). `Topic` (`slug`, `label`) and
`Entity` (`kind`, `normalized_key`, `display_name`) carry no description,
claim, truth flag, external identifier or Source. The per-Story links
`StoryTopic`/`StoryEntity` are disposable and carry a `score` (0–1), the
extractor's `model_key` and `generated_at`.

- **Input:** only the Story's current members (`StoryArticle` → `Article`), in
  publication order. At most `NEWS_STORY_ENRICHMENT_MAX_ARTICLES` (20)
  Articles are read, and at most `NEWS_STORY_ENRICHMENT_MAX_CHARS_PER_ARTICLE`
  (4000) characters each. No other Story or external source is consulted.
- **Output bounds:** `NEWS_STORY_MAX_TOPICS` (8) and
  `NEWS_STORY_MAX_ENTITIES` (20).
- **Normalization** (`news/domain/enrichment.py`): casefolding, punctuation and
  whitespace collapse, and an ASCII slug. "Central Bank", "central bank" and
  "Central  Bank" become one Entity. This is not entity resolution: "IBM" and
  "International Business Machines" stay separate.
- **Extractor:** the default `rules:capitalized-phrases-keywords@1`
  (`news/adapters/rule_based_enrichment.py`) is deterministic and offline.
  Entities are capitalized phrases, classified by titles and organization or
  place words; Topics are the content words most member Articles share.
  Scores are the share of members that mention the item. Other extractors plug
  into the `TopicExtractor`/`EntityExtractor` protocols. None is installed by
  default, and there is no optional dependency group for enrichment.
- **Replacement:** extraction runs and is validated before any transaction.
  One short transaction then replaces the Story's Topic and Entity links, so a
  failing extractor leaves the previous set untouched. Failures raise
  `EnrichmentError` (`NO_MEMBERS`, `EXTRACTOR_FAILED`, `INVALID_OUTPUT`) and
  log one warning with identifiers only, never Article text.

## Story synthesis

`news.application.story_synthesis.synthesize_story(story_id)` gives a Story a
title, summary and context built only from its member Articles (issue #31).
It returns a `SynthesisSummary` (identifiers, `model_key`,
`member_signature` and counts). The Story refresh lifecycle also uses its
compute and persistence seams.

The output is **derived**: sources support the information, and synthesis
organizes it (ADR-0004). It is also **impersonal**: `synthesize_story` takes
no user, and its input and output types carry no user, account, session,
interest, ranking, Opinion, Position or Perspective field. The same Story state
reads the same for everyone (ADR-0008).

- **Persistence** (three tables). `StorySynthesis` is a generation header
  (`model_key`, `generated_at`, `is_current`, `member_signature`) with no text,
  and at most one current generation per Story. `StorySynthesisElement` holds
  each `TITLE`/`SUMMARY`/`CONTEXT` text with its `position`, and exactly one
  `TITLE` per generation. `StorySynthesisElementSource` lists the member
  Articles supporting each element, in order. "Which Articles support this
  element?" is one query; "which Articles fed this generation?" is one join.
- **`member_signature`** (`news.domain.stories.member_signature`) is the
  SHA-256 of the sorted `article_id:updated_at` pairs of every current member.
  `Article.updated_at` is the revision marker News Core moves when a new
  revision is applied. The same signature and `model_key` reuse the current
  generation without calling the synthesizer.
- **Input bounds:** `NEWS_STORY_SYNTHESIS_MAX_ARTICLES` (20) members in
  publication order, each with its title and at most
  `NEWS_STORY_SYNTHESIS_MAX_CHARS_PER_ARTICLE` (4000) characters of body text.
- **Default synthesizer:** `extractive:lead-sentences@1` is extractive,
  offline and deterministic. It copies the earliest member title, each
  member's first complete sentence (at most 3 summary elements) and further
  sentences (at most 4 context elements). Every element cites each member
  whose text contains it. It never writes new prose. Other synthesizers plug
  into the `StorySynthesizer` protocol; any future external one must make
  sending source text an explicit opt-in.
- **Disagreement:** when members give different figures for the same point,
  the extractive default **omits** the disputed sentences rather than choosing
  a side. Detection covers figures written in digits.
- **Replacement:** the result is computed and validated with no transaction
  open: exactly one `TITLE`, at least one supporting member per element, and
  no Article outside the Story. One transaction then demotes the previous
  generation and writes the new one. A failure raises `SynthesisError` and logs
  identifiers only, and the previous current synthesis stays in place.

## Story refresh

`news.application.story_refresh.refresh_story(story_id, reason=...)` coordinates
the embedding, Topics, Entities, synthesis and membership counters as one
generation. `Story.refresh_state` is `STALE` before the first refresh and after
membership or member Article revision changes, `CURRENT` after coherent
promotion, and `FAILED` when the current membership cannot be refreshed.
`refreshed_at` is null until the first success. The bounded `refresh_error`
holds `<step>:<error kind>` (for example `story_synthesis:INVALID_OUTPUT`),
never publication text or provider output; the step is one of
`story_embedding`, `story_enrichment`, `story_synthesis` or
`refresh_promotion`. Rows failed before #33 hold the kind alone.

The generation key is the existing SHA-256 `member_signature` over **all**
sorted member `article_id:Article.updated_at` pairs. A short, read-only
snapshot captures every member's revision, source, text and event time, plus
exact article/source counts and publication window. Component-specific limits
apply after this snapshot. Embedding computes directly from the captured
Article text, so an old `ArticleEmbedding` cannot hide a newer revision.
Enrichment and synthesis use their own bounded preparation and validation
rules. Compute makes no database writes and holds no transaction or Story
lock.

A short PostgreSQL compare-and-swap transaction locks the Story, recomputes
the full membership signature and discards the entire computed result if it
moved. That pass leaves the Story `STALE` and queues another refresh after
commit. If it matches, one transaction promotes all derived rows with the
same signature and updates `Story` metadata to `CURRENT`. Concurrent readers
therefore see one complete committed generation. A `CURRENT` Story with the
same signature and complete derived rows is a read-only no-op, including on
Celery redelivery. No Redis lock is used.

`Story` existence, `StoryArticle` membership, `status` and `refresh_state`
are immediately authoritative. Embeddings, Topics, Entities, synthesis,
`article_count`, `source_count` and the publication window are eventually
refreshed. A `STALE` or `FAILED` Story retains its previous coherent derived
generation. A failed computation records only safe, bounded metadata for the
same membership signature; an older failure cannot overwrite newer `STALE`
state. An empty Story refresh skips computation, archives the Story, sets zero
counts and null dates, and retains previous derived rows for inspection.

The `StoryEmbedding`, `StoryTopic` and `StoryEntity` signatures added in
migration 0010 are nullable. `NULL` means a pre-#32 or standalone derived row
whose exact membership provenance cannot be proven. Refresh stamps each new
current row with the winning signature; migration 0010 does not invent a
signature for legacy data. There is intentionally no `StoryUpdate` table in
v0.3: state, current synthesis and row signatures answer present operator
questions without an unused history schema.

Matching, reassignment and reprocessing mark affected Stories `STALE` in the
membership transaction. News Core revision processing does the same for
existing member Stories when `Article.updated_at` advances. When Story
processing is enabled, a named `transaction.on_commit` callback queues
`refresh_story_task(story_id, reason)`. Rolled-back transactions queue nothing.
The task carries identifiers only, retries transient failures up to three
times with the existing bounded backoff, and leaves permanent failures
terminal and visible. Its soft/hard time limits are 180/210 seconds.

```bash
uv run --locked --env-file .env python manage.py news_story_refresh --story 123
uv run --locked --env-file .env python manage.py news_story_refresh --story 123 --async
uv run --locked --env-file .env python manage.py news_story_refresh --stale-failed --limit 100
uv run --locked --env-file .env python manage.py news_story_refresh --stale-failed --limit 100 --async
```

The sweep selects Story ids in ascending order and bounds `--limit` to 1–1000.
There is no Beat entry for Story refresh: reconciliation covers Articles only.
A `STALE` Story whose refresh message was lost, or any Story marked `STALE`
while `NEWS_STORY_PROCESSING_ENABLED` is off (for example by a synchronous
`news_story_process`), is refreshed by its next membership change or by
`news_story_refresh --stale-failed`.

## Story observability

Story processing is explainable from the structured logs and four read-only
commands (issue #33). There is no metrics platform and no HTTP operator API.
Explanations come from what was **recorded at decision time** —
`StoryArticle.evidence`, `ArticleStoryProcessing` and the Story's refresh
columns — never from running retrieval again, which could answer differently
once other Stories exist.

### Story log context and step records

`news/logging.py::story_logger(**context)` is the ingestion adapter with the
Story context. Only the fields known at a step are bound:

| Field | Bound by |
| --- | --- |
| `article_id` | every Article step: embedding, retrieval, decision, association |
| `story_id` | every refresh step; association records name the Story they joined |
| `model_key` | the embedding model for Article steps; each component's own key in refresh |
| `task_id`, `attempt`, `trigger` | Celery tasks (`TASK`, or `RETRY` on a retry); absent on synchronous operator calls |

Each meaningful step emits **one** record, `News Story step completed` (INFO) or
`News Story step failed` (WARNING), with a stable `step` and a monotonic
`duration_ms`:

| `step` | Where | Fields |
| --- | --- | --- |
| `article_embedding` | `story_processing.embed_step` | `state`, `attempts`, `error_kind`, `pipeline_key` |
| `candidate_retrieval` | `story_matching.match_article` | `candidate_count` |
| `matching_decision` | `story_matching.match_article` | `decision` (`MATCH`/`CREATE_NEW_STORY`), `match_reason`, `match_rule` (`PRIMARY_DISTANCE`/`SECONDARY_EVENT_VERIFY`, null on create), `chosen_story_id`, `distance`, `threshold`, `candidate_count` |
| `story_association` | `story_matching.match_article` | `outcome` (`MATCHED`, `CREATED_STORY`, `ALREADY_ASSIGNED`), `story_id`, `matcher_key` |
| `story_matching` | `story_processing.match_step` | `state`, `attempts`, `error_kind`, `story_id` |
| `story_embedding` | `story_refresh.refresh_story` | `model_key`, `member_count` |
| `topic_extraction` | `story_enrichment.compute_story_enrichment` | `model_key`, `topic_count` |
| `entity_extraction` | `story_enrichment.compute_story_enrichment` | `model_key`, `entity_count` |
| `story_synthesis` | `story_synthesis.compute_story_synthesis` | `model_key`, `element_count`, `synthesis_source_count`, `input_article_count` |
| `story_refresh` | `story_refresh.refresh_story` | `outcome` (`REFRESHED`, `NOOP`, `STALE_RETRY`, `ARCHIVED`, `FAILED`), `refresh_state`, `refresh_reason`, `member_count`, `article_count`, `source_count`, `failed_step`, `error_kind` |

A replayed match that finds the Article already assigned logs only its
`story_association` with `ALREADY_ASSIGNED`: no decision was made, so none is
logged. A `NOOP` refresh carries no `refresh_state`, because it changed none.
Failed enrichment and synthesis keep their existing `News Story enrichment
failed` / `News Story synthesis failed` warnings, now with `step`.

The JSON allowlist gained `step`, `failed_step`, `candidate_count`,
`chosen_story_id`, `distance`, `threshold`, `decision`, `match_reason`,
`match_rule`, `matcher_key`, `member_count`, `article_count`, `source_count`,
`synthesis_source_count`, `refresh_state` and `refresh_reason`
(`story_id`, `model_key`, `topic_count`, `entity_count`, `error_kind`,
`duration_ms`, `state` and `outcome` already existed). Titles, descriptions,
bodies, payloads, synthesis text, prompts, provider responses and vectors are
not allowlisted: an attempt to log one is dropped whole, not truncated.

### Operator commands

```bash
# Recent Stories, newest first: counters, refresh state, failed step, synthesis model_key
uv run --locked --env-file .env python manage.py news_stories --last 20
uv run --locked --env-file .env python manage.py news_stories --stale
uv run --locked --env-file .env python manage.py news_stories --failed        # exit 1 if any
uv run --locked --env-file .env python manage.py news_stories --failed --refresh

# One Story: lifecycle, counters, members, Topics, Entities, synthesis provenance
uv run --locked --env-file .env python manage.py news_story --story 123
uv run --locked --env-file .env python manage.py news_story --story 123 --members 500
uv run --locked --env-file .env python manage.py news_story --story 123 --refresh

# Why did Article 42 join or create its Story?
uv run --locked --env-file .env python manage.py news_story_explain --article 42
uv run --locked --env-file .env python manage.py news_story_explain --article 42 --reprocess

# Articles whose Story processing is not complete
uv run --locked --env-file .env python manage.py news_story_backlog               # exit 1 on failures
uv run --locked --env-file .env python manage.py news_story_backlog --failed --limit 100
uv run --locked --env-file .env python manage.py news_story_backlog --pending --limit 50 --process
```

| Question | Command | What answers it |
| --- | --- | --- |
| Which Stories are stale or failed? | `news_stories --stale` / `--failed` | `REFRESH`, `FAILED_STEP`, `ERROR` columns |
| Why did Article X join Story Y? | `news_story_explain --article X` | `decision=MATCH`, `chosen_story_id`, the deciding `distance`, the `threshold` in force, `candidate_count` and the recorded candidates |
| Why did Article X create a new Story? | `news_story_explain --article X` | `decision=CREATE_NEW_STORY` with `reason` `NO_CANDIDATES`, `NO_COMPATIBLE_CANDIDATE`, `ABOVE_THRESHOLD` or `VERIFICATION_REJECTED`, plus each recorded candidate's distance and each secondary verification's `result` |
| Which rule joined Article X to Story Y? | `news_story_explain --article X` | `match_rule=PRIMARY_DISTANCE` or `SECONDARY_EVENT_VERIFY`, with the verification's nearest member distance and shared-name count; revision 1 evidence is shown as `PRIMARY_DISTANCE`, its only rule |
| Which Articles support Story Y? | `news_story --story Y` | every member (id, Source, canonical URL, title, publication time, method, recorded distance) with `CITED`, and each synthesis element's supporting Article ids |
| Which Story-derived step failed? | `news_story --story Y`, `news_story_explain --article X`, `news_story_backlog --failed` | `failed_step` and `error_kind` |
| How do I reprocess one Article? | `news_story_explain --article X --reprocess` (or `news_story_process --article X --reprocess`) | `reprocess_article` (#29) |
| How do I refresh one Story? | `news_story --story Y --refresh` (or `news_story_refresh --story Y`) | `refresh_story` (#32) |

**Explanations.** The recorded evidence holds the matcher's `reason`, the
deciding `distance`, `candidate_count`, at most five candidates with their
distances, the `max_distance` and `max_time_gap_hours` in force and the
embedding `model_key`. `candidate_count` counts every candidate retrieved;
only the nearest five are recorded. Since #36 it also holds the `rule` that
accepted a match, `secondary_max_distance` and the secondary `verification`
entries, which `news_story_explain` prints as `match_rule`,
`secondary_threshold` and one line per verified candidate. Since #38 it also
holds `secondary_max_member_distance` and `kept_current_story`, printed as
`secondary_member_threshold` and `kept_current_story`. `fresh=yes` means
the Article is matched under the configured `pipeline_key` and still has its
primary association.

**Failed step.** A Story's `refresh_error` records its step. An Article's is
read from what the failure left: embedding-provider kinds fail at
`article_embedding`; `MISSING_EMBEDDING` and `STORY_NO_LONGER_ACTIVE` at
`story_matching`; `DATABASE_UNAVAILABLE` and `UNEXPECTED` at
`story_matching` when the Article's embedding exists for the recorded model,
otherwise at `article_embedding`. `retries_exhausted=yes` means reconciliation
has stopped re-dispatching it (`NEWS_STORY_PROCESSING_MAX_ATTEMPTS`).

**Backlog.** `news_story_backlog` never conflates states: `MISSING` (no
processing row: never attempted), `PENDING`, `EMBEDDED`, `FAILED`,
`UNASSIGNED` (`MATCHED` but its primary association is gone) and `STALE`
(`MATCHED` under another `pipeline_key`). `--pending` is everything not
`FAILED`; `--failed` only `FAILED`. Unlike reconciliation it applies no cutoff
and no attempt cap. It exits **1** when the listing contains a failure, like
`news_runs --stale`.

**Bounds and actions.** Every listing is ordered in SQL and bounded to
1–1000 (`--last` default 20, `--members` default 100, `--limit` default 50).
Inspection is read-only unless an action flag is given: `--reprocess` calls
`reprocess_article`, `--refresh` calls `refresh_story(reason="operator")` and
`--process` calls `process_article` for each listed Article. Commands contain
no matching, retrieval or enrichment rule; a test inspects their source.
Output shows identifiers, states, keys, canonical URLs and titles — never
body text, descriptions, payloads or synthesis text.

## Local Compose stack

Prerequisites: Docker Engine with BuildKit and Docker Compose v2.20 or newer
(`docker compose`, not legacy `docker-compose`). Docker Desktop with Linux
containers also works. The backend publishes only on host loopback.

### Image versions

| Runtime | Pinned image tag | Version strategy |
| --- | --- | --- |
| Python | `python:3.14.4-slim-bookworm` | Matches `.python-version`; multi-platform digest pinned in Dockerfile |
| uv | `ghcr.io/astral-sh/uv:0.12.13` | Matches the existing dependency workflow; digest pinned in Dockerfile |
| Database | `pgvector/pgvector:0.8.6-pg17-bookworm` | PostgreSQL 17.11 (`17.11-1.pgdg12+2`), pgvector 0.8.6; digest pinned in Compose |
| Broker | `redis:8.10.1-alpine` | Celery broker/result backend; digest pinned in Compose |

Tags and manifest digests were checked against their registries. Python 3.14.4
preserves the backend runtime; PostgreSQL 17 on Bookworm provides a maintained
pgvector image with the familiar `/var/lib/postgresql/data` layout. The database
and Redis images both support Linux amd64 and arm64. See the
[upstream pgvector images](https://github.com/pgvector/pgvector#docker).

Digests prevent tag rebuilds from changing the runtime unexpectedly. To update,
verify the replacement image, change its tag and digest together, rebuild and
repeat migrations/vector/persistence checks on a disposable project. PostgreSQL
major upgrades require a separate data migration; never point a new major at an
existing volume without an upgrade plan.

### Configuration and networking

Run these commands from the **repository root**. Create `backend/.env` from the
example only if it does not already exist; preserve an existing local file.

```bash
cp -n backend/.env.example backend/.env
```

Use `--env-file backend/.env` on every Compose command. There is no second root
environment file. Compose passes only declared variables to each service; the
database does not receive Django's secret key.

The same file supports both execution modes:

| Connection | Host | Port |
| --- | --- | --- |
| Django running on the host | `POSTGRES_HOST=127.0.0.1` | `POSTGRES_PORT=55432` in the example |
| Django in Compose | `postgres` (overridden explicitly by Compose) | `5432` (internal container port) |

The database host port defaults to 55432 to avoid interfering with an existing
host PostgreSQL on 5432. Update `POSTGRES_PORT` if needed. `BACKEND_PORT` optionally
changes the backend host port (default 8000). Both ports bind to `127.0.0.1`.

Shell exports override Compose `--env-file` values. To use the file exclusively,
run the workflow in a shell where these names are unset:

```bash
unset SECRET_KEY DEBUG ALLOWED_HOSTS POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD POSTGRES_HOST POSTGRES_PORT BACKEND_PORT
```

If you intentionally override a port (e.g. for an isolated validation project),
keep the same override for all commands. Single-quote values containing literal
`$` or `#` in the env file as appropriate for Compose's env-file syntax.
Use `config --quiet` to validate without dumping credentials. The full rendered
configuration and container inspection can contain environment values; do not
publish those outputs.

These are disposable local credentials, not production credentials. The official
PostgreSQL entrypoint creates the local `POSTGRES_USER` with permission to enable
extensions. Changing credentials in `.env` does not update a previously
initialized database: change the role explicitly or deliberately reset only a
disposable project.

### Build, migrate and start

```bash
docker compose --env-file backend/.env config --quiet
docker compose --env-file backend/.env build backend
docker compose --env-file backend/.env up -d --wait --wait-timeout 90 postgres
docker compose --env-file backend/.env run --rm backend python manage.py migrate --noinput
docker compose --env-file backend/.env up -d backend
docker compose --env-file backend/.env logs --tail=50 backend postgres
```

The database health check uses `pg_isready` over TCP; Compose waits for
`service_healthy` before starting Django. It proves PostgreSQL is accepting
connections, not that Django credentials or schema are correct. The explicit
migration command proves those prerequisites before API startup. There are no
startup sleeps and no automatic migrations hidden in the image command.

The backend runs Django's development server on `0.0.0.0:8000` inside the
container. Source is bind-mounted at `/app` for reload; dependencies live at
`/opt/venv`, so a host `.venv` does not replace them. Rebuild after dependency or
Dockerfile changes. `.dockerignore` keeps host environments, secrets and caches
out of image build context.

The backend exposes `/health/live`, `/health/ready` and JWT authentication
under `/api/auth/`. News ingestion has no HTTP operator API; Celery/Beat and
`manage.py` commands drive it. Check `/health/live` to verify the HTTP listener
and `/health/ready` for required dependencies.

### Schema ownership and pgvector

`database` is a small Django infrastructure app with no product models.
`database/0001_enable_vector` uses Django's `CreateExtension("vector")` operation.
The image provides the extension binaries; `migrate` enables the extension in the
selected database and records it in Django migration history. There is no
competing init SQL script. Re-running `migrate` skips applied migrations, and
`CreateExtension` also tolerates an already enabled extension.

Use a migration role with extension privileges when adapting this beyond local
development. Do not reverse the extension migration once later tables use vector
types; removing an extension can remove its dependent objects.

```bash
docker compose --env-file backend/.env run --rm backend python manage.py check
docker compose --env-file backend/.env run --rm backend python manage.py makemigrations --check --dry-run
docker compose --env-file backend/.env run --rm backend python manage.py showmigrations
# The container shell expands its own configured user/database, not host variables.
docker compose --env-file backend/.env exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
SELECT version();
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
SELECT to_regclass('public.accounts_user');
SELECT app, name FROM django_migrations ORDER BY app, name;
SELECT '[0,0,0]'::vector <-> '[3,4,0]'::vector AS l2_distance;
SQL
```

The distance must be **5**: `<->` is Euclidean (L2) distance and
`sqrt(3² + 4²) = 5`. This uses literals, without an embedding model or table.

### Stop, restart and reset

Normal stop/start preserves `postgres_data`:

```bash
docker compose --env-file backend/.env down
docker compose --env-file backend/.env up -d --wait --wait-timeout 90 postgres
docker compose --env-file backend/.env up -d backend
```

For a process restart without removing containers:

```bash
docker compose --env-file backend/.env restart postgres backend
docker compose --env-file backend/.env up -d --wait --wait-timeout 90 postgres
docker compose --env-file backend/.env logs --tail=50 backend postgres
```

**Destructive reset — deletes this project's database permanently:**

```bash
docker compose --env-file backend/.env down --volumes
```

Use that only after inspecting the project's resources and confirming its data
is disposable. It is not part of routine restart. After a reset, repeat database
startup and the explicit migration command.

### Isolated fresh-volume verification

Use an unused project name via `-p`, such as `pulso-issue2-check`, on **every**
Compose command. First inspect existing containers and volumes with that project
label. Use unused host ports through `POSTGRES_PORT` and `BACKEND_PORT` overrides.
Compose automatically namespaces the network and `postgres_data` volume by
project; no fixed container name or external volume is used.

On the fresh project: follow the build/start/migrate steps, verify SQL above,
create `issue2_persistence_test` through Django's `create_user`, then run normal
`down` and `up` without `--volumes`. Assert that the same account primary key
still exists. Re-run `config --quiet` and `migrate --noinput`; both must succeed
without losing the account. Only then remove that test project's resources and
volume if desired. Do not prune global Docker state or delete another project.

## Current scope

The stack covers the backend, PostgreSQL/pgvector, Redis/Celery, liveness/
readiness health endpoints and mobile API authentication. It retains the
issue #1 custom User and package boundaries. Database runtime validation is
now possible using Compose; host-only checks still require a reachable
configured PostgreSQL to verify applied migration history. News ingestion and
the Story Engine are documented above; product HTTP APIs for Stories do not
exist yet.

See [module boundaries](../docs/architecture/module-boundaries.md).

## Backend quality and isolated tests

`uv sync --locked` installs the `dev` group with Ruff, pytest and pytest-django.
The development Docker image intentionally uses `--no-dev`; run the quality
commands on the host using the project's uv environment. Future CI can reuse the
same commands. No paid account or external provider credentials are needed.

Tests use a **separate PostgreSQL instance**, `postgres-test`, with the same
pinned pgvector image as development. It is opt-in through the `test` profile,
uses disposable tmpfs storage, and never mounts `postgres_data`. It starts only
when explicitly requested. Its public credentials belong solely to this instance.

From the repository root:

```bash
# Uses public examples for Compose interpolation; does not start the dev services.
docker compose --env-file backend/.env.example --profile test up -d --wait --wait-timeout 90 postgres-test
cd backend
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked python manage.py check --settings=config.settings_test
uv run --locked python manage.py makemigrations --check --dry-run --settings=config.settings_test
uv run --locked pytest
```

As with the development stack, unset conflicting exported Compose variables
before loading the example file. The test service's database name, username and
password are fixed and independent of those development variables.

The default host test port is **55433**. To avoid a port conflict, set
`TEST_POSTGRES_PORT` to the same unused port for both Compose and pytest.
`TEST_POSTGRES_HOST` defaults to `127.0.0.1`; `localhost` and `postgres-test` are
also accepted for local/Compose execution. The container itself listens on 5432.

`config.settings_test` imports shared application wiring from `config.common`,
without loading development environment variables or credentials. pytest creates
`test_pulso`, runs real migrations (including `database.0001_enable_vector`), and
drops that database after the session. The service's maintenance database is
`pulso_tests`. No manual SQL or pre-created test schema is required.

The ordinary backend suite includes `tests/news/test_news_core_end_to_end.py`:
it exercises RSS and JSON Feed through a real loopback fixture HTTP server,
RawArticle intake, normalization and Article deduplication. The reusable
fixture corpus is catalogued in `tests/fixtures/news/README.md`; the server
implementation lives in `tests/news/fixture_server.py`. A session DNS guard
blocks public hostname resolution, so tests cannot silently depend on live
sites. `config.settings_test` permits private targets only to reach the local
fixture server; development settings still default to denying them. The
separate real-worker News smoke remains opt-in with `pytest -m celery_smoke`.

The Story Engine end-to-end gate (#34) is
`tests/news/test_story_engine_end_to_end.py`, supported by `story_pipeline.py`.
It calls the real application services against PostgreSQL/pgvector, processing
the #26 corpus in publication order and refreshing after each association.
It asserts exact membership of **12 active Stories / 26 StoryArticle rows**,
named same-event and false-merge scenarios, final enrichment and embedding,
source-grounded synthesis, second-pass idempotency, reprocessing, task
redelivery and controlled concurrency on separate database connections.
Reprocessing all Articles preserves the 12 active event groups and leaves
2 archived empty historical Stories, excluded from candidate retrieval.
Historical synthesis generations are allowed; current derived state must be
unique and share the final membership signature. All five News Core provenance
tables are compared before and after rebuilding/reprocessing.

`tests/news/test_story_quality.py` scores that full pipeline with the existing
#26 evaluator. On the **repository-owned synthetic regression corpus**, matcher
v2 (#36) has precision **1.000**, recall **1.000**, false-merge count/rate
**0 / 0.000**, false-split count/rate **0 / 0.000**, and **0 unassigned**.
These are exact regression gates: with recorded inputs and deterministic
execution there is no tolerance; any new split or merge needs deliberate
re-measurement. Failures identify fixture Article ids, expected event labels,
actual Story ids and offending pairs. These measurements are not production
accuracy estimates. Historical v1 and current v2 figures are kept distinct in
the [corpus README](tests/fixtures/news/stories/README.md#quality-regression-gate).

Run just the #34 gates with:

```bash
uv run --locked pytest tests/news/test_story_engine_end_to_end.py tests/news/test_story_quality.py
```

The tests replay `RecordedEmbeddingProvider` vectors with the shipped local
rule-based extractor and deterministic extractive synthesizer. The existing
non-loopback DNS guard remains enabled: no model download, hosted API or live
feed is needed. The separate-worker path is opt-in as documented below.

The root backend `conftest.py` rejects alternate settings, development database
names/credentials, mirrors and `--no-migrations` before test database setup.
Use the standard commands above; the guard protects against configuration
mistakes, not arbitrary Python code intentionally modifying connections.

Tests verify account persistence, password hashing and validation, username
uniqueness, automatic vector extension setup and numeric distance/ordering in
PostgreSQL. Run `uv run --locked pytest` twice to verify repeated creation and
teardown; no `--reuse-db` flag is enabled by default.

To apply formatting intentionally:

```bash
uv run --locked ruff format .
```

Lint, formatting, migration drift and failing tests return nonzero exit codes.
Ruff includes existing migration files; their formatting changes do not change
schema operations. Authentication transport and product behavior remain outside
this issue.

To stop and remove **only the disposable test service**, from the repository root:

```bash
docker compose --env-file backend/.env.example --profile test stop postgres-test
docker compose --env-file backend/.env.example --profile test rm -f postgres-test
```

This discards test data held in tmpfs and leaves the development database and its
persistent volume intact. For an isolated validation project, use the same `-p`
project name on every Compose command.

## Worker infrastructure (Redis, Celery worker, optional Beat)

The API, a Celery worker and an optional Celery Beat scheduler share the same
backend codebase and image, running as separate Compose services/processes
(ADR-0001, ADR-0005). This issue (#4) proves that infrastructure with a
harmless diagnostic task; it introduces no product task.

| Setting | Contract |
| --- | --- |
| `REDIS_HOST`, `REDIS_PORT` | Required, same pattern as the `POSTGRES_*` settings |
| `CELERY_DIAGNOSTIC_BEAT_ENABLED` | Optional, defaults to `false`; only toggle enabling `diagnostics.tasks.diagnostic_ping` on a 30s Beat schedule |
| `NEWS_INGESTION_ENABLED` | Optional, defaults to `true`; gates the three News Beat entries (see [Worker and Beat](#worker-and-beat)) |
| `NEWS_POLL_DISPATCH_INTERVAL_SECONDS` | Optional, defaults to `300`; positive integer interval for `news-poll-due-endpoints` |
| `NEWS_STORY_PROCESSING_ENABLED` | Optional, defaults to `false`; enables after-commit Story task dispatch and the `news-story-reconcile` Beat entry (see [Story processing](#story-processing)) |

`REDIS_PORT` defaults to **6399** on the host to avoid colliding with a
locally installed Redis on 6379; Compose always uses `redis:6379` between
containers, the same pattern as `POSTGRES_HOST`/`POSTGRES_PORT`.

### Queue, broker and timeout configuration

`config/celery.py` builds one Celery app (`config.celery_app`) shared by the
API, worker and Beat processes; `app.config_from_object("django.conf:settings",
namespace="CELERY")` reads its configuration from Django settings, and
`app.autodiscover_tasks()` finds `tasks.py` in each `INSTALLED_APPS` entry.
`config/common.py` documents the minimal, workload-agnostic defaults shared by
every environment:

- a single `default` queue — workload-specific queues are deferred until a
  concrete operational reason exists (ADR-0005's queue topology is a
  non-goal for this issue);
- a 5s broker connection timeout, so a slow/unreachable Redis fails fast
  instead of hanging the API, worker or Beat process at startup;
- a 60s hard / 30s soft task time limit, so a stuck task cannot run forever;
- `CELERY_TASK_ACKS_LATE` and `CELERY_TASK_REJECT_ON_WORKER_LOST` are both
  `True`: a task is only acknowledged after it finishes, and a worker that
  dies mid-task makes the message redeliverable. Combined with ADR-0005's
  "tasks may execute more than once" invariant, this is why task effects on
  domain state must be idempotent.

`CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND` are environment-specific and
defined in `config/settings.py` (development, Redis logical DB 0) and
`config/settings_test.py` (isolated tests, DB 1 on the same local Redis
instance — see below). Redis is a broker/result cache, not authoritative
persistence: losing it loses queued/derived state, never domain data.

### The diagnostic task

`diagnostics/` is a temporary infrastructure app, not a product domain
module. `diagnostics/tasks.py` defines `diagnostic_ping`, a thin `@shared_task`
adapter that delegates to `diagnostics/application.py::execute_diagnostic_ping`
(ADR-0005's task boundary). That function reads and writes no domain state —
it only logs and returns a small `{"ok": True, "executed_at": ...}` result —
so repeated or duplicate delivery cannot mutate authoritative data by
construction. Its completion is observable through worker logs and through
the task result (used here only as an operational/test probe, never as
domain truth — ADR-0005's "Result handling").

### Running the stack with a worker

From the repository root, after the [build/migrate/start](#build-migrate-and-start)
steps above:

```bash
docker compose --env-file backend/.env config --quiet
docker compose --env-file backend/.env up -d --wait --wait-timeout 90 postgres redis
docker compose --env-file backend/.env run --rm backend python manage.py migrate --noinput
docker compose --env-file backend/.env up -d backend worker
docker compose --env-file backend/.env logs --tail=50 backend worker redis
```

Prove real (non-eager) execution across containers — the worker must be a
separately running process, not local/eager execution:

```bash
docker compose --env-file backend/.env run --rm backend python manage.py shell -c "
from diagnostics.tasks import diagnostic_ping
print(diagnostic_ping.delay().get(timeout=10))
"
docker compose --env-file backend/.env logs --tail=20 worker
```

The worker log shows `received`, the application log line and `succeeded`
with the same result the caller printed. Celery reports the container as
running with superuser privileges (`SecurityWarning`); this is expected for
this bind-mounted local dev image, which runs every service as root, and is
not specific to the worker.

### Automated real-broker smoke check

Ordinary `pytest` runs are independent of a running worker: `tests/test_diagnostics.py`
and `tests/news/test_tasks.py` call the task adapters with `.apply()`
(synchronous, in-process, no broker). The real-broker checks live in
`tests/test_celery_smoke.py` (diagnostic ping),
`tests/news/test_news_celery_smoke.py` (News ingestion), and
`tests/news/test_story_celery_smoke.py` (Story processing and refresh), all marked
`celery_smoke` and excluded from the default run through `pyproject.toml`'s
`addopts`. They require a separately running worker consuming the same Redis
instance used by `config.settings_test` (logical DB 1, isolated from
development DB 0):

```bash
docker compose --env-file backend/.env.example --profile test up -d --wait --wait-timeout 90 postgres-test redis
cd backend
DJANGO_SETTINGS_MODULE=config.settings_smoke_worker uv run --locked celery -A config worker --loglevel=INFO --concurrency=2 &
uv run --locked pytest -m celery_smoke
```

`config.settings_smoke_worker` is `config.settings_test` with exactly one
change: the worker connects to `test_pulso`, the database pytest-django creates
for the test session, instead of the base `pulso_tests` database. The News
check asserts rows the worker itself wrote, so the two processes must share one
database — a worker started with `config.settings_test` reads and writes a
different database and the check fails immediately rather than passing by
accident. pytest-django applies the migrations to `test_pulso`, the worker
connects lazily on its first task, and Celery's Django fixup closes that
connection after every task, so the test database can still be dropped at the
end of the session. The News check is `django_db(transaction=True)` so its own
writes are committed and therefore visible to the worker's connection; its
rows are removed by the usual post-test flush.

Both checks are bounded: the diagnostic ping uses `.get(timeout=10)` and the
News check `.get(timeout=30)`, each failing with a clear message if no worker
consumes the task. The News check serves a repository fixture from a loopback
HTTP server on an ephemeral 127.0.0.1 port (no internet, no `MockTransport`,
nothing listening externally) — permitted because `config.settings_test` allows
private-network targets — then dispatches `news.tasks.ingest_endpoint.delay()`
twice: the first run is `SUCCEEDED` with Articles created, the second is
`NO_CHANGE` with no new Article. The fixture server runs in the pytest process
and the worker is a host process in the same CI job, so `127.0.0.1` resolves to
the same loopback for both; a containerized worker would not reach it, so this
command assumes the host worker used by CI. Stop the background worker
afterward; it is not part of the normal test suite and does not start
automatically.

The complete Story smoke (#34) embeds and matches a first Article through
Redis, promotes its generation in the separate worker, then sends a later
same-event Article from another Source through the same path and refreshes
again. It asserts the shared Story, two associations, distinct source count,
embedding/enrichment/synthesis signatures and member-only synthesis citations
in PostgreSQL. The isolated worker uses the deterministic embedding provider;
this transport test complements the recorded-vector corpus quality gate.

### Verifying the optional Beat schedule

Beat is profile-gated (`profiles: ["beat"]`) and its diagnostic schedule is
disabled unless explicitly enabled; neither runs during normal startup. The
News and Story schedules have their own flags (`NEWS_INGESTION_ENABLED`,
`NEWS_STORY_PROCESSING_ENABLED`). To verify the diagnostic schedule locally:

```bash
docker compose --env-file backend/.env up -d worker
CELERY_DIAGNOSTIC_BEAT_ENABLED=true docker compose --env-file backend/.env --profile beat up -d beat
docker compose --env-file backend/.env logs --tail=20 worker beat
```

Within 30s the Beat log shows `Sending due task diagnostic-ping`, and the
worker log shows the same task `received`/`succeeded` sequence as above.
Only one Beat instance may run against the local file-based schedule
(`/tmp/celerybeat-schedule` inside the container) at a time; running two
corrupts its last-run bookkeeping. Stop and remove `beat` afterward:

```bash
docker compose --env-file backend/.env --profile beat stop beat
docker compose --env-file backend/.env --profile beat rm -f beat
```

### Conventions for future domain tasks

These are established by this issue for later domain work (ADR-0005), not
implemented as product behavior here:

- dispatch a task after its triggering database transaction commits (e.g.
  `transaction.on_commit`), never before — a worker must not load state
  that has not been committed yet;
- design task effects to be idempotent; a task may be delivered or executed
  more than once (`CELERY_TASK_ACKS_LATE`/`CELERY_TASK_REJECT_ON_WORKER_LOST`
  above make this a real, not theoretical, possibility;
- pass entity identifiers as task parameters, not large serialized objects;
- bound retries explicitly (`autoretry_for`, `max_retries`, backoff) for
  transient failures; do not retry indefinitely on deterministic failures;
- keep tasks thin adapters that call an application service, matching
  `diagnostics/tasks.py` and `diagnostics/application.py` here.

## Health endpoints (liveness and readiness)

`GET /health/live` and `GET /health/ready` are unauthenticated, GET-only
(other methods return 405) and require no request body. Neither persists
data, enqueues work, or requires PostgreSQL/Redis credentials in the
response. They live in `backend/health/` — a plain URL/view package like
`backend/api/`, not a registered Django app — and are wired at the project
root in `config/urls.py`, not under `/api/`, because they are process/
infrastructure endpoints, not part of the product API.

### `/health/live`

Always returns **200** while the process can handle requests, independent of
every external service by construction: the view calls no dependency probe
at all.

```json
{"status": "ok"}
```

### `/health/ready`

Probes only the dependencies strictly required to serve today's traffic,
each with its own short-lived connection bounded by
`HEALTH_CHECK_TIMEOUT_SECONDS` (2s; `config/common.py`) — a slow or
unreachable dependency fails the probe instead of hanging the request.

| Dependency | Required to serve traffic | Failure behavior |
| --- | --- | --- |
| PostgreSQL | Yes | `status: "unavailable"`, HTTP **503** |
| Redis | No | `status: "degraded"`, HTTP **200** |

```json
{
  "status": "ready",
  "dependencies": {
    "database": {"status": "healthy", "required": true},
    "redis": {"status": "healthy", "required": false}
  }
}
```

**The required-vs-degraded decision (issue #5 scope item):** no HTTP endpoint
exists yet that dispatches a Celery task synchronously as part of its
response (ADR-0005's dispatch-after-commit convention is deliberately
async). PostgreSQL is the domain source of truth and every future endpoint
will need it; Redis failure only degrades background/async processing
(ADR-0005), not the ability to serve an HTTP response. So PostgreSQL failure
is `unavailable`/503; Redis failure is `degraded`/200 — ordinary request
traffic continues, only work that would have been queued cannot run until
Redis recovers. This is a decision about today's endpoint set; a future
endpoint that synchronously depends on Redis would need to revisit it, with
a new ADR if the tradeoffs warrant one.

Worker/Beat process liveness is explicitly outside this contract: `/health/ready`
probes broker (Redis) reachability only, never whether a worker is consuming
from it (see the [worker infrastructure](#worker-infrastructure-redis-celery-worker-optional-beat)
section above for that — `pytest -m celery_smoke` and the manual Compose
walkthrough there).

Responses are sanitized: only the fixed `status: "healthy"/"unhealthy"`
enum and the fixed `required` boolean are returned per dependency — no
hostnames, credentials or exception details, verified by
`tests/test_health.py`.

### Compose integration

The `backend` service's healthcheck calls `/health/ready` (not `/health/live`):
a meaningful "can this backend actually serve traffic" signal for
`docker compose up --wait`, not just "the process started". No service
`depends_on` backend, so a transient dependency outage here cannot cascade
into another service failing to start; it only delays/fails `--wait` and
shows as `unhealthy` in `docker compose ps` until the dependency recovers.
Verified locally: stopping `postgres` while `backend` is running turns
`/health/ready` into 503 immediately (connection refused) while `/health/live`
stays 200, and the container's aggregate Docker health state flips to
`unhealthy` after `retries` (10) consecutive failed checks (~30s at the
configured 3s interval) — tolerant of a brief blip, not a hair-trigger.

### Tests

```bash
uv run --locked pytest tests/test_health.py -v
```

Dependency-failure tests point the PostgreSQL/Redis probe at `10.255.255.1`
(a reserved, silently-dropping test address) rather than mocking the
probe's return value, so they exercise the real bounded-timeout connect
path, not just the response-building logic around it; each asserts wall-clock
elapsed time stays under the configured timeout plus margin.

## Authentication

The mobile API authenticates with **stateless JWT bearer tokens**
(`djangorestframework-simplejwt`), not cookies/sessions — see
[ADR-0009](../docs/adr/0009-jwt-mobile-authentication.md) for the full
decision, alternatives considered, and the recorded residual-validity
contract for logout. This section is the executable/operational half of
that ADR.

| Method | Path | Auth | Request body | Success | Failure |
| --- | --- | --- | --- | --- | --- |
| `POST` | `/api/auth/login` | None | `{"username", "password"}` | 200 `{"access", "refresh"}` | 400, generic `"Invalid credentials."` for wrong password, unknown username **or** inactive account |
| `POST` | `/api/auth/refresh` | None (refresh token is the credential) | `{"refresh"}` | 200 `{"access"}` | 401 if expired, blacklisted or wrong token type |
| `POST` | `/api/auth/logout` | None (refresh token is the credential) | `{"refresh"}` | 205, empty body | 400 if missing/invalid/already-blacklisted |
| `GET` | `/api/auth/me` | `Authorization: Bearer <access>` | — | 200 `{"id", "username"}` | 401 if missing/invalid/expired |

Access tokens last 15 minutes; refresh tokens last 14 days
(`SIMPLE_JWT` in `config/common.py`). `/api/auth/me` returns only `id` and
`username` — no email, name or permission fields exist on the account yet.

### Local account provisioning

No registration endpoint exists (issue #6 excludes it). Provision a local
account with Django's own tooling:

```bash
# Interactive, prompts for username/password:
docker compose --env-file backend/.env run --rm backend python manage.py createsuperuser
# Or non-interactively, e.g. for a throwaway local test account:
docker compose --env-file backend/.env run --rm backend python manage.py shell -c "
from django.contrib.auth import get_user_model
get_user_model().objects.create_user(username='local-dev', password='replace-with-a-real-password')
"
```

### Executable request examples

Against the Compose stack (`docker compose --env-file backend/.env up -d backend`,
published on `http://127.0.0.1:8000`):

```bash
# Login
curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "local-dev", "password": "replace-with-a-real-password"}'
# {"access": "...", "refresh": "..."}

# Current user (replace $ACCESS with the value above)
curl -s http://127.0.0.1:8000/api/auth/me -H "Authorization: Bearer $ACCESS"
# {"id": 1, "username": "local-dev"}

# Refresh (replace $REFRESH with the value from login)
curl -s -X POST http://127.0.0.1:8000/api/auth/refresh \
  -H 'Content-Type: application/json' \
  -d "{\"refresh\": \"$REFRESH\"}"
# {"access": "..."}

# Logout — blacklists $REFRESH; the access token above keeps working until
# its own 15-minute expiry (documented residual-validity window, ADR-0009)
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/api/auth/logout \
  -H 'Content-Type: application/json' \
  -d "{\"refresh\": \"$REFRESH\"}"
# 205
```

### Tests

```bash
uv run --locked pytest tests/test_authentication.py -v
```

Covers: valid/invalid/unknown/inactive login (with byte-identical generic
failure responses), password never appearing in any response, current-user
with no/malformed/expired/valid tokens, refresh (including rejecting an
access token used as a refresh token), logout (missing field, already-used
refresh token, blocking further refresh), the documented residual-validity
window on an already-issued access token after logout, and that neither
login nor logout requires a CSRF token (`Client(enforce_csrf_checks=True)`).

## Feed impressions

`POST /api/feed-impressions` accepts a client-reported qualified exposure of a
Story card on HOME_FEED ([ADR-0012](../docs/adr/0012-qualified-feed-impressions.md)).
It is not a view count, click, vote or proof of attention. The request/result
contract, validated limits and client-telemetry limits are in the
[Mobile Feed architecture](../docs/architecture/mobile-feed.md#feedimpression-server-policy-43).
There is no endpoint that lists impressions and no public metric.

```bash
curl -s -X POST http://127.0.0.1:8000/api/feed-impressions \
  -H "Authorization: Bearer $ACCESS" -H 'Content-Type: application/json' \
  -d '{"events":[{"event_id":"4f4bb18e-b0e0-4e7f-8cf8-849b7013fa63","story_id":"1","feed_session_id":"dd42a0ee-0727-4796-bd11-dba56f1c498b","position":0,"surface":"HOME_FEED","policy_version":1,"occurred_at":"'"$(date -u +%Y-%m-%dT%H:%M:%SZ)"'"}]}'
# {"results":[{"event_id":"4f4bb18e-...","outcome":"accepted","code":null}]}
# Repeating the same command returns "duplicate" and changes nothing.
```

The account and `received_at` come from the server. Invalid structure returns
400 and writes nothing, bodies over 32 KiB return 413, and more than 60 batches
per minute per account returns 429 with `Retry-After`. The throttle counters
live in Django's default per-process cache, so with several processes the bound
is advisory; it is an operational limit, not fraud protection.

### Retention runbook

Rows are kept for `READING_IMPRESSION_RETENTION_DAYS` (30) days by
`received_at`. **Nothing deletes them automatically**: retention holds only if an
operator runs the prune command regularly (for example daily from the host's
own scheduler). Deleting an account removes its impressions immediately.

```bash
# Dry run (the default): counts eligible rows and prints the fixed cutoff.
uv run --locked --env-file .env python manage.py reading_prune_impressions
# Dry run: 1200 FeedImpression row(s) received before 2026-08-25T09:00:00+00:00 are eligible; ...

# Apply with that same cutoff, 1000 rows per transaction (the default).
uv run --locked --env-file .env python manage.py reading_prune_impressions \
  --apply --before 2026-08-25T09:00:00+00:00
# Deleted 1200 FeedImpression row(s) received before 2026-08-25T09:00:00+00:00 in 2 batch(es); 0 eligible row(s) remain.
```

`--batch-size` (1–10000) bounds each delete transaction and `--max-batches`
bounds one run; re-running with the same `--before` resumes where it stopped.
`--before` may never be later than the `--days` cutoff, so the command cannot
delete rows still inside retention. Logs record only counts, the cutoff and
duration.
