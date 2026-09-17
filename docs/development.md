# Local development guide

One consolidated walkthrough for the whole Foundation: backend, worker,
scheduler, mobile shell and the contribution workflow. It links to
[`backend/README.md`](../backend/README.md) and
[`mobile/README.md`](../mobile/README.md) for stack-specific depth rather
than repeating it; this guide is the thing a new contributor follows once,
start to finish, on a clean checkout.

Every command below was actually run, in order, against a genuine fresh
`git clone` of this repository, while writing this guide (issue #10). Where
useful, the real observed output is inline rather than a description of
what "should" happen.

## Prerequisites

| Tool | Version | Why |
| --- | --- | --- |
| Docker Engine + Compose v2.20+ | — | Runs PostgreSQL/pgvector, Redis, the backend, worker and (optional) Beat |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | 0.12.13 | Backend dependency management; also provisions Python 3.14.4 itself |
| Node (via nvm/volta/fnm) | 24.20.0 (`mobile/.nvmrc`) | Mobile toolchain |
| npm | bundled with Node (11.19.0 at setup time) | Mobile package manager |
| git | — | — |

No paid account, API key or provider credential is required for anything
in this guide. `backend/.env.example` and `mobile/.env.example` contain
only public, disposable local values, safe to copy as-is.

## Quick start

Run from the **repository root** unless a step says otherwise.

### 1. Environment files

```bash
cp -n backend/.env.example backend/.env
cp -n mobile/.env.example mobile/.env
```

`-n`/`cp -n` never overwrites a file that already exists — safe to re-run.
Both `.env` files are git-ignored; both `.env.example` files are the
source of truth for what a fresh contributor gets by default.

### 2. Backend, database, broker and worker

```bash
docker compose --env-file backend/.env config --quiet
docker compose --env-file backend/.env build backend
docker compose --env-file backend/.env up -d --wait --wait-timeout 90 postgres redis
docker compose --env-file backend/.env run --rm backend python manage.py migrate --noinput
docker compose --env-file backend/.env up -d --wait --wait-timeout 60 backend worker
```

Observed on a clean checkout: `migrate` applies 29 migrations — Django's
own `auth`/`contenttypes`/`sessions` migrations, `accounts.0001_initial`,
`database.0001_enable_vector`, and the twelve `token_blacklist.*`
migrations bundled with `djangorestframework-simplejwt`;
`up --wait` reports all four containers `Healthy` (`postgres`, `redis`,
`backend`, `worker`) before returning — `backend`'s own healthcheck
(issue #5) is what makes that meaningful rather than just "the process
started" (see [backend/README.md's Compose integration](../backend/README.md#compose-integration)).

### 3. Verify pgvector

```bash
docker compose --env-file backend/.env exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' <<'SQL'
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
SELECT '[0,0,0]'::vector <-> '[3,4,0]'::vector AS l2_distance;
SQL
```

Observed:

```text
 extname | extversion
---------+------------
 vector  | 0.8.6

 l2_distance
-------------
           5
```

`5` is exact: `sqrt(3² + 4²)`. No embedding model or table involved — just
proof the extension and its distance operator work.

### 4. Verify liveness and readiness

```bash
curl -s http://127.0.0.1:8000/health/live
curl -s http://127.0.0.1:8000/health/ready
```

Observed:

```json
{"status": "ok"}
{"status": "ready", "dependencies": {"database": {"status": "healthy", "required": true}, "redis": {"status": "healthy", "required": false}}}
```

See [backend/README.md's Health endpoints](../backend/README.md#health-endpoints-liveness-and-readiness)
for the full contract, including what happens when a dependency is down.

### 5. Provision a local account and verify login/logout

No registration endpoint exists by design (issue #6). Create one with
Django's own tooling:

```bash
docker compose --env-file backend/.env run --rm backend python manage.py shell -c "
from django.contrib.auth import get_user_model
get_user_model().objects.create_user(username='local-dev', password='replace-with-a-real-password')
"
```

```bash
LOGIN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "local-dev", "password": "replace-with-a-real-password"}')
ACCESS=$(echo "$LOGIN" | python3 -c "import json,sys;print(json.load(sys.stdin)['access'])")
REFRESH=$(echo "$LOGIN" | python3 -c "import json,sys;print(json.load(sys.stdin)['refresh'])")

curl -s http://127.0.0.1:8000/api/auth/me -H "Authorization: Bearer $ACCESS"
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8000/api/auth/logout \
  -H 'Content-Type: application/json' -d "{\"refresh\": \"$REFRESH\"}"
```

Observed: `/api/auth/me` → `{"id":1,"username":"local-dev"}`; logout → `205`.
See [backend/README.md's Authentication section](../backend/README.md#authentication)
for the full contract, including the documented residual-validity window
after logout ([ADR-0009](adr/0009-jwt-mobile-authentication.md)).

### 6. Verify real asynchronous execution (a separate worker, not eager)

```bash
docker compose --env-file backend/.env run --rm backend python manage.py shell -c "
from diagnostics.tasks import diagnostic_ping
print(diagnostic_ping.delay().get(timeout=10))
"
docker compose --env-file backend/.env logs --tail=10 worker
```

Observed: the shell prints `{'ok': True, 'executed_at': '...'}`, and the
`worker` container's own log — a genuinely separate process — shows
`received` then `succeeded` for that task ID. See
[backend/README.md's worker infrastructure section](../backend/README.md#worker-infrastructure-redis-celery-worker-optional-beat)
for the queue/timeout/retry conventions this establishes for future tasks.

### 7. Optional: verify the Celery Beat diagnostic schedule

Disabled by default; normal startup schedules nothing. To verify it:

```bash
CELERY_DIAGNOSTIC_BEAT_ENABLED=true docker compose --env-file backend/.env --profile beat up -d beat
# wait up to ~30s, then:
docker compose --env-file backend/.env logs beat worker
```

Observed: the `beat` log shows `Scheduler: Sending due task diagnostic-ping`
roughly 30 seconds after start, and the `worker` log shows a matching
`received`/`succeeded` pair at the same timestamp — Beat actually
triggered a real worker execution, not merely started without error.
Stop it afterward (it is not part of normal startup):

```bash
docker compose --env-file backend/.env --profile beat stop beat
docker compose --env-file backend/.env --profile beat rm -f beat
```

### 8. Mobile shell

```bash
cd mobile
nvm install    # or: nvm use
npm ci
npx expo-doctor
npx expo start
```

Press `a` for the Android emulator, `w` for web, or scan the QR code with
Expo Go on a physical device. The shell renders without the backend
running at all (it makes no network call on startup — see
[mobile/README.md's API base URL section](../mobile/README.md#api-base-url)
for the emulator/simulator/device host-address differences, which matter
once you do want it talking to the backend above).

Verified on an actual Android emulator (Pixel 6, Android 14/API 34,
`google_apis` x86_64 system image, booted headless with KVM acceleration):

```bash
sdkmanager "system-images;android-34;google_apis;x86_64"
avdmanager create avd -n pulso-test -k "system-images;android-34;google_apis;x86_64" -d pixel_6
emulator -avd pulso-test -no-window -no-audio -no-boot-anim
adb wait-for-device
npx expo start --android
```

`expo start --android` detected the running emulator, fetched and
installed Expo Go automatically (no prior Expo Go install needed), bundled
the app (1406 modules) and opened it. A screenshot pulled via `adb shell
screencap` shows the real rendered screen — "Pulso" / "Mobile application
shell" / "API base URL: http://localhost:8000" — with the backend **not
running** at the time, confirming the "no network call on startup" claim
above by absence of any error/timeout, not just by reading the source.
`adb logcat` showed no fatal error or crash. Screenshot kept as evidence
in `not_shared/validation/issue10/android-emulator-render.png` — that
whole directory is git-ignored (`not_shared/` in the root `.gitignore`),
the repository's existing convention for local validation artifacts.

## Quality and test commands

These are exactly what CI runs (`.github/workflows/ci.yml`, issue #9) — if
one of these passes locally, the matching check should pass in CI too,
modulo environment differences like a cold Docker image cache.

| Local command | CI check name | What it covers |
| --- | --- | --- |
| `cd backend && uv run --locked ruff check . && uv run --locked ruff format --check .` | `backend` | Lint/format |
| `cd backend && uv run --locked python manage.py check [--settings=config.settings_test]` | `backend` | Django system checks, dev and isolated test settings |
| `cd backend && uv run --locked python manage.py makemigrations --check --dry-run --settings=config.settings_test` | `backend` | Migration drift |
| `cd backend && uv run --locked pytest` | `backend` | Full suite against real PostgreSQL/pgvector + Redis |
| `cd backend && uv run --locked pytest -m celery_smoke` (needs a real worker running — see [backend/README.md](../backend/README.md#automated-real-broker-smoke-check)) | `worker-smoke` | Real-broker Celery round trip |
| `cd mobile && npx expo-doctor` | `mobile` | Expo/SDK/peer-dependency health |
| `cd mobile && npm run lint` | `mobile` | ESLint |
| `cd mobile && npm run format:check` | `mobile` | Prettier |
| `cd mobile && npm run typecheck` | `mobile` | `tsc --noEmit` |
| `cd mobile && npm run test:ci` | `mobile` | Jest, non-interactive |

Backend commands need `postgres-test`/`redis` running first:

```bash
docker compose --env-file backend/.env.example --profile test up -d --wait --wait-timeout 90 postgres-test redis
```

Observed while writing this guide, on the same fresh checkout: all of the
above passed — `32 passed, 1 deselected` for the main backend suite,
`1 passed` for the real-broker smoke check, and a clean `expo-doctor`
(21/21), lint, format, typecheck and Jest run for mobile (`3 passed`).

### A bug the walkthrough itself found

Running `npx expo export --platform web` as part of this walkthrough
showed `/index.test` as a real exported static route, alongside `/` and
`/_sitemap` — `mobile/src/app/index.test.tsx` (added by issue #8) was
sitting directly in `expo-router`'s file-based route directory, so it
shipped as an actual (broken) screen, not just a Jest file. Fixed as part
of this issue by moving it to `mobile/src/app/__tests__/index.test.tsx` —
`expo-router` ignores `__tests__` directories — and re-verified: the
export goes back to exactly `/`, `/_sitemap`, `/+not-found`, and
`npm run test:ci` still passes from the new location. This is exactly why
this issue's acceptance criteria call for a literal walkthrough rather
than trusting each stack's own isolated test suite: nothing in issue #8's
own Jest/ESLint/TypeScript checks could have caught a routing-level
side effect that only `expo export` surfaces.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `manage.py` / Compose fails with "Missing required environment setting" | `backend/.env` missing or a required key blank | `cp backend/.env.example backend/.env`; never edit `.env.example` itself |
| `Bind for 127.0.0.1:PORT failed: port is already allocated` | A previous Compose project's container is still using that host port (dev `redis`/`postgres` and the `test` profile share default ports across different Compose *project names*, i.e. different working directories) | `docker ps -a`, stop/remove the conflicting container, or override the port (`POSTGRES_PORT`, `REDIS_PORT`, `TEST_POSTGRES_PORT`, `TEST_REDIS_PORT`) — see backend/README.md's isolated-validation-project pattern |
| `docker compose ... up --wait` times out | An image pull is slow, or a genuine startup failure | `docker compose --env-file backend/.env logs --tail=50 <service>`; healthchecks fail loud, not silent |
| `/health/ready` returns 503 | PostgreSQL unreachable/down | Check `docker compose ... logs postgres`; `/health/live` should still be `200` — if it isn't, the process itself is down, not just a dependency |
| `pytest -m celery_smoke` fails with "No worker consumed the task within 10s" | No worker is running against the same (isolated test) Redis DB | Start one first: `DJANGO_SETTINGS_MODULE=config.settings_test uv run --locked celery -A config worker --loglevel=INFO` |
| Mobile `expo start` can't reach the backend from an emulator/device | Wrong `EXPO_PUBLIC_API_BASE_URL` for that target | See [mobile/README.md's API base URL table](../mobile/README.md#api-base-url) (`10.0.2.2` for the Android emulator, a LAN IP for a physical device) |
| `npm ci` / `uv sync --locked` fails on a lockfile mismatch | `package.json`/`pyproject.toml` changed without regenerating the lockfile | `npm install` / `uv lock`, review the diff, commit both together |

### Logs

```bash
docker compose --env-file backend/.env logs --tail=50 backend postgres redis worker
docker compose --env-file backend/.env --profile beat logs beat   # only if Beat is running
```

### Restart (preserves data)

```bash
docker compose --env-file backend/.env restart postgres redis backend worker
```

### Destructive reset — deletes the local database permanently

```bash
docker compose --env-file backend/.env down --volumes
```

Only the named Compose project's own `postgres_data` volume is removed;
this does not touch any other project's containers/volumes. Re-run the
Quick start's build/migrate/start steps afterward.

## Contribution workflow

```text
milestone (e.g. v0.1.0 — Foundation)
  ↓
issue (one Foundation issue = one reviewable unit of work)
  ↓
branch (from an up-to-date default branch)
  ↓
commits (small, one logical change each)
  ↓
pull request (references the issue; CI runs backend/worker-smoke/mobile — issue #9)
  ↓
protected default branch (merge only after review + passing checks)
```

Branch naming follows the issue's suggested prefix (from the Foundation
planning; `<n>` is the GitHub issue number):

| Prefix | Used for | Example from this repo |
| --- | --- | --- |
| `feat/<n>-<slug>` | New capability | `feat/6-authentication-foundation` |
| `chore/<n>-<slug>` | Tooling/infrastructure setup, no new capability | `chore/8-mobile-quality` |
| `fix/<n>-<slug>` | Bug fixes | *(none yet in Foundation)* |
| `ci/<n>-<slug>` | CI/workflow changes | `ci/9-github-actions` |
| `docs/<n>-<slug>` | Documentation-only changes | `docs/10-local-development` |

A PR description includes `Closes #<issue-number>` (or `Fixes`/`Resolves`,
GitHub treats them identically) so merging the PR automatically closes the
issue. Use the [engineering task issue template](../.github/ISSUE_TEMPLATE/task.md)
(`.github/ISSUE_TEMPLATE/task.md`) when opening a new issue — GitHub's
"New issue" chooser offers it automatically on this repository.

### Branch protection: actual state, recorded for maintainers

Checked directly (`gh api repos/dinhostork/pulso/branches/master/protection`)
rather than assumed, since this guide's own acceptance criteria ask for
that. As of this writing, `master` requires:

- a pull request before merging (`required_pull_request_reviews` present);
- **0** approving reviews (`required_approving_review_count: 0`) —
  a PR is required to exist, but nothing currently requires anyone to
  approve it;
- code-owner review (`require_code_owner_reviews: true`) — currently
  **without effect**, because no `CODEOWNERS` file exists in the
  repository for it to consult;
- the branch is additionally hard-locked (`lock_branch: true`);
- force-pushes and branch deletion are disabled.

**Gap:** `required_status_checks` is **not configured at all** — despite
issue #9 now providing three named checks (`backend`, `worker-smoke`,
`mobile`; see [the root README's Continuous integration section](../README.md#continuous-integration)),
nothing requires them to pass before a merge. Separately,
`enforce_admins` is `false`, which is why every Foundation issue's merge
in this repository's history so far was a **direct push to `master`**,
not a reviewed PR merge — GitHub's own response to each of those pushes
explicitly said so: *"Bypassed rule violations for refs/heads/master:
Changes must be made through a pull request. / Cannot change this locked
branch."* Enabling required status checks, requiring at least one
approval, enforcing the rules for admins too, and/or adding a
`CODEOWNERS` file are each a one-click Settings → Branches change a
maintainer can make; none were changed by writing this guide.

## What "done" looks like

Everything in this document was executed, in this order, against one
clean `git clone` of this repository (the commit this guide's branch
started from), on a machine with no prior Pulso state:

1. copy both `.env.example` files;
2. build, start, migrate the backend stack; all four containers report
   `Healthy`;
3. pgvector distance query returns exactly `5`;
4. `/health/live` → `200`, `/health/ready` → `200 ready`;
5. a locally-provisioned account logs in, reads its own `{id, username}`,
   and logs out (`205`);
6. a diagnostic task dispatched from one container completes in the
   separate `worker` container's log;
7. enabling Beat produces a real worker execution within ~30s, then is
   disabled again;
8. `npm ci` + `expo-doctor` + `npx expo start --android` render the shell
   on a real Android emulator (screenshotted), backend not running;
9. every quality/test command in the table above passes, on both stacks;
10. one real defect (the leaked `/index.test` route) was found and fixed
    as a direct result of following this guide, not assumed away.
