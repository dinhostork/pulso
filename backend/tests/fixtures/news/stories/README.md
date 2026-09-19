# Story matching evaluation corpus

`corpus.json` in this directory is a labeled set of short, repository-authored
Articles with the event each one describes. It is the ground truth that Story
matching (#28) is scored against before any threshold is chosen, so false
merges (two events collapsed) and false splits (one event fragmented) are
measured rather than guessed.

All text is synthetic and written for this repository. People, places,
publishers and organizations are fictional. Nothing is copied from a news
publication.

## Format

```json
{
  "schema_version": 1,
  "anchor": "2026-03-02T12:00:00+00:00",
  "scenarios": {"<name>": {"description": "...", "article_ids": ["<id>", "..."]}},
  "articles": [
    {
      "id": "harbor-storm-01",
      "source_slug": "elsby-courier",
      "title": "...",
      "body": "...",
      "language": "en",
      "published_offset_minutes": 0,
      "expected_event": "elsby-harbor-storm-closure",
      "note": "Why this record holds its label.",
      "rationale": "Only on ambiguous or adversarial records: why it joins or stays apart.",
      "syndicated_from": "Only on syndicated copies: the id of the record it copies."
    }
  ]
}
```

- `id` is stable and never reused. Tests and diagnostics refer to Articles by
  it, because database primary keys differ between runs.
- `expected_event` is the only ground truth: Articles with equal labels
  describe the same event, and every record has exactly one label. Labels say
  nothing about wording, fingerprints or `duplicate_of`.
- **Anchor.** Publication time is always `anchor + published_offset_minutes`.
  The corpus never uses the current date, so its time windows cannot drift.
- `syndicated_from` records publication provenance, not event truth. The
  loader turns it into News Core's `duplicate_of`.
- Anything describing a matcher is refused by the schema, so the corpus cannot
  encode one: similarity scores, embeddings, thresholds or Story ids all fail
  to load.

## Events

| `expected_event` | Articles (Source, offset in minutes) |
| --- | --- |
| `elsby-harbor-storm-closure` | `harbor-storm-01` (elsby-courier, 0), `harbor-storm-02` (kestrel-post, 40), `harbor-storm-03` (meridian-daily, 95), `harbor-storm-04` (elsby-courier, 2880) |
| `elsby-harbor-second-storm` | `harbor-second-storm-01` (elsby-courier, 216000), `harbor-second-storm-02` (kestrel-post, 216060) |
| `brightline-rail-strike` | `rail-strike-01` (northwind-wire, 300), `rail-strike-02` (varrow-herald, 330, syndicated copy of `rail-strike-01`), `rail-strike-03` (kestrel-post, 420) |
| `varrow-council-budget-vote` | `varrow-budget-01` (varrow-herald, 600), `varrow-budget-02` (meridian-daily, 660) |
| `lowmere-council-budget-vote` | `lowmere-budget-01` (lowland-review, 720), `lowmere-budget-02` (lowland-review, 800) |
| `calloway-reelection-bid` | `calloway-reelection-01` (varrow-herald, 4320), `calloway-reelection-02` (meridian-daily, 4400) |
| `kestrel-bay-earthquake` | `kestrel-quake-01` (northwind-wire, 1000), `kestrel-quake-02` (kestrel-post, 1030) |
| `almen-valley-earthquake` | `almen-quake-01` (northwind-wire, 1015) |
| `meridian-museum-map-exhibition` | `museum-maps-01` (meridian-daily, 1500), `museum-maps-02` (lowland-review, 1600) |
| `lowmere-chess-final` | `chess-final-01` (lowland-review, 1700) |
| `almen-river-flood` | `almen-flood-01` (lowland-review, 5000), `almen-flood-02` (meridian-daily, 6440), `almen-flood-03` (lowland-review, 9320) |
| `almen-dam-inquiry` | `almen-inquiry-01` (meridian-daily, 10080), `almen-inquiry-02` (lowland-review, 10200) |
| `veldora-dunmar-collapse-inquiry` | `dunmar-collapse-01` (northwind-wire, 11520) |
| `sarran-orlanth-strike-accusation` | `sarran-strikes-01` (kestrel-post, 11580), `sarran-strikes-02` (meridian-daily, 11700) |
| `estmark-drone-readiness-warning` | `tarvia-drone-warning-01` (lowland-review, 12960) |
| `korvel-delegation-drone-threat` | `tarvia-delegation-drones-01` (varrow-herald, 13020), `tarvia-delegation-drones-02` (northwind-wire, 13080) |

## Scenario catalog

All scenarios live in `corpus.json` under `scenarios`. A record may belong to
more than one scenario.

| Scenario | Articles | Expected grouping and why |
| --- | --- | --- |
| `same_event_different_source` | `harbor-storm-01`, `harbor-storm-02`, `varrow-budget-01`, `varrow-budget-02` | Two groups of one event each, every Article from a different Source. Joining them across Sources is the basic job of a Story. |
| `same_event_different_wording` | `harbor-storm-01`, `harbor-storm-03`, `kestrel-quake-01`, `kestrel-quake-02` | Same event, little shared vocabulary: the fleet-and-waterfront account of the harbor closure and the "windows rattle" account of the quake. Splitting them is a false split. |
| `same_topic_different_event` | `varrow-budget-01`, `varrow-budget-02`, `lowmere-budget-01`, `lowmere-budget-02` | Two separate councils approving budgets on the same day with near-identical headlines. A shared topic is not a shared event. |
| `same_entities_different_event` | `varrow-budget-01`, `calloway-reelection-01`, `calloway-reelection-02` | Mayor Calloway and the Varrow council appear in both the budget vote and the later reelection announcement, which even cites the budget. Same entities, two events. |
| `same_event_later_reporting` | `harbor-storm-01`, `harbor-storm-04`, `almen-flood-01`, `almen-flood-03` | Follow-ups two or three days later about the same closure or flood. Later reporting joins the existing Story. |
| `temporally_distant_similar_event` | `harbor-storm-01`, `harbor-storm-02`, `harbor-second-storm-01`, `harbor-second-storm-02` | A second storm closes the same harbor, with the same authority and harbor master, five months later. Only time separates the events; merging them is a false merge. |
| `syndicated_duplicate_publication` | `rail-strike-01`, `rail-strike-02`, `rail-strike-03` | `rail-strike-02` is a word-for-word copy, so News Core links it with `duplicate_of`. `rail-strike-03` is independent coverage of the same strike. All three form one event: deduplication is not Story clustering. |
| `high_lexical_overlap_different_event` | `kestrel-quake-01`, `almen-quake-01`, `kestrel-quake-02` | Two templated wire reports share almost every word but describe separate earthquakes 15 minutes apart. The differently worded local report, not the lookalike, belongs with `kestrel-quake-01`. |
| `clearly_unrelated_events` | `museum-maps-01`, `museum-maps-02`, `chess-final-01`, `rail-strike-01` | Events with nothing in common, including one single-Article event. |
| `story_drift_boundary` | `almen-flood-01`, `almen-flood-02`, `almen-flood-03`, `almen-inquiry-01`, `almen-inquiry-02` | A flood covered over several days, then a government inquiry into the dam operator. The inquiry mentions the flood and its places but is a new decision with its own actors. Letting the flood Story absorb it is a false merge. |
| `same_conflict_different_event` | `dunmar-collapse-01`, `sarran-strikes-01`, `sarran-strikes-02` | One ongoing war (#38). An army opens an inquiry into its own collapse at Dunmar; an hour later the Sarran movement accuses neighbouring Orlanth of 26 air strikes, then a second Source rewords that accusation. The two events share the war, Veldora and the movement's name and sit close in meaning, but merging them is a false merge. |
| `same_war_technology_different_event` | `tarvia-drone-warning-01`, `tarvia-delegation-drones-01`, `tarvia-delegation-drones-02` | One war and one weapon (#38). A president warns that allies are not ready for drone warfare in Tarvia; an hour later Harvanian drones hold up a ministers' train bound for Korvel, then a second Source rewords that incident. Same war, same technology, overlapping names, different events. |

## Consuming the corpus

The loader and metrics are importable modules, not test-local helpers:

- `tests/news/story_corpus.py`
  - `read_corpus()` parses and validates the file without a database.
  - `load_corpus()` creates ordinary `Source`, `SourceEndpoint`, `RawArticle`
    and `Article` rows. Canonical URLs are `https://<source_slug>.example/stories/<id>`.
    Each Source gets one inactive loopback endpoint that is never fetched,
    and the loader makes no DNS lookups.
  - Loading twice returns the same rows. The returned `LoadedCorpus` maps
    fixture ids to database ids (`article_ids`) and provides `expected_events`
    and `names` for scoring.
- `tests/news/story_metrics.py`
  - `evaluate(expected, assigned, names=...)` scores any
    `{article_id: story_id or None}` assignment over the pairwise same-event
    relation. It reports precision, recall, false merge count and rate, false
    split count and rate, and unassigned Articles.
  - Every false merge is reported with its Story id, the two event labels and
    the Article ids; every false split with its event label and the Story ids
    it was spread across. `EvaluationReport.describe()` renders all of it for
    an assertion message.
  - `primary_assignments(article_ids)` reads an assignment from `StoryArticle`
    primary rows.

The #34 full-pipeline quality gate uses this evaluator:

```python
loaded = load_corpus()
run_pipeline(publication_order(loaded))  # tests.news.story_pipeline
report = evaluate(
    loaded.expected_events,
    primary_assignments(loaded.article_ids.values()),
    names=loaded.names,
)
assert report.precision == report.recall == 1.0, report.describe()
assert report.false_merge_count == report.false_split_count == 0, report.describe()
assert report.unassigned_count == 0, report.describe()
```

The loader and evaluator need no HTTP, fixture server, embedding provider or model.
The pipeline gate replays recorded embeddings and uses local deterministic
extraction/synthesis under the existing non-loopback DNS guard.

`recorded_local_embeddings.json` is not part of the corpus and holds no ground
truth. It stores the local embedding model's vectors for the corpus inputs, so
Story matching tests can replay real embeddings offline
(`tests/news/recorded_embeddings.py`). Re-record it whenever corpus text
changes.
The formulas and zero-denominator rules are documented in `story_metrics.py`.

## Quality regression gate

These measurements describe the **repository-owned synthetic regression
corpus**, not production accuracy: 32 Articles, 16 expected events and 21
same-event pairs. The recorded embedding model is
`fastembed:BAAI/bge-small-en-v1.5@52398278842e`.

| Measurement | Corpus | Precision | Recall | False-merge pairs | False-split pairs | Unassigned |
| --- | --- | --- | --- | --- | --- | --- |
| Historical matcher v1 (#28), direct | 26 Articles | 1.000 | 0.632 (12/19) | 0 | 7 | 0 |
| Historical pre-#36 full match + refresh | 26 Articles | 1.000 | 0.789 (15/19) | 0 | 4 | 0 |
| Historical matcher v2 (#36), direct | 26 Articles | 1.000 | 1.000 (19/19) | 0 | 0 | 0 |
| Historical matcher v2 (#36), full match + refresh | 26 Articles | 1.000 | 1.000 (19/19) | 0 | 0 | 0 |
| Historical matcher v2 (#36), direct | 32 Articles | 0.870 | 0.952 (20/21) | 3 | 1 | 0 |
| Historical matcher v2 (#36), full match + refresh | 32 Articles | 0.840 | 1.000 (21/21) | 4 | 0 | 0 |
| Current matcher v3 (#38), direct | 32 Articles | 1.000 | 1.000 (21/21) | 0 | 0 | 0 |
| Current matcher v3 (#38), full match + refresh | 32 Articles | 1.000 | 1.000 (21/21) | 0 | 0 | 0 |

The #38 hard negatives were added and measured under matcher v2 before the
matcher changed: v2 merged both, which is the failure the real-world smoke test
showed. The historical figures were measured before #36 and retained in its results;
the direct matcher gate is `test_story_matching_corpus.py`, while #34 owns
`test_story_engine_end_to_end.py` and `test_story_quality.py`. Current
false-merge and false-split rates are both 0.000. There is no quality margin:
every deterministic expected event currently groups correctly, so any new
merge or split must fail with its fixture ids, labels and Story ids visible.
Never alter labels or recorded vectors to satisfy a failing quality gate.

Matcher v2 joined the formerly split Varrow budget pair and all three Almen
flood reports, and v3 keeps them. The inquiry stays separate; the Kestrel/Almen
earthquake hard negative records `VERIFICATION_REJECTED` / `NO_SHARED_ANCHOR`.
The two #38 hard negatives (`sarran-strikes-01` against the Dunmar inquiry,
`tarvia-delegation-drones-01` against the readiness warning) record
`VERIFICATION_REJECTED` / `MEMBER_TOO_FAR` with one shared name. Exact
membership checks require 16 active Stories and 32 associations. Reprocessing
every Article preserves this partition while retaining 4 archived empty
historical Stories, one per single-Article event; those are excluded from
candidate retrieval and active counts.

The suite also checks reconstruction with News Core provenance unchanged,
duplicate delivery, current-generation coherence, and the exact controlled
concurrency outcomes: two simultaneous first reports initially create two
Stories and converge to one active Story through reprocessing; a refresh of
an obsolete membership snapshot is discarded before the final generation is
promoted. Historical non-current synthesis rows are legitimate.
