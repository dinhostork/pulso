# Phase 3 (Mobile Feed) acceptance record

This records what was verified for Phase 3 (issues #40–#52), how, and what was
not. Automated evidence is reproducible from the repository; native evidence
was collected manually and is stated exactly as observed. Anything not listed
as run was not run.

## Automated evidence

| Layer | Evidence | Command (see the stack READMEs) |
| --- | --- | --- |
| Pipeline → API | `backend/tests/reading/test_reading_loop.py`: the recorded corpus (32 Articles) grouped by the real Story services under matcher v3 into the 16 expected active event groups, then read through the authenticated Feed/detail/source APIs; provenance and derived Story state unchanged by reads and private writes | `uv run --locked pytest tests/reading/test_reading_loop.py` |
| Citation across layers | The same module produces [`reading-loop.json`](../contracts/mobile-feed/README.md) from a corpus Story (IDs renumbered, two server-clock timestamps pinned) and fails if the fixture drifts; `mobile/src/integration/__tests__/readingLoop.test.tsx` decodes it and asserts the displayed citation row's publisher, title and hand-off URL, including a previous-generation citation whose Article left the Story | both suites |
| Private writes | Bookmark PUT/DELETE repeated after a lost response leave one row with the original `saved_at`; a replayed FeedImpression batch is `duplicate` with the stored row unchanged; account B sees and removes none of A's state; an archived Saved Story (real reprocessing) is a removable tombstone and cannot be re-saved | backend suite |
| Reading loop | Real login → Feed → save → detail → Saved → refresh-token restore → Saved still from the server → remove → Feed/detail updated; a second account sees nothing | backend suite |
| Bookmark/refresh ordering | A Feed refresh answered before a save or removal committed, and released after the confirmation, keeps the confirmed state for that Story while other Stories take the response's values (both directions, deterministic held responses) | `npm run test:ci` |
| Mobile loop | Sign in, Feed, detail, citation hand-off, sources, save, Saved, token expiry with single refresh and replay, removal, prior-generation citation, exposure delivery failure and retry with the same event, logout and account switch, through the real transport, decoders, session controller, cache and screens | `npm run test:ci` |
| Feature tests | #41–#51 feature suites, unchanged, plus the #50 bookmark suites | backend `pytest`, mobile `test:ci` |
| Web build | `npx expo export --platform web` succeeds and exports only the Phase 3 routes | CI mobile job |

Recorded results for the commit that closes Phase 3 are in the
[quality gates](#quality-gates) section.

## Native evidence

Collected by hand on one physical device. Nothing here was run on an emulator.

| | |
| --- | --- |
| Device | Motorola moto g17 (physical phone) |
| OS | Android 15 / API 35 |
| Screen | 1080×2400 |
| Runtime | Expo Go 57.0.9, **development mode** (not a release build) |
| Browser | Firefox |
| Backend | Local Docker backend reached through ADB port forwarding |
| Data | 176 Stories, 9 Feed pages; disposable local account |

### Feed (#48) — passed

Real Feed rendering; rapid scrolling across all 9 pages; pagination; one card
per Story; multi-Article Stories; source/article counts; Feed position kept
across detail and back; successful pull-to-refresh; a failed refresh kept the
cards and the session.

Automated only: duplicate Story IDs across pages; the 10-page cache bound.

### Story details and sources (#49) — passed while the process stayed alive

Story detail; source list; publisher opened in the system browser (Firefox as
a separate task); the publisher URL carried no Pulso bearer token; return to
the same Sources screen; Sources → Story → Feed back stack; Feed position
kept.

Automated only: withheld/unavailable publication link; unsafe target
rejection.

### FeedImpressions (#51) — passed

Below 50% visible produced no persisted impression; at or above 50% produced
one; the 1000 ms dwell; one exposure per session; scrolling away and back and
detail and back produced no duplicate; rapid scrolling produced no false
exposure; a fast tap produced none; a successful refresh started a new
session and a failed one kept it; backgrounding reset the dwell; text scale
1.3 and 2.0; a tall card qualified correctly; absolute positions; delivery
retry; the terminal drop after three retries. Persisted rows were checked in
the backend database for the disposable smoke account only.

Automated only: the exact 49% and 999 ms boundaries.

### Bookmarks and Saved (#50) — passed

Same device, runtime, backend path and 176-Story data set, with two
disposable local accounts created for this smoke and deleted afterwards:

- **Save from Feed:** the card changed to "Saved" with "Remove from Saved";
  the Story's detail showed the same state; the Saved tab listed the Story;
  the server held exactly one Bookmark for the account.
- **Remove:** removing it from the Saved tab left the explicit "No saved
  Stories" state on the same route; without a restart the Feed card and the
  detail showed "Save" again and no screen claimed Saved; the server held no
  Bookmark.
- **Restart:** after saving again from the detail, the Expo Go process was
  terminated and cold-started. The session was restored (refresh, then
  `/me`), the Feed card showed Saved and the Saved tab listed the Story, both
  from fresh `GET /api/feed` and `GET /api/bookmarks` responses.
- **Account isolation:** with account A's Bookmark still on the server, A
  signed out and B signed in on the same device: B's Feed showed no Saved
  label or remove control for that Story, and B's Saved tab was empty.
- **Saved and FeedImpressions:** after the Feed's own queued exposures had
  flushed, 25 s on the Saved tab with its card fully visible, including a
  pull-to-refresh, added no FeedImpression row; the backend received only
  `GET /api/bookmarks` in that window.

Not run natively: the archived Saved tombstone (covered by the backend
integration test with real reprocessing and by Jest).

### Session restore — observed

During the process-death case below, the cold restart restored the session
from SecureStore and opened the Feed without a sign-in.

### Not run

| Target or check | Status |
| --- | --- |
| iOS (any device or simulator) | **Not run.** No macOS/Xcode/iOS target was available in the validation environment. This is a recorded platform acceptance exception, not a pass; iOS behavior is covered only by shared code and Jest. |
| Archived Saved tombstone on a device | Not run natively (backend integration and Jest only) |
| TalkBack screen reader | Not run |
| Real mobile-network throttling | Not run |
| Small-screen physical device | Not run (only the 1080×2400 device above) |
| 10-page Feed bound on a device | Not run natively (automated only) |
| Unsafe or withheld publication link on a device | Not run natively (automated only) |
| Android release build | Not run (Expo Go development mode only) |

Web rendering is not native evidence and implies nothing about Android or
iOS.

## Known limitations (v0.4)

### Navigation restoration after OS process death

Story/source navigation state and loaded source pages are in-memory state in
v0.4. Returning from an external browser preserves context while the Pulso
process remains alive.

If the operating system terminates Pulso while the browser is in the
foreground, Pulso performs a cold restart. Authentication can be restored,
but navigation restarts at Feed. Story/source route state and loaded pages
are not persisted in v0.4.

Unsent FeedImpressions may also be lost on process death by design.

Observed on the device above: with the process alive, publisher browser →
back returned to the same Sources route. On two of the round trips Android's
low-memory killer terminated Expo Go while Firefox was in front; Pulso then
cold-started into the Feed with the session restored.

### Defect found during the native smoke (fixed)

Discovered during the physical Android smoke: a device still holding the
refresh token of an account that had since been deleted could not recover by
itself. `POST /api/auth/refresh` returned 500, because the installed SimpleJWT
`TokenRefreshSerializer` loads the token's user without handling a missing
row; the app correctly treated a 5xx as retryable and showed "Pulso could not
reach the server to restore your session" with a Try again that could never
succeed. "Sign out" cleared the credential and recovered.

Fixed after the smoke: the project's refresh endpoint
(`accounts.views.RefreshView` with `accounts.serializers.RefreshSerializer`)
now answers a deleted account's token with the same 401 body SimpleJWT
already returns for an inactive account (`No active account found for the
given token.`). Only the User model's missing-row case is translated; valid,
expired, malformed and blacklisted tokens keep their existing behavior, and
unexpected server failures still surface as errors. The session layer
already treats a 400/401 refresh during restore as terminal: it clears the
stored refresh credential and account-scoped cache state and returns to
sign-in with "Your session has ended. Sign in again."

Regression coverage: `backend/tests/test_authentication.py` (deleted
account → 401 with no user recreated, inactive-account parity, expired and
malformed tokens, a database failure during the lookup not masked as 401) and
`mobile/src/session/__tests__/routing.test.tsx` (cold start with a rejected
refresh token lands on sign-in with the credential cleared and no product
request). The fix was verified at the HTTP and session boundaries only; no
second physical-device run was performed for it.

### Other limitations

- Browser API access is not configured (no CORS); web builds and renders only.
- Exposure delivery is best effort and in memory; there is no offline queue
  for exposures or bookmarks.
- FeedImpression retention is operator-run (`reading_prune_impressions`), not
  automatic.
- Story Engine entity labelling is heuristic: one device smoke showed a Story
  whose entity data classified "Berkshire Hathaway" as a person. The app showed
  the API data faithfully; this is upstream Story Engine behavior, not a Mobile
  Feed defect, and Phase 3 does not compensate for it in rendering.

## Invariant review

Checked against the merged code, not only the documentation.

| Invariant | Holds | Evidence |
| --- | --- | --- |
| Article ≠ Story | Yes | Stories group Articles through `StoryArticle`; the Feed returns one card per Story with its article/source counts (reading-loop test: 32 Articles → 16 cards, counts equal membership); sources list each Article separately |
| Opinion ≠ Perspective | Yes | Neither exists in Phase 3: no model, endpoint or screen; no placeholder UI |
| Position ≠ Perspective | Yes | No Position or Perspective exists; Bookmarks and FeedImpressions carry no stance |
| AI ≠ Source | Yes | Synthesis is labelled "Summary generated by Pulso from the cited publications" with its generation time; sources and citations are publisher Articles only, and a synthesis never appears as a source row |
| One user = one active position per Story | Yes (vacuously) | No positions exist yet; the `(user, story)` Bookmark uniqueness is private reading state, not a position |
| "Represents me" ≠ vote | Yes | No such action exists; Bookmarks and impressions expose no counts and are documented as neither votes nor popularity |
| Recommendation ≠ factual personalization | Yes | `feed_candidates` takes no user and orders by `(created_at, id)` only; two accounts receive the same order and facts, differing only in `viewer.bookmarked` (reading-loop and #47 tests); Recommendation remains future work |

## Expo Doctor

At the start of this batch `npx expo-doctor` reported 21/21. The earlier 20/21
result was Expo SDK patch drift (`expo`, `expo-linking`, `expo-router`) that
commit `aee5a10` aligned before #50; Phase 3 made no further dependency
change.

## Quality gates

Results on the commit that records this document, run locally with the CI commands:

| Gate | Result |
| --- | --- |
| Backend `pytest` | 1094 passed, 7 deselected (the 5 `celery_smoke` and 2 opt-in `local_embedding` tests) |
| Worker smoke `pytest -m celery_smoke` | 5 passed, against a separately running worker |
| Ruff lint / format, Django checks, migration drift | Pass / pass / pass / no changes |
| Mobile Jest (`test:ci`) | 29 suites, 330 tests passed (collection checked with `npx jest --listTests`) |
| Expo Doctor, ESLint, Prettier, TypeScript | 21/21 / pass / pass / pass |
| Web export route check | Pass (15 route files: the 7 Phase 3 routes plus group aliases) |
