# `cover-art-archive` object appears on release lookups automatically — no `inc=` needed

Source: https://community.metabrainz.org/t/check-if-cover-art-is-available-via-musicbrainz-api/582862 ("Check if cover art is available via MusicBrainz API" — MetaBrainz Community Discourse)
Kind: community claim, verified against the live API
Retrieved: 2026-07-07
API version: MusicBrainz ws/2 (current production)

## Claim (verbatim/paraphrased from the thread)

Original poster **fhe** asked whether the MusicBrainz search endpoint surfaces cover-art availability, or whether checking requires a separate Cover Art Archive (CAA) call per result.

Community member **outsidecontext** explained: search results don't include cover-art metadata, but **individual release lookups do**, via a `"cover-art-archive"` object with fields:
```json
{
  "cover-art-archive": {
    "artwork": true,
    "count": 11,
    "back": true,
    "front": true,
    "darkened": false
  }
}
```
`artwork`: any art exists; `count`: number of images; `front`/`back`: those specific image types exist; `darkened`: art exists but is disabled/hidden for copyright reasons (effectively unavailable). Noted: CAA itself has no rate limiting, so bulk per-release CAA calls (for the cases search doesn't cover) are feasible. A feature request (**SEARCH-223**) to add cover-art presence to search results directly exists but hadn't gained traction as of the thread.

## Verification (exact command + response, verbatim)

Looked up a release **without** requesting `inc=cover-art-archive` (or any cover-art-related `inc=` at all — only `inc=recordings` was passed, for an unrelated reason) to test whether the object appears unconditionally:

```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/release/114bdc29-02ca-45e8-83cb-9b2dae354e20?inc=recordings&fmt=json"
```
Parsed `cover-art-archive` field from the response:
```json
{"darkened":false,"count":1,"back":false,"artwork":true,"front":true}
```
The object is present with exactly the fields the thread describes, confirmed live, and confirmed to require **no explicit `inc=` parameter** — it's included by default on single-release lookups.

## Confirmed behavior for integrators

- Single release lookups (`GET /ws/2/release/<mbid>`) always include a `cover-art-archive` summary object (`artwork`, `count`, `front`, `back`, `darkened`) at no extra cost — you do not need `inc=` anything to get it, contrary to how most other supplementary data on this API works.
- This summary tells you *whether* cover art exists and its shape, but not the images themselves — actually fetching image bytes/thumbnails still requires a separate call to the Cover Art Archive (`coverartarchive.org`), which is a different service from `ws/2` with its own (effectively unlimited, per outsidecontext) rate limits.
- This `cover-art-archive` object is **not** present on search-endpoint (`query=`) results or on browse-endpoint list items — only on a direct single-entity lookup. If you need cover-art availability for a list of releases (e.g. from a search or browse response), you must follow up with one lookup (or a CAA call) per release; there is no bulk way to get it as of this API version (per the open, untraction'd SEARCH-223 request cited above).
- `darkened: true` means art technically exists in the archive but has been suppressed (typically for copyright reasons) — treat it as unavailable for display purposes even though `artwork` may be `true`.
