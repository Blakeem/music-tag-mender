# Merged entity MBIDs: HTTP 301 redirect, with body already reflecting the new entity

Source: https://eve.gd/2025/10/09/using-a-public-api-or-the-instability-of-musicbrainz-ids/ (blog, Martin Paul Eve, 2025-10-09); background mechanism name (`gid_redirect`) from https://wiki.musicbrainz.org/Category:Redirects [curator note 2026-07-07: the community.metabrainz.org "how-to-use-regex-in-search" thread previously cited here as "community context" does not discuss gid_redirect or redirects at all — verified via fetch, it is the regex-search thread already cited in lucene-regex-field-search-syntax.md — the citation was a mismatch and has been removed; the blog-post claims below remain independently confirmed by the live 301 test]
Kind: community/blog claim, verified against the live API
Retrieved: 2026-07-07
API version: MusicBrainz ws/2 (current production)

## Claim (verbatim, from the blog post)

> "These IDs wouldn't change...but that I could rely on the ID I put in being returned by the API."
>
> "A redirect on an API is not enough if you rely on matching up the expected ID to data in templates and so on."
>
> "APIs should provide a history of why an identifier is redirecting, if it changes, with information about the redirects to it."

The author gives a concrete example of a release whose MBID changed after a merge:
- Original MBID used locally: `a82fd9b7-4129-4e44-8867-a1a21493ee8e`
- Current (post-merge) MBID: `114bdc29-02ca-45e8-83cb-9b2dae354e20`

His claim: querying the old MBID returns the new entity's data (including the new `id`), not an error and not literally the ID that was requested.

## Verification (exact command + response, verbatim)

```
$ curl -s -D - -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/release/a82fd9b7-4129-4e44-8867-a1a21493ee8e?fmt=json"
```

Response headers:
```
HTTP/2 301
content-type: application/json; charset=utf-8
content-length: 688
location: https://musicbrainz.org/ws/2/release/114bdc29-02ca-45e8-83cb-9b2dae354e20?fmt=json
access-control-allow-origin: *
```

Response body (the 301 response has a body — most HTTP clients that don't follow redirects will miss this):
```json
{"title":"Losing It","quality":"normal","packaging-id":"119eba76-b343-3e02-a292-f0f00644bb9b","id":"114bdc29-02ca-45e8-83cb-9b2dae354e20","country":"GB","cover-art-archive":{"count":1,"back":false,"artwork":true,"front":true,"darkened":false},"asin":null,"text-representation":{"language":"eng","script":"Latn"},"barcode":"5060913709754","date":"2021-12-17","disambiguation":"","status":"Official","release-events":[{"date":"2021-12-17","area":{"disambiguation":"","type-id":null,"type":null,"sort-name":"United Kingdom","iso-3166-1-codes":["GB"],"name":"United Kingdom","id":"8a754a16-0027-3a29-b6d7-2b40ea0481ed"}}],"status-id":"4e304316-386d-3409-af2e-78857eec5cfe","packaging":"None"}
```

## Confirmed behavior for integrators

- Looking up an MBID that has been merged into another entity returns **HTTP 301** with a `Location` header pointing at the canonical (post-merge) MBID's URL.
- The 301 response **already carries a full JSON/XML body** for the target entity (not just a redirect stub) — the body's `"id"` field is the *new* MBID, not the one you requested.
- If your client follows redirects transparently (e.g. `curl -L`, most HTTP libraries by default) you will silently get data for a different MBID than you queried, with no indication in the payload that a merge occurred. Clients that key data by the MBID they sent must compare the response `id` against the request MBID, or inspect the `Location`/status code before following it, to detect merges.
- This applies across entity types that support merging (releases confirmed here; the same `gid_redirect`-backed mechanism applies to artists, labels, recordings, works, release-groups, areas, places, events, series per https://wiki.musicbrainz.org/Category:Redirects).
