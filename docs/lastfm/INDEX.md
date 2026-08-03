# Last.fm API docs — index

Curated for: developers (and coding LLMs) integrating against the Last.fm web service API 2.0
(`ws.audioscrobbler.com/2.0/`). Two trust tiers: `official/` is verbatim-captured from
last.fm's own docs; `community-undocumented/` is community claims that were each testbed-verified
(or explicitly marked UNVERIFIED) against the live API on 2026-07-07, with the exact `curl`
command + response excerpt next to every claim (API key always shown as `REDACTED`).

**Curate round 2** (this pass): re-read all 76 files from round 1, integrated one gap-fill file
added since, re-verified organization/fidelity, rewrote this index in full. No deletions —
round 1 already trimmed the set to exactly what the brief needs; round 2 found nothing duplicate,
irrelevant, or paraphrased.

## official/ — top-level

- `official/introduction.md` — API root URL, method-call shape (`package.method`), encoding, ToS/scrobbling pointers. Read first, orientation.
- `official/overview.md` — one-paragraph marketing landing page + "getting started" checklist (get key → read docs → join forums). Skip unless onboarding a new API account.
- `official/rest-requests.md` — **read for response format**: `api_key`+`method` required params, `<lfm status>` envelope, error XML shape, `format=json` + `callback`, the 4 XML→JSON conversion rules, and a full XML/JSON pair example (tag.search). Canonical source for "XML default, format=json" from the brief — but see Coverage notes below and `community-undocumented/json-attr-envelope-rule.md` for where its stated JSON rules are incomplete/stale against the live API.
- `official/authentication.md` — auth overview: get key → configure account → pick web/desktop/mobile path → link to authspec. Read first for auth, then jump to the specific how-to.
- `official/auth-spec.md` — **the full authentication API spec** (v1.0): web/desktop/mobile flows end to end, token lifetime (60 min, single-use), session lifetime (infinite until revoked), and the signing algorithm (§8: sort params alphabetically, concat `name+value`, append secret, MD5). Canonical source for `api_sig` signing.
- `official/web-auth-howto.md` — web app auth walkthrough: redirect to `/api/auth`, optional custom `cb=` callback param, token→session exchange, signing example (secret `'mysecret'`).
- `official/desktop-auth-howto.md` — desktop app auth walkthrough: `auth.getToken` → browser authorize → `auth.getSession`, same signing scheme.
- `official/mobile-auth-howto.md` — mobile/standalone-device auth walkthrough: direct `auth.getMobileSession` with username+password over HTTPS POST only.
- `official/error-codes.md` — **the numeric error-code table** (1–29, several gaps/dupes retained verbatim e.g. 1 and 19 both "This error does not exist"). Canonical source for the brief's error-code table.
- `official/scrobbling-2.0.md` — **the Scrobbling 2.0 spec**: now-playing vs scrobble semantics, the 30s/half-duration-or-4min "when to scrobble" rule, `chosenByUser`, batch limit (50/request), retryable vs non-retryable error codes, ignored-message codes 0–5, meta-data correction (`corrected="1"`) behavior. Canonical source for the brief's Scrobbling 2.0 section.
- `official/terms-of-service.md` — **trimmed excerpt** (curation note in file header): only §2.6 (must use documented API surface), §3 (non-commercial-use restriction), §4.3.4 (100MB "Reasonable Usage Cap" + caching requirement), §4.4 (rate-limit clause, no published number). Full legal boilerplate (warranties, indemnity, trademarks, publicity, termination, governing law) was cut as out of scope for a coding-integration reference — see file header for what was removed and why. This is the "officially vague" rate-limit source that `community-undocumented/rate-limits.md` verifies against.

## official/methods/ — full method inventory (57 methods, matches the current live method index exactly)

Each file: params (required/optional), auth requirement, one XML sample response, method-specific + generic error list. Read the specific method file when implementing that call.

**album.* (6)**: `album-add-tags.md` (write) · `album-get-info.md` · `album-get-tags.md` · `album-get-top-tags.md` · `album-remove-tag.md` (write) · `album-search.md`

**artist.* (10)**: `artist-add-tags.md` (write) · `artist-get-correction.md` (autocorrect suggestion lookup) · `artist-get-info.md` · `artist-get-similar.md` · `artist-get-tags.md` · `artist-get-top-albums.md` · `artist-get-top-tags.md` · `artist-get-top-tracks.md` · `artist-remove-tag.md` (write) · `artist-search.md`

**auth.* (3)**: `auth-get-mobile-session.md` (mobile flow; documents the deprecated `authToken` param — the one still-official example of "deprecated but described as working, discouraged") · `auth-get-session.md` (web/desktop flow, step 3) · `auth-get-token.md` (desktop flow, step 2)

**chart.* (3)**: `chart-get-top-artists.md` · `chart-get-top-tags.md` (note: the official page's own description text says "Get the top artists chart" — copy-paste artifact in the source itself, captured verbatim; the sample response and params are correctly for tags) · `chart-get-top-tracks.md`

**geo.* (2)**: `geo-get-top-artists.md` (by ISO 3166-1 country name) · `geo-get-top-tracks.md` (by country, optional metro `location`)

**library.* (1)**: `library-get-artists.md` (paginated user library with play/tag counts — the only method still live in this namespace)

**tag.* (7)**: `tag-get-info.md` · `tag-get-similar.md` · `tag-get-top-albums.md` · `tag-get-top-artists.md` · `tag-get-top-tags.md` (global top tags) · `tag-get-top-tracks.md` · `tag-get-weekly-chart-list.md`

**track.* (12)**: `track-add-tags.md` (write) · `track-get-correction.md` · `track-get-info.md` · `track-get-similar.md` · `track-get-tags.md` · `track-get-top-tags.md` · `track-love.md` (write) · `track-remove-tag.md` (write) · `track-scrobble.md` (write, **the** scrobble endpoint — array notation for batches up to 50, ASCII param-name sort quirk for signing) · `track-search.md` · `track-unlove.md` (write) · `track-update-now-playing.md` (write, the now-playing endpoint)

**user.* (13)**: `user-get-friends.md` · `user-get-info.md` · `user-get-loved-tracks.md` · `user-get-personal-tags.md` · `user-get-recent-tracks.md` (scrobble history, `extended` param, `nowplaying` attr) · `user-get-top-albums.md` · `user-get-top-artists.md` · `user-get-top-tags.md` · `user-get-top-tracks.md` · `user-get-weekly-album-chart.md` · `user-get-weekly-artist-chart.md` · `user-get-weekly-chart-list.md` · `user-get-weekly-track-chart.md`

Not present (confirmed removed/blocked live, see `community-undocumented/deprecated-and-removed-methods.md`): the whole `event.*`/`venue.*`/`artist.getEvents`/`geo.getEvents` family, `artist.getShouts`, `artist.getTopFans`, `album.getBuylinks`, `album.getImages`, `track.getImages`, `user.getPlaylists`, `artist.getPodcast` (all error 3 "Invalid Method"); `artist.getImages` (still routed but error 4 "Authentication Failed" — a distinct, undocumented anomaly); `user.getArtistTracks` (error 27 "Deprecated").

## community-undocumented/ — testbed-verified quirks and gaps in the official docs

- `rate-limits.md` — **the brief's "rate limits, officially vague" deliverable**: quotes the live ToS's non-numeric position, the widely-cited-but-unverifiable "5 req/s per IP averaged over 5 min" historical figure (UNVERIFIED against current ToS text), real error-29 reports from client-library issue trackers, and the "HTTP 200 can still carry an error body" gotcha.
- `deprecated-and-removed-methods.md` — **the brief's "deprecated but still working" + removed-methods deliverable**: 13 fully-removed methods confirmed via live `curl` (error 3), the `artist.getImages` anomaly (error 4, not error 3), `user.getArtistTracks` as the live trigger for error 27, and confirmation that `geo.getTopArtists`/`geo.getTopTracks`/`tag.getSimilar` are NOT deprecated despite community confusion.
- `autocorrect-and-getcorrection.md` — `autocorrect=0` vs `=1` before/after diff, the undocumented `/+noredirect/` URL marker for unresolved names, and how `artist.getCorrection`/`track.getCorrection` relate to the `autocorrect` param.
- `mbid-quirks.md` — MusicBrainz ID field quirks: Last.fm's `mbid` may be a Track MBID where a Recording MBID is expected (UNVERIFIED here, needs MusicBrainz-side confirmation — out of this testbed's scope), and a 2015 client-library bug (artist mbid substituted for missing track mbid) that no longer reproduces today.
- `pagination-and-limit-quirks.md` — **the brief's pagination-conventions deliverable, empirical half**: real `limit` ceiling is 1000 (not the documented 200) on `user.getRecentTracks`, `page=0` is a hard error (bound 1–1,000,000) not "page 1", and `limit` out-of-range handling is inconsistent across methods (explicit error 6 vs. generic error 8 vs. silent clamp — three different methods, three different behaviors).
- `json-attr-envelope-rule.md` — **new this round; closes round 1's open gap.** The precise, testbed-derived rule for when the JSON `@attr` wrapper appears: any element with attributes *and* one or more child elements (object or array) gets those attributes moved into a nested `"@attr"` key instead of flattened — applies recursively per-element at every depth, regardless of key-name collisions or empty arrays. Also finds `official/rest-requests.md`'s own worked JSON example (`tag.search`'s `"for":"disco"`) is now stale against live behavior (the equivalent live `*.search` calls wrap `for` under `@attr`), and that `format=json` responses never carry the `<lfm status>` root wrapper at all (effectively always `raw=true`). Read this alongside `official/rest-requests.md` for the complete XML→JSON picture.
- `response-data-quirks.md` — every `artist.image` now resolves to one generic placeholder hash (artist-image service is dead, ties to `artist.getImages`→error 4 above); `album.search` omits `listeners`/`playcount` that `album.getInfo` includes.
- `scrobble-ignored-code-quirks.md` — staff-confirmed bug: scrobbles older than ~14 days are mis-reported as ignored-code 1 ("artist ignored") instead of code 3 ("timestamp too old"). Not independently testbed-verified (write-only endpoint, out of scope) — recorded as staff-confirmed-but-UNVERIFIED-here.
- `user-getrecenttracks-extended-and-nowplaying-quirks.md` — verified `extended=0` vs `=1` diff (adds `loved` flag + expands `artist` object, but artist `mbid` can go empty and artist `image` is always the placeholder); two now-playing/date-field community reports left UNVERIFIED (require a live now-playing session this testbed couldn't manufacture).

## Coverage notes

**Inconsistencies found (both sides cited):**

1. **JSON `@attr` pagination envelope was under-specified in the official docs vs. what the API actually returns — now resolved by `community-undocumented/json-attr-envelope-rule.md`.** `official/rest-requests.md`'s stated JSON conversion rule ("Attributes are expressed as string member values with the attribute name as key") implies container attributes like `page`/`perPage`/`totalPages` would appear as flat siblings — consistent with its own `"for":"disco"` example. Live behavior instead nests those same attributes under a `"@attr"` key whenever the element also has child-element content. `json-attr-envelope-rule.md` (added this round) pins down the exact rule with 5 independent testbed findings, and additionally shows the official page's own `tag.search` worked example is now stale: the equivalent live `*.search` calls (`tag.search` itself is now dead, error 3) wrap `"for"` under `@attr` too. The official page itself is unchanged (captured as-is, per the brief) — the inconsistency is between that page and live behavior, and is now fully documented rather than an open gap.

2. **`rate-limits.md`'s ToS section citation is slightly loose.** It attributes the "Reasonable Usage Cap" quote to "Section 4.3" generally; in `official/terms-of-service.md` (and the live page) that text is specifically sub-clause 4.3.4. The quoted text itself matches verbatim — only the section-number precision is off. Not corrected in place since it's descriptive prose, not the verbatim quote; noted here for awareness.

**Fidelity:** all `official/` files carry proper source headers and read as verbatim captures (including preserved source-side quirks like `chart.getTopTags`'s mismatched description text, and the "Required (unless mbid)]" bracket typo repeated across several method pages — both left as-is, faithful to the live page; re-confirmed this round on a fresh sample across album/artist/auth/chart/geo/library/tag/track/user). All `community-undocumented/` claims, including the new `json-attr-envelope-rule.md`, carry inline citations and an explicit verdict (CONFIRMED / UNVERIFIED / PARTIALLY VERIFIED-STALE) with the exact `curl` command run. No paraphrase-flagged files found; every file (57 methods + 11 official top-level + 9 community-undocumented = 77) starts with a proper source header, verified by a header-presence sweep this round.

**Organization this round:** the one gap-fill file added since round 1 landed in an auto-named directory (`testbed-capture-and-document-the-json-attr-paginat/`, truncated from its gather focus string) containing a single file. Relocated it to `community-undocumented/json-attr-envelope-rule.md` (matching the trust-tier and naming convention of its siblings) and fixed its internal relative links accordingly; the odd directory was removed. No other reorganization, splitting, or deletion was needed — every method file is well under one-focused-read size (max 102 lines, `track.scrobble`), and round 1's trim of `official/terms-of-service.md` down to the four API-relevant clauses remains the set's only content deletion to date.

**Gaps:** none open. The one gap left by round 1 (the `@attr` envelope rule) is closed by this round's integrated file. No new gaps surfaced during the re-read.
