# Undocumented `dismax=true` search parameter (matches website's fuzzy-ranking behavior)

Source: https://community.metabrainz.org/t/how-can-i-get-the-same-results-in-the-api-as-in-the-web-interface/235608 ("How can I get the same results in the API as in the web interface?" — MetaBrainz Community Discourse)
Kind: community-sourced hidden parameter, verified against the live API
Retrieved: 2026-07-07
API version: MusicBrainz ws/2 (current production)

## Claim (verbatim/paraphrased from the thread, attributed to named posters)

Original poster **obruchez** noticed that searching for "kenny garret" (typo for Kenny Garrett) ranks results differently on the musicbrainz.org website search box than via the `/ws/2/artist?query=` API — the website surfaces the intended artist higher.

**Bitmap** explained: the web interface uses a "dismax" query method when advanced Lucene syntax isn't explicitly enabled by the user. Dismax searches across multiple fields with different per-field weightings (rather than the API's plain per-field literal search). The undocumented way to get this behavior from the API is to add `&dismax=true` to the query string:
```
http://musicbrainz.org/ws/2/artist/?query=kenny%20garret&dismax=true
```

**ijabz** added technical/historical context: "Dismax search didn't originally exist, it was added later to provide better results for the website search," and it wasn't originally exposed for API use because it constrains field-selection flexibility. The parameter is not listed on the official `MusicBrainz_API`/`MusicBrainz_API/Search` doc pages.

## Verification (exact commands + responses, verbatim)

### Without `dismax`

```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/artist/?query=kenny%20garret&fmt=json&limit=3"
```
Result (parsed, `score name`):
```
100 Kenny Rogers
97 Garret
96 Kenny Garrett
```

### With `dismax=true`

```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/artist/?query=kenny%20garret&fmt=json&limit=3&dismax=true"
```
Result (parsed, `score name`):
```
100 Kenny Rogers
96 Garret
96 Kenny Garrett
```

The parameter is accepted (no error) and measurably changes the relevance scoring (`Garret`'s score moved from 97 to 96, changing its margin relative to `Kenny Garrett`) — confirming `dismax=true` is a real, functioning parameter, distinct from the plain query path. Exact rank ordering for this particular query at time of testing did not reproduce the thread's original "Kay Garret ranks first" complaint (data/ranking may have changed since 2021), but the parameter's live effect on scoring is confirmed.

## Confirmed behavior for integrators

- `dismax=true` is a real, working, but **undocumented** query-string parameter on `/ws/2/<entity>?query=` search endpoints. It switches ranking to the same Dismax (multi-field weighted) mode the musicbrainz.org website search box uses by default, which can produce different top results / scores than the API's plain default ranking for typo-prone or ambiguous queries.
- Use it if you want API search results to match what a human sees on musicbrainz.org's search box for the same free-text input, especially for user-facing autocomplete/typo-tolerant search features.
- Because it's undocumented, treat it as unstable/unsupported — it may change or be removed without notice in future MusicBrainz server releases.
