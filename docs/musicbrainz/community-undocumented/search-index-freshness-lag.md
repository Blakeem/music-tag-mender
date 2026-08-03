# Search (`query=`) results can lag behind the live database — lookup/browse do not

Source: https://community.metabrainz.org/t/search-index-not-updated/216999 ("Search index not updated" — MetaBrainz Community Discourse, spanning reports from 2017 and 2022-2023)
Kind: community bug reports — historical, NOT independently re-verified live in this pass (see caveat below); recorded because the underlying architectural fact (search is a separately-updated index, not a live DB query) is a real, currently-true structural property relevant to integrators, even though the specific historical outage reports are old and resolved.
Retrieved: 2026-07-07
API version: MusicBrainz ws/2 (current production)

## Claim (verbatim/paraphrased from the thread)

**2phones** (2017-02-23): documentation stated search indexes update every 3 hours, but the recording index's "Last updated" timestamp was 3 months stale.

**reosarevok** (MetaBrainz staff, same day): confirmed the bug, said the recording index was "being updated right now."

**2phones** (2017-04-02): problem recurred (index timestamp again badly stale).

**reosarevok** (2017-04-02): filed ticket **SEARCH-441** to track it.

Recurrence in 2022-2023: **InvisibleMan78** repeatedly reported search-index data dumps lagging behind full database export dumps. **Bitmap** (MetaBrainz staff, 2023-11-16) confirmed the cause was disk space exhaustion blocking successful exports, with plans to migrate the export process to different infrastructure. Resolved 2023-11-18 (both dump types produced with matching dates).

## Why this could not be verified live in this pass

Confirming a *current* indexing lag would require creating new data (e.g. adding a new artist/recording) and then polling both a direct lookup and a `query=` search for it over time to measure the delay — this is a write operation and out of scope for a read-only testbed. This file therefore does **not** claim a current specific lag duration; the historical incidents above are recorded as-is (with their own resolution dates), and only the structural fact below was checked.

## What WAS verified: browse/lookup are unconditionally live; only `query=` search goes through a separate index

This session's own testing (see other files in this directory) repeatedly round-tripped entities immediately via **lookup** (`GET /ws/2/<entity>/<mbid>`) and **browse** (`GET /ws/2/<entity>?<relation>=<mbid>`) with no indication of staleness — these read directly from the production database. The `query=` **search** endpoints, by contrast, are documented (https://musicbrainz.org/doc/Development/Search_Architecture, https://musicbrainz.org/doc/Search_Server) to run against a separate Solr/Lucene index that is built/refreshed on its own schedule rather than being queried live — which is the architectural reason the lag reports above were possible in the first place, and remains true regardless of whether the specific historical outages have since been fixed.

## Practical guidance for integrators

- Do not assume `query=` search results reflect the database in real time. If your integration needs to find an entity **immediately** after it (or a relationship on it) is edited, prefer a direct **lookup by MBID** or a **browse** call over a `query=` search — those are confirmed to hit the live database directly, while search goes through an index that has, historically, sometimes fallen significantly behind (hours to months, per the incidents above, though currently apparently healthy — not independently re-verified here).
- If a `query=` search unexpectedly fails to find something you know exists, don't treat that as proof the data is wrong — cross-check with a direct lookup/browse before concluding the entity is missing or misnamed.
