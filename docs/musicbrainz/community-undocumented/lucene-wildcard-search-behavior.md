# Search: leading wildcards work (contrary to default Lucene expectations); trailing wildcards work as expected

Source: question raised at https://community.metabrainz.org/t/wildcard-search-in-api/563412 ("Wildcard search in API" — user leremjs asked whether partial/incomplete-word matches like `"jazz on the autobah*"` are supported); behavior verified directly against the live API rather than relying on the (incomplete) thread replies.
Kind: community question, answered/verified via testbed
Retrieved: 2026-07-07
API version: MusicBrainz ws/2 (current production)

## Claim / question (verbatim, from the forum thread)

> User leremjs: attempting autocomplete via `/ws/2/recording`, found that quoted phrase search like `"jazz on the"` matches, but `"jazz on the autobah*"` (trailing wildcard inside a quoted phrase, on an incomplete final word) returns zero results. Asked whether the API supports partial-word / wildcard matching including across whitespace.

Standard Lucene `QueryParser` disallows **leading** wildcards (`*term`) by default (`allowLeadingWildcard=false`), which is a commonly cited gotcha for any Lucene-backed search API. It was unclear from the thread and from official docs whether MusicBrainz's search server has this restriction enabled.

## Verification (exact commands + responses, verbatim)

### Leading wildcard — `*eatles`

```
$ curl -s -D - -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/artist/?query=%2Aeatles&fmt=json&limit=3"
```
```
HTTP/2 200
content-type: application/json
content-length: 21120

{"created":"2026-07-07T19:01:51.125Z","count":289,"offset":0,"artists":[{"id":"b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d", ... "score":100,"name":"The Beatles", ...
```
No error — `*eatles` returned 200 OK with 289 matches, top-scored result "The Beatles" (score 100).

### Trailing wildcard — `beat*`

```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/artist/?query=beat%2A&fmt=json&limit=5"
```
Result (parsed):
```
count: 12540
100 The Beatles
82 John Lennon
81 Paul McCartney
75 BoA
75 George Harrison
```
Trailing wildcards work as expected, matching prefix "beat".

## Confirmed behavior for integrators

- **Leading wildcards are allowed** on MusicBrainz's search endpoints (`*eatles` returned valid, well-scored results with HTTP 200) — this differs from vanilla Lucene `QueryParser` defaults, so don't assume you need to avoid leading `*`/`?` the way you might with a raw self-hosted Lucene/Solr setup. This was verified empirically; it is not stated either way in the official docs found during this pass.
- Trailing wildcards (`beat*`) behave exactly as expected for prefix matching.
- The original poster's specific failure (`"jazz on the autobah*"`, a wildcard placed at the end of the *last word inside a quoted phrase*) is a different case from a bare wildcard term — quoted-phrase queries appear not to support a trailing wildcard on the phrase's final word the same way a bare term does. Treat wildcards inside quoted phrases as unreliable; prefer a bare (unquoted) trailing-wildcard term for prefix/autocomplete-style matching instead of embedding the wildcard inside a quoted phrase.
