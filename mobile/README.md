# Mobile bootstrap

An Expo/TypeScript application shell for Pulso's React Native mobile app. This
is a bootstrap (issue #7): a reproducible shell other mobile issues extend, not
a product screen. No Story feed, Opinion UI, fake dataset or authentication
flow is implemented here — see the [root README](../README.md) for the
product this shell will eventually host, and
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

| Target | Command | Requirement |
| --- | --- | --- |
| Android (emulator or device) | `npx expo start --android` (or press `a`) | Android Studio emulator, or the Expo Go app on a physical device |
| iOS simulator | `npx expo start --ios` | macOS with Xcode |
| Web | `npx expo start --web` | Nothing extra; runs in a browser |

The shell renders (a single "Pulso" screen) without a running backend: it
never calls the API on startup, only displays the configured base URL as
text — see below.

### TypeScript and lint

```bash
npm run typecheck   # tsc --noEmit
npm run lint         # expo lint (ESLint); not yet configured — issue #8
```

`typecheck` is this issue's validation gate. `lint`'s underlying ESLint
config/dependencies are established by issue #8 (mobile quality and test
tooling), not this bootstrap; running it today prompts to install them
interactively.

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

| Running on | `EXPO_PUBLIC_API_BASE_URL` | Why |
| --- | --- | --- |
| Web (`expo start --web`) | `http://localhost:8000` (default) | Browser and backend share the host's network namespace |
| Android **emulator** | `http://10.0.2.2:8000` | The emulator's own loopback alias for the host machine; `localhost` inside the emulator means the emulator itself, not your machine |
| iOS **simulator** | `http://localhost:8000` | The simulator shares the host's network namespace, unlike the Android emulator |
| Physical device (either OS), via Expo Go | `http://<host-LAN-IP>:8000` | The device is a separate machine on the network; it cannot resolve `localhost` as your development machine. The backend's Compose setup only publishes to `127.0.0.1` (backend/README.md) — reconfigure that publish to your LAN interface, or use `expo start --tunnel`, to reach it from a physical device |

If unset, `src/config/env.ts` falls back to `http://localhost:8000` (the web/
default case) so the shell still renders with no `.env` file at all.

## Project structure

```text
src/
  app/            expo-router file-based routes; only screens/layouts here
    _layout.tsx   root layout (SafeAreaProvider, status bar, Stack)
    index.tsx     the landing screen
  config/
    env.ts        public, build-time-inlined configuration (API base URL)
assets/           app icon and splash images
```

`src/app` is intentionally the only place route files live, matching
`expo-router`'s file-based routing convention: adding a new screen means
adding a file here, ready for future navigation without restructuring.
