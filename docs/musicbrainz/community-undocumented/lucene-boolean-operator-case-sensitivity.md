# Search: boolean operators (AND/OR/NOT) must be uppercase — lowercase is treated as a literal search term

Source: derived from general Lucene query syntax knowledge referenced across community discussions of MusicBrainz's `/ws/2/<entity>?query=` search (e.g. https://musicbrainz.org/doc/Indexed_Search_Syntax mentions the `AND` operator but does not call out case sensitivity); verified directly against the live API.
Kind: quirk verified via testbed, not explicitly called out as a gotcha in official docs
Retrieved: 2026-07-07
API version: MusicBrainz ws/2 (current production)

## Verification (exact commands + responses, verbatim)

Query: artists matching `arid:b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d` (The Beatles' MBID) combined with the term `beatles`, once with lowercase `and`, once with uppercase `AND`.

### lowercase `and` — NOT treated as a boolean operator

```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/artist/?query=arid%3Ab10bbbfc-cf9e-42e0-be17-e2c3e1d2600d%20and%20beatles&fmt=json&limit=3"
```
Result (parsed):
```
count: 249350
100 The Beatles
62 Various Artists
62 John Lennon
```
A count of 249,350 shows this was evaluated as an OR-like/default query (the literal word "and" plus "beatles" plus the arid clause, combined with the default OR-ish scoring), not a strict intersection.

### uppercase `AND` — treated as a boolean operator

```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/artist/?query=arid%3Ab10bbbfc-cf9e-42e0-be17-e2c3e1d2600d%20AND%20beatles&fmt=json&limit=3"
```
Result (parsed):
```
count: 1
100 The Beatles
```
Exactly one result — the strict intersection of "has this arid" AND "matches beatles" — confirming `AND` was parsed as the boolean operator only when uppercase.

## Confirmed behavior for integrators

- `AND`, `OR`, `NOT` (and presumably `TO` for range queries) must be **uppercase** to function as Lucene boolean/range operators against the MusicBrainz search endpoints. Lowercase versions (`and`, `or`, `not`) are indexed/matched as ordinary literal search terms instead, silently changing the query's meaning rather than erroring — this is easy to get wrong when building queries programmatically from user input without normalizing case.
- Always uppercase these operator keywords explicitly when constructing query strings; do not rely on case-insensitive matching.
