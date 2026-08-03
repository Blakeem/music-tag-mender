# MusicBrainz ws/2 API docs — index

Curated for: documenting the MusicBrainz public web-service API (ws/2) for a coding LLM building
integrations — every endpoint, params, `inc=` includes, response formats, search/Lucene syntax,
pagination, rate limiting, auth, error semantics, plus verified undocumented quirks.

All official pages are `Retrieved: 2026-07-07`, pulled from the live wiki (revisions noted per file).
All community/testbed files use User-Agent `aipg-docs-test/1.0 (blakeem@gmail.com)` for verification.

This is round 2 of curation: round 1's set is intact below; this pass folded in a gap-fill gather
(rating scale) and re-verified a sample of testbed claims live (all reproduced — see Coverage notes).

## official/ — core API mechanics (verbatim from musicbrainz.org/doc/*)

- `official/musicbrainz-api.md` — the main API reference: resource list, lookup/browse/search URL
  shapes, `inc=` subqueries (per-entity + misc + relationship includes), non-MBID lookups
  (discid/isrc/iswc), `url` by-text lookup, browse paging & linked-entity tables, release(-group)
  type/status filter values, and the full "Submitting data" spec (tags/genres, ratings, collections,
  barcode, ISRC — POST/PUT/DELETE shapes, auth requirement, `client=` param). Read first — it's the
  spine everything else hangs off.
- `official/rate-limiting.md` — the three-stage throttle (User-Agent → source IP → global), the
  "anonymous User-Agent" blocklist, required User-Agent string format, and the "don't schedule/poll"
  etiquette rules. Read before writing any client that fires more than a handful of requests.
- `official/oauth2-authentication.md` — OAuth2 as the alternative to HTTP digest: registering an app,
  authorization-code flow, refresh/revoke, the full scope list (`profile`, `email`, `tag`, `rating`,
  `collection`, `submit_isrc`, `submit_barcode`), bearer-token usage. Read when building a "sign in
  with MusicBrainz" flow rather than plain digest auth.
- `official/entity-artist.md` — Artist entity data model (name, sort-name, type enum, gender, area,
  begin/end dates, IPI, ISNI, alias). Background reference for what artist fields mean, not API
  mechanics.
- `official/entity-recording.md` — Recording entity data model (title, artist, length, ISRC). Same
  role as above, for recordings.
- `official/entity-release.md` — Release entity data model: title/date/country/label/catalog#/barcode,
  full **status enum** (7 values incl. `expunged` — see Coverage notes), packaging, language/script,
  data-quality levels, medium title/format. Read when you need the authoritative status-value list or
  release-level field semantics.
- `official/rating-system.md` — the Rating_System page: ratings are a 1–5 star, user-assigned value
  aggregated into a community average; this is the **display/UI** scale only. Read alongside
  `community-undocumented/rating-value-scale-and-api-mapping.md` for how this maps to the API's
  0–100 `<user-rating>` write value.

### official/search/ — split from the official Search page, one file per searchable index

`official/search/syntax.md` — read this one first: the query params common to every search endpoint
(`type`, `fmt`, `query`, `limit`, `offset`, `dismax`, `version`), Lucene syntax pointer, null-field
search (`-field:*`), zero-count search, and double-escaping for literal searches. Then jump to the
entity you need — each has an `Example` URL, a full `Search Fields` table (the field names usable in
`query=`, e.g. `arid`, `recordingaccent`, `primarytype`), and an XML + JSON response sample:

- `official/search/annotation.md` — search entity annotations (free-text notes) directly.
- `official/search/area.md`, `official/search/artist.md` (largest — most search fields of any entity),
  `official/search/cdstub.md`, `official/search/event.md`, `official/search/instrument.md`,
  `official/search/label.md`, `official/search/place.md`, `official/search/recording.md` (largest
  field table), `official/search/release-group.md`, `official/search/release.md`,
  `official/search/series.md`, `official/search/tag.md`, `official/search/url.md`,
  `official/search/work.md`.

### official/examples/ — split from the official Examples page, request+response pairs

Lookup examples (one file per entity type; each is a real request URL plus full XML and JSON response
bodies, some fields elided by the source for brevity):
`official/examples/lookup-area.md`, `lookup-artist.md`, `lookup-event.md`, `lookup-genre.md`,
`lookup-instrument.md`, `lookup-label.md`, `lookup-place.md`, `lookup-recording.md`, `lookup-release.md`
(largest — full release+media+track shape), `lookup-release-group.md`, `lookup-url.md`, `lookup-work.md`.
No `lookup-series.md` exists in this set — see Gaps.

- `official/examples/browse.md` — one browse-request example (paged release list) showing the
  `-count`/`-offset` response envelope.
- `official/examples/relationships.md` — worked examples of `inc=`-loaded relationships: a classical
  release with recording-level + work-level rels, relationship attributes (incl. `credited-as`),
  series-membership attributes. Read when wiring up relationship parsing.
- `official/examples/genres-tags-ratings.md` — one request showing `genres`+`tags`+`ratings` all
  included together, full response.

## community-undocumented/ — under-documented features & quirks, each verified live

Every claim here was checked against the production API with a read-only GET; the exact `curl` command
and a verbatim response excerpt sit next to each claim. Community-sourced claims are labeled with their
forum/blog origin. This pass re-ran a sample of these commands live (rating value, release status
`expunged`) and reproduced identical results — see Coverage notes.

- `error-handling-and-quirks.md` — **not from an official doc page**: the full HTTP status-code
  inventory the official pages don't spell out — 400 (invalid/nil MBID, invalid `inc=`, malformed
  request shape), 401 (auth-gated `inc=`, shows both `WWW-Authenticate: Digest` and `Bearer` schemes),
  404 vs 400 distinction, 406 (bad `fmt=`), 501 (genre search), 503 pointer; plus search
  `limit=`/`offset=` out-of-range behavior (silently falls back to default/empty rather than erroring)
  and format-negotiation precedence. Read this whenever you need to know what status code + body shape
  to expect from a failure.
- `bulk-url-lookup-resource-parameter.md` — the only built-in batch-lookup mechanism in the API:
  repeated `resource=` (up to 100) on `/ws/2/url`; response-shape difference between one vs. multiple
  `resource=` params; misses are silently skipped in the batch form.
- `rate-limiting-community-notes.md` — supplements `official/rate-limiting.md`: auth does **not** raise
  your read rate limit (it only unlocks private-data access); no batch endpoint outside `/ws/2/url`;
  token-bucket client pattern as the community workaround for large jobs. Flags its own unverifiable
  bits explicitly as UNVERIFIED.
- `browse-release-pagination-track-count-cap.md` — `limit=` on release browse is not honored once
  `inc=recordings` (or other track/media-level includes) is added; the server caps total *tracks*
  (~500) per page instead, so `releases.length` can be well under `limit`. Read before writing any
  pagination loop that adds `inc=recordings` to a release browse.
- `collection-public-browse-by-editor.md` — `GET /ws/2/collection?editor=<name>` and
  `/ws/2/collection/<mbid>/<entities>` both work fully unauthenticated for public collections; private
  collections require digest auth as that editor.
- `cover-art-archive-inline-on-release-lookup.md` — single release lookups always carry a
  `cover-art-archive` object (`artwork`/`count`/`front`/`back`/`darkened`) with **no `inc=` needed**;
  absent from search/browse results; actual image bytes are a separate `coverartarchive.org` call.
- `merged-entity-mbid-redirect-behavior.md` — looking up a merged-away MBID returns **HTTP 301** with
  a `Location` header **and** a full body already describing the *new* entity (whose `id` differs from
  the requested MBID) — a redirect-following client will silently get the wrong-keyed data unless it
  checks `id` against what it requested. One of its two cited sources was a mismatched forum-thread
  citation, corrected this pass — see Coverage notes; the live-verified 301 behavior itself is unaffected.
- `genre-list-endpoint-ws2-genre-all.md` — `/ws/2/genre/all` verified live (2,165 genres at capture
  time), field shape (`id`/`name`/`disambiguation`), and its history (ticket MBS-9880, prior
  `entities.json` GitHub workaround). Its own "undocumented" framing is stale — see Coverage notes.
- `annotation-search-endpoint.md` — practical note that `/ws/2/annotation?query=` is easy to miss in
  third-party client libraries. The endpoint's mechanics are now fully covered in
  `official/search/annotation.md` — see Coverage notes.
- `search-dismax-parameter.md` — empirical proof `dismax=true` measurably changes search ranking
  (matches the musicbrainz.org website's default ranking mode). Its "undocumented" framing is stale —
  `dismax` is now listed in `official/search/syntax.md` — see Coverage notes; the scoring-effect
  measurement is still the unique value here.
- `search-index-freshness-lag.md` — architectural fact (still true): `query=` search runs against a
  separately-refreshed Solr/Lucene index, while lookup/browse hit the live DB directly. Historical
  outage reports are old/resolved and marked as such; not independently re-timed in this pass.
- `lucene-boolean-operator-case-sensitivity.md` — `AND`/`OR`/`NOT` must be **uppercase**; lowercase is
  indexed as a literal term, silently changing query meaning (verified: count 249,350 vs. count 1 for
  the same query differing only in case).
- `lucene-regex-field-search-syntax.md` — `field:/regex/` is supported but matches **per indexed word
  token**, not across the whole field string; documents the multi-clause `AND` workaround for
  multi-word patterns.
- `lucene-wildcard-search-behavior.md` — leading wildcards (`*term`) work on MusicBrainz's search
  server (unlike vanilla Lucene `QueryParser` defaults); trailing wildcards work as expected; wildcards
  inside quoted phrases are unreliable.
- `rating-value-scale-and-api-mapping.md` — **new this pass**, closes round 1's rating-scale gap:
  reconciles the 1–5 star display scale (`official/rating-system.md`) with the 0–100
  `<user-rating>` write value (`official/musicbrainz-api.md`'s submission example); verified live that
  GET responses report ratings on the 1–5 scale (`"rating":{"value":4.6,"votes-count":46}`, reproduced
  this pass); the 0–100/step-20/"0 removes rating" mapping itself is corroborated by two independent
  third-party client libraries (not independently write-verified, since this is a read-only testbed) —
  its one unconfirmed sub-claim (strict step-of-20 enforcement) is flagged UNVERIFIED in the file.

## Coverage notes

### Inconsistencies (cross-source)

1. **Release status enum**: `official/musicbrainz-api.md` ("Release (Group) Type and Status" section)
   lists 6 status filter values — `official, promotion, bootleg, pseudo-release, withdrawn, cancelled`
   — omitting `expunged`. `official/entity-release.md` ("Status" section) lists 7, including
   `expunged`. Verified empirically **again this pass**: `status=bogus-status` on a release browse →
   HTTP 400 `"bogus-status is not a recognized release status."`; `status=expunged` → HTTP 200 with a
   (empty-for-this-artist) result set, i.e. it's a *recognized* value — same result as round 1.
   `entity-release.md` is complete; `musicbrainz-api.md`'s filter-value list is stale on the live wiki
   itself (not a bad capture on our part — re-fetching would reproduce the same gap, so this is not
   listed under Gaps below; a coding LLM should treat `entity-release.md`'s 7-value list, not
   `musicbrainz-api.md`'s 6-value list, as authoritative for valid `status=` values).
2. **`dismax` "undocumented" framing is stale**: `community-undocumented/search-dismax-parameter.md`
   states dismax "is not listed on the official MusicBrainz_API/MusicBrainz_API/Search doc pages," but
   `official/search/syntax.md` (split from the official Search page, captured the same day) documents
   `dismax` directly in its common-parameters table. Likely added to the wiki after the cited forum
   thread. The file's live-verified ranking-change measurement is still valid and kept; treat
   `official/search/syntax.md` as the authoritative parameter reference, and this file as a supplementary
   before/after scoring example only.
3. **`/ws/2/genre/all` "undocumented" framing is stale**: `community-undocumented/genre-list-endpoint-ws2-genre-all.md`
   frames the endpoint as "easy to miss... undocumented/unannounced," but `official/musicbrainz-api.md`
   (the genre "all" sub-resource paragraph) fully documents it, including the `fmt=txt` behavior. Keep
   the community file for its unique data (verified genre count/fields, MBS-9880 history, prior
   workaround); don't take its "undocumented" claim at face value.
4. **Annotation search endpoint** — `community-undocumented/annotation-search-endpoint.md` calls this
   endpoint "under-documented/easily-missed," but `official/search/annotation.md` fully documents it
   (search-fields table + XML/JSON examples). The community file's remaining value is purely the
   "overlooked in third-party libraries" practical framing, not new technical facts.
5. **Rating scale — two different numeric ranges for the same value, neither official page cross-links
   the other**: `official/rating-system.md` (Rating_System page) documents only a 1–5 star UI scale.
   `official/musicbrainz-api.md`'s ratings-submission example uses `<user-rating>100</user-rating>` /
   `<user-rating>20</user-rating>` — a 0–100 scale — with no explanation. Verified live this pass that
   GET responses report the 1–5 scale (`rating.value: 4.6`), confirming the two scales coexist for the
   same underlying data depending on read vs. write. Resolved by the new
   `community-undocumented/rating-value-scale-and-api-mapping.md` (closes round 1's open Gap).

### Fidelity notes

1. `error-handling-and-quirks.md` is intentionally filed under `community-undocumented/` (not
   `official/`) — its own header states "NOT an official doc page... testbed-derived," which matches its
   actual provenance; its content (verified curl commands + responses) is sound.
2. `community-undocumented/merged-entity-mbid-redirect-behavior.md` cited
   `community.metabrainz.org/t/how-to-use-regex-in-search/410531` as "community context" for the
   `gid_redirect` mechanism. Checked this pass by fetching the thread: it is exclusively about Lucene
   regex search syntax (the same thread already correctly cited in `lucene-regex-field-search-syntax.md`)
   and does not mention redirects, merges, or `gid_redirect` at all — a mismatched citation, corrected
   in the file this pass (struck the false attribution, left a dated curator note). The file's substantive
   claims (HTTP 301 + body-reflects-new-entity behavior) are independently supported by the cited blog
   post and the live curl test in the same file, which reproduces cleanly, so no content was lost.
3. `rating-value-scale-and-api-mapping.md` is explicitly a **synthesis** file (its own header says
   "synthesized from official MusicBrainz sources + live ws/2 testbed verification"), not a verbatim
   single-source capture — it quotes its sources verbatim inline and clearly separates "officially
   confirmed," "community-sourced," and its one flagged UNVERIFIED sub-claim. This matches the
   established pattern of the other `community-undocumented/` analysis files (e.g.
   `error-handling-and-quirks.md`) and is the right shape for a cross-source gap-fill; not flagged as a
   verbatim-rule violation.

### Gaps

- `official/examples/` has a `lookup-*.md` file for every core lookup entity except **series**
  (area/artist/event/genre/instrument/label/place/recording/release/release-group/url/work are all
  present; series is not, though `official/search/series.md` and the series-membership-attributes
  section of `official/examples/relationships.md` do cover series partially). Not independently
  confirmed whether the official Examples wiki page even has a series lookup example — worth a
  targeted re-check.
