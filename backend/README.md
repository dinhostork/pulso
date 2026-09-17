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

## News ingestion settings

News source endpoints are validated against all resolved IP addresses when
saved, and the shared HTTP fetcher checks each request and redirect again.
Only HTTP(S) targets and RSS/XML/JSON/text feed media types are accepted;
`text/html` is refused. RSS and JSON Feed parsers arrive in later issues.

| Setting | Default | Behavior |
| --- | --- | --- |
| `NEWS_FETCH_ALLOW_PRIVATE_NETWORKS` | `false` | Only environment override. `true` permits private/loopback targets for controlled local tests; normal development and deployment should keep it `false`. |
| `NEWS_FETCH_MAX_RESPONSE_BYTES` | 5 MiB | Rejects oversized `Content-Length` before reading; decoded gzip/deflate output is bounded during decompression. Unknown-length or compressed streams are refused when decoded content reaches the cap. |
| `NEWS_FETCH_MAX_REDIRECTS` | 5 | Each hop is checked; HTTPS-to-HTTP downgrade is refused. |
| `NEWS_FETCH_CONNECT_TIMEOUT_SECONDS` | 5 | Connection timeout. |
| `NEWS_FETCH_READ_TIMEOUT_SECONDS` | 15 | Per-read timeout. |
| `NEWS_FETCH_WRITE_TIMEOUT_SECONDS` | 5 | Write timeout. |
| `NEWS_FETCH_POOL_TIMEOUT_SECONDS` | 5 | Connection-pool timeout. |

The last six settings are fixed application constants, not environment knobs.
`config.settings_test` enables the private-network exception for the future
loopback fixture server (#21); policy tests explicitly exercise both flag
values with fake DNS. The override does not disable scheme, redirect, size,
media-type or timeout checks. DNS answers are checked before connecting, but
the HTTP client resolves again at connection time. DNS rebinding in that gap
remains a known risk; no IP pinning is implemented here.

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

There are no application HTTP endpoints yet. With the example `DEBUG=true`
and an empty route registry, Django serves its installation page with **200** at
`http://127.0.0.1:8000/api/`; with debug disabled, an unmatched path returns **404**.
This can verify the HTTP listener, but it is not a health API. Startup and
database checks are separate.

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
configured PostgreSQL to verify applied migration history. Mobile, semantic
features and CI remain separate issues.

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
calls the application function directly and calls the task with `.apply()`
(synchronous, in-process, no broker). The real-broker check lives in
`tests/test_celery_smoke.py`, marked `celery_smoke` and excluded from the
default run through `pyproject.toml`'s `addopts` — it requires a separately
running worker consuming the same Redis instance used by `config.settings_test`
(logical DB 1, isolated from development DB 0):

```bash
docker compose --env-file backend/.env.example --profile test up -d --wait --wait-timeout 90 postgres-test redis
cd backend
DJANGO_SETTINGS_MODULE=config.settings_test uv run --locked celery -A config worker --loglevel=INFO --concurrency=2 &
uv run --locked pytest -m celery_smoke
```

The check calls `.get(timeout=10)` on the dispatched result: it fails with a
clear message within that 10s bound if no worker consumes the task, and it
re-dispatches to assert that repeated delivery still leaves domain state
(`User` count) unchanged. Stop the background worker afterward; it is not
part of the normal test suite and does not start automatically.

### Verifying the optional Beat schedule

Beat is profile-gated (`profiles: ["beat"]`) and its diagnostic schedule is
disabled unless explicitly enabled; neither runs during normal startup, and
no product schedule exists yet. To verify it locally:

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
