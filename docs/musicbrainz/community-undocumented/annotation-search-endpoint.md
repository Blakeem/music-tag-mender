# `/ws/2/annotation?query=` — searching entity annotations directly

Source: general community/developer awareness that annotations (free-text notes editors attach to entities) are searchable via their own endpoint, distinct from searching the entities themselves; this is present in official search-syntax examples (e.g. annotation score examples referenced in search results) but easy to overlook since it isn't grouped with the main entity search endpoints in most third-party client libraries. Verified directly against the live API.
Kind: under-documented/easily-missed endpoint, verified against the live API
Retrieved: 2026-07-07
API version: MusicBrainz ws/2 (current production)

## Verification (exact command + response, verbatim)

```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/annotation?query=beatles&fmt=json&limit=3"
```
```json
{"created":"2026-07-07T19:01:12.380Z","count":1010,"offset":0,"annotations":[{"type":"release","score":100,"entity":"6e2dfd3e-962c-4291-98b6-1987a7b4d17f","name":"Why Don't We Do It In Abbey Road - The Album","text":"An album of original songs by various Beatles (and Beatles related) tribute acts"},{"type":"artist","score":99,"entity":"d09d2a70-8b81-445a-b990-80976de8189d","name":"Peter Nash","text":"Pete Nash is a 'second generation' Beatles Fan (since 1975), former staff writer for The Beatles Book Monthly and contributor to magazines such as Record Collector, Mojo, Q, NME and various music journals, as well as contributing to The Beatles Anthology book and film plus many other Beatles and ...
```

## Confirmed behavior for integrators

- `GET /ws/2/annotation?query=<lucene query>` searches the free-text annotation notes editors have attached to entities (any entity type), independent of searching the entities' own indexed fields (name, tags, etc.).
- Each hit includes `type` (the annotated entity's type, e.g. `release`, `artist`), `entity` (that entity's MBID), `name` (the entity's display name at annotation time), `text` (the annotation content itself), and the usual `score`.
- Standard `query=`, `limit=`, `offset=`, `fmt=` parameters apply the same as other search endpoints. This is useful for full-text searching editorial notes/trivia that wouldn't otherwise be found via a normal entity name/field search.
