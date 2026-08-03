# Pagination / `limit` / `page` quirks (testbed-verified)

Source: testbed probing of `https://ws.audioscrobbler.com/2.0/`, 2026-07-07 (api_key=REDACTED),
prompted by a community bug report cited below. Official docs for `user.getRecentTracks` state
`limit` "Defaults to 50, with a maximum of 200."

## Real server-side ceiling is 1000, not the documented 200

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user=rj&api_key=REDACTED&format=json&limit=201"
→ 201 track objects returned; "@attr":{"perPage":"201","totalPages":"754","page":"1","user":"RJ","total":"151375"}

curl -s "https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user=rj&api_key=REDACTED&format=json&limit=1001"
→ {"message":"limit param out of bounds (1-1000)","error":6}

curl -s "https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user=rj&api_key=REDACTED&format=json&limit=0"
→ {"message":"limit param out of bounds (1-1000)","error":6}
```

**Verdict: CONFIRMED (testbed, 2026-07-07). `user.getRecentTracks` silently accepts and honors
`limit` values above the documented 200 cap, all the way up to the real ceiling of 1000 (the exact
bound is disclosed only in the error message triggered by going over it, "limit param out of bounds
(1-1000)"). For a bulk-scrobble-history client this cuts the number of pagination round-trips by 5x
compared to following the documented 200 max.**

## `page=0` is rejected, not treated as "first page"

A 2015 client-library bug report noted a behavior change:

> "API takes a parameter called `page`. Behaviour of API used to be that when `page=1`, the response
> would be the same as `page=0` prepended with the 'now playing' track." Current behavior: "When
> `page=0`: The API returns an error... When `page=1`: the now playing track is no longer included."
> — https://github.com/inflatablefriends/lastfm/issues/78, opened 2015-09-01 (retrieved 2026-07-07)

**Testbed verification (2026-07-07) — the `page=0` rejection still holds today:**

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user=rj&api_key=REDACTED&format=json&page=0"
→ {"message":"page param out of bounds (1-1000000)","error":6}
```

**Verdict: CONFIRMED (testbed, 2026-07-07). Pages are 1-indexed; `page=0` is a hard error (error 6),
not an alias for page 1. Bound disclosed in the error text: `page` accepts 1–1,000,000.**

## `limit` bounds-checking is inconsistent across methods

Three different paginated methods handle an out-of-range `limit=0` three different ways — none of
this is documented anywhere official:

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user=rj&api_key=REDACTED&format=json&limit=0"
→ {"message":"limit param out of bounds (1-1000)","error":6}          # explicit bounds error

curl -s "https://ws.audioscrobbler.com/2.0/?method=library.getartists&user=rj&api_key=REDACTED&format=json&limit=0"
→ {"message":"Operation failed - Most likely the backend service failed. Please try again.","error":8}  # generic error, wrong code

curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.gettoptracks&artist=Radiohead&api_key=REDACTED&format=json&limit=0"
→ 200 OK, no error — silently falls back to the default limit (50 tracks returned;
  "@attr":{"artist":"Radiohead","page":"1","perPage":"50","totalPages":"9210","total":"460454"})
```

**Verdict: CONFIRMED (testbed, 2026-07-07). Do not assume a uniform "invalid limit → error 6"
contract across the API: `user.getRecentTracks` validates and reports the real bound,
`library.getArtists` reports a generic/misleading "backend service failed" (error 8) for what is
actually a bad parameter, and `artist.getTopTracks` doesn't validate at all — it just silently
clamps to its default page size. Integrators should validate `limit` client-side rather than relying
on server error codes to catch a bad value.**
