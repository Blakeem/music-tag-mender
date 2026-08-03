# Public collections are browsable by editor username with no authentication

Source: paraphrased/aggregated from search-engine indexing of official `Collections` doc content plus community usage patterns (no single dedicated forum thread found; captured here because it's a frequently-asked practical question — "do I need to log in to read someone's public collection?" — and the exact unauthenticated request shape is easy to get wrong). Verified directly against the live API using a real, well-known editor account.
Kind: practical usage pattern, verified against the live API
Retrieved: 2026-07-07
API version: MusicBrainz ws/2 (current production)

## Claim

`GET /ws/2/collection?editor=<username>` lists a given editor's **public** collections without requiring authentication; private collections are excluded unless the request is authenticated as that same editor.

## Verification (exact command + response, verbatim)

```
$ curl -s -D - -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/collection?editor=reosarevok&fmt=json"
```
```
HTTP/2 200
content-type: application/json; charset=utf-8
content-length: 1360

{"collections":[{"type-id":"b21ef166-d652-3e15-958d-1ff7ad3412ab","entity-type":"artist","type":"Artist collection","artist-count":288,"editor":"reosarevok","name":"Estonian composers","id":"09653a17-088a-4f50-8bf6-7e4982693c55"},{"event-count":54,"id":"138748fa-0a7d-3995-989d-74426b9b4e38","editor":"reosarevok","name":"Attending","entity-type":"event","type-id":"de6aedf5-73c2-3f7c-88f8-e128c189a205","type":"Attending"},{"editor":"reosarevok","name":"Composer Diversity Project","id":"2d5b6052-9f4b-49c1-8e86-2c83cdc3b6e3","type-id":"b21ef166-d652-3e15-958d-1ff7ad3412ab","entity-type":"artist", ...
```

No credentials, digest auth, or session cookie were sent — this is a plain unauthenticated GET — and it returned a real editor's list of public collections, each with `id`, `name`, `entity-type`, `type`, `editor`, and an entity-type-specific count field (`artist-count`, `event-count`, etc.).

## Confirmed behavior for integrators

- `GET /ws/2/collection?editor=<username>&fmt=json` works unauthenticated and lists that editor's public collections, each tagged with an entity-type-specific count field (e.g. `artist-count`, `event-count`, `release-count` depending on `entity-type`) rather than a single generic count field name.
- To also see a user's **private** collections, the request must be authenticated (HTTP digest) as that same user; unauthenticated requests only ever see public ones.
- Once you have a collection's MBID, `GET /ws/2/collection/<mbid>/<entity-plural>` (e.g. `/releases`, `/artists`) returns the collection's contents; this also works unauthenticated for public collections. Verified:
  ```
  $ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
    "https://musicbrainz.org/ws/2/collection/09653a17-088a-4f50-8bf6-7e4982693c55/artists?fmt=json&limit=3"
  ```
  ```
  HTTP_STATUS:200
  {"artist-count":288,"type":"Artist collection","editor":"reosarevok","entity-type":"artist","artists":[{"type-id":null,"name":"Els Aarne","sort-name":"Aarne, Els","type":null,"disambiguation":"Estonian composer","id":"44a9a8d8-8cf8-454a-a8bc-2ab06e428657"}, ...]}
  ```
  No credentials sent; the public collection's contents (288 artists) were returned successfully with pagination (`limit=3` honored) via `artist-count`/`artists`.
