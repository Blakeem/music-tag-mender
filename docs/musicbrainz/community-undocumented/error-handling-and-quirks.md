Source: NOT an official doc page. This file is testbed-derived — the official docs (musicbrainz-api.md,
search.md) do not document HTTP status codes or error-response bodies in any dedicated section, so
these facts were established empirically against the live production API (https://musicbrainz.org/ws/2/)
per the project's verification protocol: every command below was actually run and its response excerpt
recorded verbatim, unedited except for whitespace/truncation noted inline.
Retrieved: 2026-07-07
User-Agent used for all requests: `aipg-docs-test/1.0 (blakeem@gmail.com)`

---

## Error response bodies

Both formats wrap errors in a simple two-field structure: `error` (message) and `help` (a link to
`https://musicbrainz.org/development/mmd`, printed on effectively every error regardless of type).

JSON shape:
```
{"error":"<message>","help":"For usage, please see: https://musicbrainz.org/development/mmd"}
```

XML shape:
```
<?xml version="1.0" encoding="UTF-8"?>
<error><text>...</text><text>For usage, please see: https://musicbrainz.org/development/mmd</text></error>
```

## Verified status codes

### 400 Bad Request — malformed or nil MBID

```
curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" -D - "https://musicbrainz.org/ws/2/artist/00000000-0000-0000-0000-000000000000?fmt=json"
```
```
HTTP/2 400
content-type: application/json; charset=utf-8

{"error":"Invalid mbid.","help":"For usage, please see: https://musicbrainz.org/development/mmd"}
```
Same result for a syntactically invalid (non-UUID) path segment, e.g. `/ws/2/artist/not-a-uuid`.

### 400 Bad Request — invalid inc= value

```
curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" -D - "https://musicbrainz.org/ws/2/artist/5b11f4ce-a62d-471e-81fc-a69a8278c7da?inc=bogus-thing&fmt=json"
```
```
HTTP/2 400
{"error":"bogus-thing is not a valid inc parameter for the artist resource.","help":"..."}
```
The message names the offending parameter and the resource type — useful for programmatic retry/backoff logic that wants to strip bad `inc=` values.

### 400 Bad Request — missing mandatory query parameter (malformed request shape)

```
curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" -D - "https://musicbrainz.org/ws/2/artist/?fmt=json"
```
```
HTTP/2 400
{"error":"The given parameters do not match any available query type for the artist resource.","help":"..."}
```
This is what you get when the request matches neither a lookup (no MBID), a browse (no recognized `<entity>=<mbid>` filter param), nor a search (`query=` param). Same 400 with the same message shape if `query=` is entirely absent on a search-style URL.

### 404 Not Found — well-formed MBID with no matching entity

```
curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" -D - "https://musicbrainz.org/ws/2/artist/12345678-1234-1234-1234-123456789abc?fmt=json"
```
```
HTTP/2 404
{"help":"For usage, please see: https://musicbrainz.org/development/mmd","error":"Not Found"}
```
XML equivalent:
```
<?xml version="1.0" encoding="UTF-8"?>
<error><text>Not Found</text><text>For usage, please see: https://musicbrainz.org/development/mmd</text></error>
```
Note the distinction from the 400 case above: a *nil* MBID (`00000000-...`) or malformed string is rejected as `Invalid mbid.` (400) before any lookup is attempted, but a syntactically valid v4-shaped UUID that simply doesn't exist in the database returns 404. The official `url` lookup doc explicitly documents 404 for a missing `resource`; this generalizes to all MBID lookups.

### 401 Unauthorized — accessing an authentication-gated inc= without credentials

```
curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" -D - "https://musicbrainz.org/ws/2/artist/5b11f4ce-a62d-471e-81fc-a69a8278c7da?inc=user-tags&fmt=json"
```
```
HTTP/2 401
www-authenticate: Digest realm="musicbrainz.org", charset=UTF-8, qop="auth,auth-int", nonce="...", opaque="...", algorithm="MD5"
www-authenticate: Bearer realm="musicbrainz.org", charset=UTF-8

{"error":"You are not authorized to access this resource.","help":"..."}
```
Confirms the two supported auth schemes at the transport level: the server advertises both `WWW-Authenticate: Digest` (realm `musicbrainz.org`, MD5, qop `auth,auth-int`) and `WWW-Authenticate: Bearer` (OAuth2) on the same 401 — a client can pick either. This applies to any `user-*` inc= (user-tags, user-ratings, user-genres) and to private-collection access.

### 406 Not Acceptable — invalid fmt= value

```
curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" -D - "https://musicbrainz.org/ws/2/artist/5b11f4ce-a62d-471e-81fc-a69a8278c7da?fmt=yaml"
```
```
HTTP/2 406
content-type: application/xml; charset=utf-8

<error><text>Invalid format. Either set an Accept header (recognized mime types are application/json and application/xml), or include a fmt= argument in the query string (valid values for fmt are json and xml).</text><text>For usage, please see: https://musicbrainz.org/development/mmd</text></error>
```
Error body is served as XML even though the request asked for an unrecognized format (XML is the fallback/default, matching the main doc's "XML is the default format" statement).

### 501 Not Implemented — search on genre (documented as unsupported, status code was not)

```
curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" -D - "https://musicbrainz.org/ws/2/genre?query=rock&fmt=json"
```
```
HTTP/2 501
{"error":"This hasn't been implemented yet.","help":"..."}
```
The official API page says "browse and search are not implemented for genre entities at this time" but does not say what HTTP status that produces; it's 501, not 400/404.

### 503 Service Unavailable — rate limiting

Not independently reproduced here (deliberately avoided hammering the service — see `rate-limiting.md`, which documents this status code and the throttling rules directly from the official page). Officially documented, included for completeness of the error-code inventory.

## Pagination quirks (search `limit=`)

The official search docs (search.md) state: "An integer value defining how many entries should be returned. Only values between 1 and 100 (both inclusive) are allowed. If not given, this defaults to 25." They do not say what happens when you pass a value **outside** that range. Empirically, out-of-range values do not error and do not clamp to the boundary — they silently fall back to the default of 25:

```
curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" "https://musicbrainz.org/ws/2/artist?query=beatles&limit=101&fmt=json"
→ count: 281, artists returned: 25   (same as limit unset)

curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" "https://musicbrainz.org/ws/2/artist?query=beatles&limit=500&fmt=json"
→ count: 281, artists returned: 25

curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" "https://musicbrainz.org/ws/2/artist?query=beatles&limit=0&fmt=json"
→ count: 281, artists returned: 25

curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" "https://musicbrainz.org/ws/2/artist?query=beatles&limit=-5&fmt=json"
→ count: 281, artists returned: 25
```
Confirmed in-range values are respected exactly: `limit=50` → 50 returned, `limit=100` → 100 returned. HTTP status stays 200 in every case above — a client that blindly sends a user-supplied `limit` will not get an error signal that its value was out of range, it will silently get fewer results than expected.

`offset=` past the end of the result set also returns 200 with an empty `artists`/`releases`/etc. array rather than an error:
```
curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" "https://musicbrainz.org/ws/2/artist?query=beatles&offset=9999&fmt=json"
→ count: 281, offset: 9999, artists returned: 0
```
(This behavior is not stated in the official docs for search; the official Browse "Paging" section only describes offset behavior for browse requests, not search.)

## Format negotiation

Confirmed both stated mechanisms work and `fmt=` takes precedence when both are present, per the official page's claim:
```
curl -s -A "..." -H "Accept: application/json" -D - "https://musicbrainz.org/ws/2/artist/5b11f4ce-a62d-471e-81fc-a69a8278c7da"
→ content-type: application/json; charset=utf-8   (Accept header alone works, no fmt= needed)
```

`/ws/2/genre/all?fmt=txt` confirmed to return `content-type: text/plain; charset=utf-8` with one genre name per line, alphabetically ordered, as documented:
```
curl -s -A "..." "https://musicbrainz.org/ws/2/genre/all?fmt=txt" | head -5
2 tone
2-step
3-step
aak
abhang
```
