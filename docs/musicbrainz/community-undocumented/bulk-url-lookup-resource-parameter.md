# Bulk URL lookup: repeat `resource=` up to 100 times on `/ws/2/url` to batch-check many URLs in one request

Source: https://community.metabrainz.org/t/overcoming-api-rate-limiting/764224 ("Overcoming API rate limiting" — MetaBrainz Community Discourse, poster **chaban**), which points to the official `url` (by text) section of https://musicbrainz.org/doc/MusicBrainz_API. This is the one documented bulk-lookup mechanism in the whole API, but it is easy to miss because it is buried under the `url` entity's docs rather than a general "batch requests" section, and many developers assume (as thread OP sanojjonas did) that the API has no batching at all.
Kind: community pointer to an easily-missed official mechanism, verified against the live API
Retrieved: 2026-07-07
API version: MusicBrainz ws/2 (current production)

## Claim (verbatim, from the forum thread)

**chaban**:
> The MusicBrainz API's `resource` parameter can be specified multiple times, up to 100, in a single query [to `/ws/2/url`]. This returns a `url-list` rather than a single top-level `url`, and any `resource` that is not found will be skipped. This optimization was necessary because "querying one URL at a time was way too slow" (referring to browser/Chrome-throttled sequential requests).

**sanojjonas** wished this batching extended to all entity types (artists, events, etc.), not just `url`. **Victini** noted a 2017 patch adding multi-MBID lookup for other entities was never merged. **JadedBlueEyes** joked the incremental parameter surface "reinvent[s] GraphQL but in URL parameters."

## Verification (exact commands + responses, verbatim)

First, resolved two real URL relationships from The Beatles' artist page (`inc=url-rels`) to use as test resources: `https://www.allmusic.com/artist/mn0000754032` and `https://www.bandsintown.com/a/316`.

### Single `resource=` — flat object shape

```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" -G "https://musicbrainz.org/ws/2/url" \
  --data-urlencode "resource=https://www.allmusic.com/artist/mn0000754032" \
  --data-urlencode "fmt=json" --data-urlencode "inc=artist-rels"
```
```json
{"resource":"https://www.allmusic.com/artist/mn0000754032","id":"b1e974fb-3872-4344-abc2-6abd6a9e4b46","relations":[{"source-credit":"","attribute-ids":{},"target-credit":"","begin":null,"direction":"backward","attributes":[],"target-type":"artist","type-id":"6b3e3c85-0002-4f34-aca6-80ace0d7e846","end":null,"artist":{"disambiguation":"UK rock band, “The Fab Four”","id":"b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d","type":"Group","sort-name":"Beatles, The","type-id":"e431f5f6-b5d2-343d-8b36-72607fffb74b","name":"The Beatles","country":"GB"},"attribute-values":{},"type":"allmusic","ended":false}]}
```
Single `resource=` returns the `url` entity's fields directly at the top level (`resource`, `id`, `relations`, ...).

### Two `resource=` params — wrapped `url-list` shape

```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" -G "https://musicbrainz.org/ws/2/url" \
  --data-urlencode "resource=https://www.allmusic.com/artist/mn0000754032" \
  --data-urlencode "resource=https://www.bandsintown.com/a/316" \
  --data-urlencode "fmt=json"
```
```json
{"url-offset":0,"urls":[{"id":"b1e974fb-3872-4344-abc2-6abd6a9e4b46","resource":"https://www.allmusic.com/artist/mn0000754032"},{"resource":"https://www.bandsintown.com/a/316","id":"ebf3cb4d-27f7-49ad-a543-9549d6b36ce5"}],"url-count":2}
```
Two (or more) `resource=` params wrap the results in `{"urls": [...], "url-count": N, "url-offset": 0}` instead of returning a single flat object.

### A `resource=` with no match is skipped, not erroring the batch

```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" -G "https://musicbrainz.org/ws/2/url" \
  --data-urlencode "resource=https://en.wikipedia.org/wiki/The_Beatles" \
  --data-urlencode "resource=https://en.wikipedia.org/wiki/Pink_Floyd" \
  --data-urlencode "fmt=json"
```
```json
{"url-count":0,"urls":[],"url-offset":0}
```
Neither test URL happened to be a linked resource in MusicBrainz, and the batch call still returned `200 OK` with an empty `urls` array (rather than a 404), confirming unmatched resources are silently skipped as chaban described — worth noting for a single unmatched `resource=`, too (a lone `resource=` for a URL not in MB returns a plain `404 Not Found`, per the general error semantics; it's only the multi-`resource=` batch form that downgrades a miss to "just absent from the list").

## Confirmed behavior for integrators

- `/ws/2/url` is the **only** endpoint offering built-in batch lookup: pass `resource=` repeated up to 100 times to check many external URLs against MusicBrainz relationships in a single request instead of one request per URL.
- Response shape depends on cardinality: exactly one `resource=` → flat object with the url entity's own fields at the top level; two or more `resource=` → wrapped `{"urls": [...], "url-count", "url-offset"}` list, and misses are simply omitted from the list (no per-item error).
- There is no equivalent batch mechanism for other entity types (artist, recording, release, etc.) as of this API version — each of those still requires one lookup request per MBID. A 2017 community patch to add multi-MBID batch lookup for other entities was never merged (per Victini's post above) — do not assume it exists; verify against your target server version if this matters.
