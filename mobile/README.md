# Mobile bootstrap

> Setting up the whole project (backend + mobile) for the first time? Start
> at [`docs/development.md`](../docs/development.md) instead; come back here
> for mobile-specific depth.

An Expo/TypeScript application shell for Pulso's React Native mobile app. The
reproducible bootstrap now includes the Phase 3 transport, DTO-decoding and
server-state boundary, but no Story Feed screen, fake runtime dataset or
authentication flow. See the [root README](../README.md) for the product this
shell will eventually host, and
[docs/architecture/module-boundaries.md](../docs/architecture/module-boundaries.md)
for backend module ownership.

## Runtime and dependencies

Use Node **24.20.0** (`.nvmrc`) and npm (bundled with Node; this project was
set up with npm **11.19.0**). Node 24 is the current LTS line. `nvm` reads
`.nvmrc` directly; other version managers (volta, fnm, mise) either read the
same file or accept it as `.node-version` via a symlink/config of your own.

`package.json` declares dependencies; `package-lock.json` records exact
resolved versions and is the single committed lockfile — install with `npm ci`
(or `npm install`, which also respects the lockfile when it is already
present and consistent) rather than a different package manager, so
everyone resolves the same dependency tree.

From the repository root:

```bash
cd mobile
nvm install    # or: nvm use, if you already have 24.20.0
npm ci
npx expo-doctor
```

`expo-doctor` runs Expo's own project health checks (SDK/peer-dependency
consistency, config validity); it must report no issues on a clean install.

## Starting the app

```bash
npx expo start
```

This starts the Metro bundler and prints a QR code plus keyboard shortcuts.
No paid build service, Expo account or EAS project is required for this —
Expo Go (a free app from the Play Store / App Store) loads the JS bundle
directly over the local network or an emulator/simulator's loopback network.

| Target                       | Command                                   | Requirement                                                      |
| ---------------------------- | ----------------------------------------- | ---------------------------------------------------------------- |
| Android (emulator or device) | `npx expo start --android` (or press `a`) | Android Studio emulator, or the Expo Go app on a physical device |
| iOS simulator                | `npx expo start --ios`                    | macOS with Xcode                                                 |
| Web                          | `npx expo start --web`                    | Nothing extra; runs in a browser                                 |

The shell renders without a running backend and makes no request on startup.
It does not print the configured origin or use contract fixtures as runtime
fallback content.

See "Quality and testing" below for the full set of checks (lint, format,
typecheck, tests) and how to run them without any interactive prompts.

### A note on `npm audit`

A clean `npm ci` currently reports moderate-severity advisories
(`decode-uri-component`, `uuid`) several layers deep inside Expo's own CLI/
config-plugin toolchain (`@expo/cli` → `@expo/config-plugins` → `xcode`/
`query-string`). These affect local build tooling, not the shipped app
bundle, and `npm audit fix --force`'s suggested fix is to downgrade to a
years-old `expo`/`expo-router` release — a regression, not a fix. Re-check
with `npx expo-doctor` and `npm audit` after any dependency bump rather than
force-applying this.

## API base URL

The backend URL is public client configuration, not a secret: it is read
from `process.env.EXPO_PUBLIC_API_BASE_URL` (`src/config/env.ts`). Expo/
Metro inline every `EXPO_PUBLIC_`-prefixed variable into the JS bundle at
build time — treat any value behind that prefix as visible to anyone who
has the app, the same way a URL baked into a mobile binary always is; never
put a credential behind it.

Copy the example and edit it — no source change is needed to point the app
at a different backend:

```bash
cp mobile/.env.example mobile/.env
```

| Running on                               | `EXPO_PUBLIC_API_BASE_URL`        | Why                                                                                                                                                                                                                                                                                                          |
| ---------------------------------------- | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Web (`expo start --web`)                 | `http://localhost:8000` (default) | Browser and backend share the host's network namespace                                                                                                                                                                                                                                                       |
| Android **emulator**                     | `http://10.0.2.2:8000`            | The emulator's own loopback alias for the host machine; `localhost` inside the emulator means the emulator itself, not your machine                                                                                                                                                                          |
| iOS **simulator**                        | `http://localhost:8000`           | The simulator shares the host's network namespace, unlike the Android emulator                                                                                                                                                                                                                               |
| Physical device (either OS), via Expo Go | `http://<host-LAN-IP>:8000`       | The device is a separate machine on the network; it cannot resolve `localhost` as your development machine. The backend's Compose setup only publishes to `127.0.0.1` (backend/README.md) — reconfigure that publish to your LAN interface, or use `expo start --tunnel`, to reach it from a physical device |

If unset, `src/config/env.ts` falls back to `http://localhost:8000` (the web/
default case) so the shell still renders with no `.env` file at all.

## Project structure

```text
src/
  api/                fetch transport, normalized errors, DTO decoders and API helpers
  app/                expo-router file-based routes; only screens/layouts here
    _layout.tsx       root layout (single QueryClientProvider, safe area, Stack)
    index.tsx         the landing screen
    __tests__/        tests for files in app/ — see note below
  config/
    env.ts            public, build-time-inlined configuration (API base URL)
  server-state/       TanStack Query client, account-scoped keys and retry policy
assets/               app icon and splash images
```

## API and server-state boundary

`src/api/transport.ts` is the only general HTTP transport. It uses native
`fetch`, accepts approved relative `/api/*` paths, joins them to a validated
origin, applies a 15-second timeout, propagates caller cancellation, handles
empty 204/205 responses and normalizes timeout/network/abort/HTTP/JSON/DTO
failures as `ApiError`. It performs no generic retry. Absolute/foreign paths
are rejected before an Authorization header can reach `fetch`.

Credential hooks expose only access-token lookup, session epoch and one future
401 refresh/replay operation for issue #45. Tokens do not enter URLs, query
keys, public Expo variables, diagnostics or persistent storage here. Native
refresh-token secure storage is deliberately not implemented by #44.

`src/api/decoders.ts` validates the repository contracts in
`../docs/contracts/mobile-feed/`; BigAutoField IDs remain decimal strings and
timestamps/nulls remain their wire values. Invalid server data becomes a
controlled `malformed_dto` failure—there is no fixture or invented-data
fallback. Source navigation additionally rejects non-HTTP(S), credentialed and
literal local/private destinations.

`@tanstack/react-query` 5.103.1 is the sole server-state cache. Its package
metadata supports React 18/19, including this checkout's React 19.2.3. Every
viewer-decorated query key starts with the decimal-string account ID; identity
change cancels/removes that account prefix. Read queries own at most two
retries for network/timeout, 429 or 5xx failures. Transport owns none; auth owns
one replay; the future FeedImpression queue owns delivery retry. Mutations do
not retry by default, while bookmark features may opt into the exported
single retry for idempotent writes. Feed data is bounded to ten in-memory
pages and no query cache is persisted.

`src/app` is intentionally the only place route files live, matching
`expo-router`'s file-based routing convention: adding a new screen means
adding a file here, ready for future navigation without restructuring.
`expo-router` scans every file under `src/app` as a candidate route except
inside a `__tests__` directory (or files starting with `.`) — a `*.test.tsx`
placed directly in `src/app` becomes a real, navigable, exported route
(e.g. `index.test.tsx` shipping as `/index.test`), not just a Jest file.
Discovered via `npx expo export --platform web` during issue #10's
fresh-checkout walkthrough; keep any future `src/app/**` test alongside
its screen but inside a `__tests__` folder, never a bare sibling file.

## Quality and testing

`npm ci` installs every tool below; none require a running backend, an
emulator/simulator, or network access beyond the initial install.

```bash
npm run lint          # ESLint (eslint-config-expo)
npm run format:check  # Prettier, check only
npm run typecheck     # tsc --noEmit
npm run test:ci       # Jest, non-interactive
```

All four exit non-zero on failure and print machine-readable output;
they are this issue's validation gate and the commands CI should reuse.
`npm run format` (no `:check`) applies Prettier's fixes in place.

### Linting and formatting

ESLint uses Expo's own flat config (`eslint-config-expo`), with
`eslint-config-prettier` layered on top so ESLint never fights Prettier
over formatting — ESLint owns code-quality rules, Prettier owns layout.
`.prettierrc.json` only overrides `printWidth` (100, matching the
backend's Ruff `line-length`); everything else is Prettier's default.

### Tests

Jest uses the `jest-expo` preset (jsdom-free, React Native-aware
transforms) with `@testing-library/react-native` for component rendering.
All tests run fully offline. In addition to shell/environment coverage, API
tests exercise origin/path safety, timeout/cancellation, empty responses,
normalized failures and 401 replay; decoder tests consume the repository JSON
contracts; server-state tests prove account isolation, retry ownership and the
page cap.

The original bootstrap tests still verify:

- `src/app/__tests__/index.test.tsx` renders the landing screen and asserts its
  visible text without exposing the API origin.
  `@testing-library/react-native`'s `render` is asynchronous (`await
render(...)`) as of v14; a call site that forgets `await` fails with a
  clear "`render` function has not been called" error rather than a silent
  false pass, which is exactly how this was caught while writing the test.
- `src/config/env.test.ts` re-evaluates `src/config/env.ts` with
  `EXPO_PUBLIC_API_BASE_URL` unset and set (via `jest.resetModules()` +
  `require`, since the module reads `process.env` once at import time),
  asserting both the documented `localhost:8000` fallback and that an
  explicit value is honored — the "public-configuration validation
  coverage" scope item.

`npm test` runs Jest in local/watch mode (interactive); `npm run test:ci`
(`jest --ci --watchAll=false --passWithNoTests`) is the non-interactive
form for scripts and CI — it terminates on its own regardless of terminal
type, per this issue's "CI test mode terminates without watch mode or
interactive input" acceptance criterion.

### Proving failures are caught

Not part of the committed suite — this is what was actually run once to
confirm the tooling fails loudly, the same way `not_shared/validation/`
records for the backend:

```bash
# A deliberate type error:
echo 'const n: number = "not a number"; export default n;' > src/config/_probe.ts
npm run typecheck   # exits 2
rm src/config/_probe.ts

# A deliberate failing assertion:
printf 'test("x", () => { expect(true).toBe(false); });' > src/config/_probe.test.ts
npx jest _probe     # exits 1
rm src/config/_probe.test.ts
```
