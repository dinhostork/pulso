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

## Current validation limits

`manage.py check` can validate this bootstrap without a database server.
`makemigrations --check --dry-run` compares model state with migration files;
it may also warn that PostgreSQL migration history cannot be checked when no
server is running. A zero exit status with “No changes detected” validates model
consistency only, not applied database history.

The initial `accounts` migration declares the custom user before future domain
migrations depend on it. Database-generated integer identity and Django password
hashing are retained. No authentication endpoint or mobile credential mechanism
is selected here.

PostgreSQL infrastructure, pgvector setup and actual migration execution belong
to [issue #2](https://github.com/dinhostork/pulso/issues/2). This issue does not
provide Compose services or claim database runtime validation. All API paths
currently have no endpoints; health and authentication endpoints arrive later.
Quality/test tooling and CI belong to later Foundation issues.

See [module boundaries](../docs/architecture/module-boundaries.md) for package
responsibilities and the mapping to the eight accepted ADRs.
