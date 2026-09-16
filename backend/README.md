# Backend bootstrap

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

Tags and manifest digests were checked against their registries. Python 3.14.4
preserves the backend runtime; PostgreSQL 17 on Bookworm provides a maintained
pgvector image with the familiar `/var/lib/postgresql/data` layout. The database
image supports Linux amd64 and arm64. See the
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

The stack covers the backend and PostgreSQL/pgvector only. It retains the
issue #1 custom User and package boundaries. Database runtime validation is now
possible using Compose; host-only checks still require a reachable configured
PostgreSQL to verify applied migration history. Health endpoints, authentication
API, workers, mobile, semantic features and CI remain separate issues.

See [module boundaries](../docs/architecture/module-boundaries.md).
