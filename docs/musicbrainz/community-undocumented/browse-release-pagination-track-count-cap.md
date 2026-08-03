# Browse pagination: `limit=` can silently return fewer items than requested when track-level data is included

Source: community claim (paraphrased in search-engine summaries citing MusicBrainz community/user reports of release-browsing behavior); independently verified against the live API. General browse/pagination mechanics are officially documented at https://musicbrainz.org/doc/MusicBrainz_API — this file captures the specific **truncation quirk** that is not obvious from that page: the returned page size depends on `inc=` parameters, not just `limit=`.
Kind: community-reported quirk, verified against the live API
Retrieved: 2026-07-07
API version: MusicBrainz ws/2 (current production)

## Claim (as reported by users, verified independently)

Browsing releases with `limit=100` does not always return 100 releases per page. When track/media data is pulled in, the server caps the *total tracks* returned across the page (reported figure: no more than ~500 tracks per page) to keep requests from timing out, so the actual `releases` array length varies and can be well under `limit`. The documented fix is to advance `offset` by the number of items actually returned in each page, not by the fixed `limit` value.

## Verification (exact commands + response, verbatim)

### Without track-level `inc=` — full `limit=100` honored

```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/release?artist=b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d&limit=100&fmt=json"
```
Result (parsed):
```
release-count: 3254
releases.length: 100
release-offset: 0
```
Every one of the 100 requested releases was returned — no truncation when media/track data isn't requested.

### With `inc=recordings` (pulls in track/media data) — `limit=100` is NOT honored

```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/release?artist=b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d&limit=100&inc=recordings&fmt=json"
```
Result (parsed):
```
release-count: 3254
releases.length: 86      <-- fewer than the requested limit=100
release-offset: 0
sum of track-count across returned releases: 494
```

Same artist, same `limit=100`, same `offset=0` — but adding `inc=recordings` cut the page down from 100 releases to 86, capping the total track count at 494 (under ~500).

## Confirmed behavior for integrators

- `limit=` is a request, not a guarantee, when the `inc=` parameters pull in track-level data (e.g. `inc=recordings`, and by extension other media-heavy includes on release browse/lookup).
- The server appears to cap the **total number of tracks** materialized in one response (observed cap in the ~500 range) rather than the number of releases, so `releases.length` can be anywhere from 1 up to `limit`.
- **Do not assume you can advance pagination by adding `limit` to `offset`.** Always read back how many items the response actually contains (e.g. `releases.length`, or the entity-specific `-offset`/`-count` fields) and advance `offset` by that actual count, not by the requested `limit`, or you will skip records.
- This is specific to endpoints/inc combinations that expand to track/media-level data; plain browse/search without such includes reliably returns exactly `limit` items per page (as shown by the first test above) up to the total count.
