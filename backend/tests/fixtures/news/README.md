# News fixture corpus

These repository-owned bytes make News tests deterministic: tests never fetch
live sites. Individual files isolate parser, identity, normalization or
deduplication scenarios; `test_news_core_end_to_end.py` verifies composition
across the application pipeline using a loopback HTTP server.

Story matching has its own labeled corpus of expected event groupings in
[`stories/`](stories/README.md). It is a JSON corpus loaded straight into
persistence, not feed bytes.

| File | Format | Scenario and expected layer/result |
| --- | --- | --- |
| `atom_valid.xml` | Atom | Two accepted Atom publications; adapter mapping. |
| `jsonfeed_malformed.json` | JSON Feed | Malformed JSON; safe adapter fetch error. |
| `jsonfeed_missing_items.json` | JSON Feed | Missing required items array; adapter fetch error. |
| `jsonfeed_valid.json` | JSON Feed | Six accepted items, including repeated ID, ID-only and non-HTTP URL; intake and normalization. |
| `not_a_feed.html` | HTML | Not a syndication document; fetcher media-type rejection. |
| `rss_changed_item_v1.xml` | RSS | First version of one publication; Article creation. |
| `rss_changed_item_v2.xml` | RSS | Revised title/body under the same identity; Article update. |
| `rss_duplicate_entry.xml` | RSS | Two identical provider entries; one RawArticle revision. |
| `rss_encoding_iso88591.xml` | RSS | ISO-8859-1 encoded title; charset decoding. |
| `rss_html_content.xml` | RSS | Active/hidden HTML and paragraphs; safe plain-text normalization. |
| `rss_id_only_entry.xml` | RSS | GUID without URL retained then rejected; mailto-only entry rejected at intake. |
| `rss_malformed_item.xml` | RSS | Valid items plus one missing identity; per-entry adapter rejection. |
| `rss_missing_optional.xml` | RSS | Minimal entry without optional metadata; adapter defaults. |
| `rss_rdf_valid.xml` | RDF/RSS | One accepted RDF entry; format mapping. |
| `rss_same_canonical_other_endpoint.xml` | RSS | Same Source, second endpoint, two URL identity duplicates. |
| `rss_same_canonical_other_source.xml` | RSS | Second Source claims two owned URLs; source identity conflicts. |
| `rss_same_external_id_new_url.xml` | RSS | Provider ID unchanged with new URL; Article update. |
| `rss_same_story_different_articles.xml` | RSS | Three different publications about one event; three distinct Articles. |
| `rss_syndicated_copy.xml` | RSS | Syndicated copy at another Source/URL; linked content duplicate. |
| `rss_syndication_origin.xml` | RSS | Original long-form publication for syndicated-copy comparison. |
| `rss_tracking_urls.xml` | RSS | Two delivered links with tracking/query differences; one canonical URL. |
| `rss_valid.xml` | RSS | Ten ordinary publications; primary ingestion and worker smoke. |
