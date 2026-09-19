# Mobile Feed contract fixtures

These repository-owned JSON examples are normative wire-shape examples for
Phase 3. They contain no runtime fallback data. Backend serializers and mobile
decoders must validate against the same files.

| Fixture                            | Coverage                                                            |
| ---------------------------------- | ------------------------------------------------------------------- |
| `feed-current.json`                | CURRENT, multi-source page and ordering                             |
| `story-current-single-source.json` | CURRENT, one source, missing optional values                        |
| `story-current-viewers.json`       | Equal factual payload for two users; only bookmark metadata differs |
| `story-updating.json`              | stale/failed fallback as UPDATING                                   |
| `story-preparing.json`             | ACTIVE with no usable generation                                    |
| `sources-page.json`                | Deterministic current-member source page                            |
| `story-unavailable.json`           | 410 unavailable error                                               |
| `errors.json`                      | Product error/status shapes                                         |
| `bookmark.json`                    | Idempotent bookmark result and unavailable saved tombstone          |
| `feed-impressions.json`            | Proposed batch shape and per-event results                          |

IDs are decimal strings deliberately, including values beyond JavaScript's
safe integer range. Dates are UTC ISO-8601 strings. Fixtures may be copied into
consumer tests, but production mobile code must never display them after an API
failure.
