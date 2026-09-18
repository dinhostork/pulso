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

A later quality gate looks like this:

```python
loaded = load_corpus()
# ... run Story matching over loaded.article_ids.values() ...
report = evaluate(
    loaded.expected_events,
    primary_assignments(loaded.article_ids.values()),
    names=loaded.names,
)
assert report.false_merge_rate <= LIMIT, report.describe()
```

Everything is offline: no HTTP, fixture server, embedding provider or model.
The formulas and zero-denominator rules are documented in `story_metrics.py`.
