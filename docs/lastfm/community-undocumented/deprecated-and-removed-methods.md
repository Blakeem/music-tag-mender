# Deprecated / removed Last.fm API methods (testbed-verified)

Source: community reports (GitHub issues of client libraries) cross-checked live against
`https://ws.audioscrobbler.com/2.0/` on 2026-07-07 (api_key=REDACTED). None of these methods appear
in the current official method index (https://www.last.fm/api and
https://lastfm-docs.github.io/api-docs/, both retrieved 2026-07-07).

## Fully removed methods (return error 3 "Invalid Method")

Community reports attribute most of this removal to Last.fm's March 2016 site/API relaunch:

> "Last.fm removed some of their APIs due to their relaunch in March 2016. Various methods were
> removed including `artist.getEvents`, `artist.getPastEvents`, `artist.getShouts`, and various geo
> methods like `geo.getEvents`, `geo.getMetroArtistChart`, and others."
> (community synthesis of multiple sources, see individual GitHub issues below)

Individual reports:

> `artist.getevents` — "the Last.FM artist.getevents API is no longer supported per
> (http://www.last.fm/api)." Suggested alternative: Songkick's API.
> — https://github.com/ampache/ampache/issues/1468, opened 2017-01-20 (retrieved 2026-07-07)

> `geo.getEvents` / `artist.getEvents` returning `"Lastfm::ApiError: Invalid Method - No method with
> that name in this package"` when called via the `ruby-lastfm` wrapper; reporter noted "the Last.fm
> API status page didn't reflect any deletion or changes to these methods" at the time.
> — https://github.com/youpy/ruby-lastfm/issues/81 (retrieved 2026-07-07)

**Testbed verification — all return HTTP 200 with error 3, confirmed 2026-07-07:**

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.getevents&artist=Radiohead&api_key=REDACTED&format=json"
→ {"message":"Invalid Method - No method with that name in this package","error":3}

curl -s "https://ws.audioscrobbler.com/2.0/?method=geo.getevents&location=London&api_key=REDACTED&format=json"
→ {"message":"Invalid Method - No method with that name in this package","error":3}

curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.getshouts&artist=Radiohead&api_key=REDACTED&format=json"
→ {"message":"Invalid Method - No method with that name in this package","error":3}

curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.getpastevents&artist=Radiohead&api_key=REDACTED&format=json"
→ {"message":"Invalid Method - No method with that name in this package","error":3}

curl -s "https://ws.audioscrobbler.com/2.0/?method=geo.getmetroweeklychart&api_key=REDACTED&format=json"
→ {"message":"Invalid Method - No method with that name in this package","error":3}

curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.gettopfans&artist=Radiohead&api_key=REDACTED&format=json"
→ {"message":"Invalid Method - No method with that name in this package","error":3}

curl -s "https://ws.audioscrobbler.com/2.0/?method=album.getbuylinks&artist=Radiohead&album=OK+Computer&country=United+States&api_key=REDACTED&format=json"
→ {"message":"Invalid Method - No method with that name in this package","error":3}

curl -s "https://ws.audioscrobbler.com/2.0/?method=user.getplaylists&user=rj&api_key=REDACTED&format=json"
→ {"message":"Invalid Method - No method with that name in this package","error":3}

curl -s "https://ws.audioscrobbler.com/2.0/?method=album.getimages&artist=Radiohead&album=OK+Computer&api_key=REDACTED&format=json"
→ {"message":"Invalid Method - No method with that name in this package","error":3}

curl -s "https://ws.audioscrobbler.com/2.0/?method=track.getimages&artist=Radiohead&track=Creep&api_key=REDACTED&format=json"
→ {"message":"Invalid Method - No method with that name in this package","error":3}

curl -s "https://ws.audioscrobbler.com/2.0/?method=event.getinfo&event=12345&api_key=REDACTED&format=json"
→ {"message":"Invalid Method - No method with that name in this package","error":3}

curl -s "https://ws.audioscrobbler.com/2.0/?method=venue.getevents&venue=12345&api_key=REDACTED&format=json"
→ {"message":"Invalid Method - No method with that name in this package","error":3}

curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.getpodcast&artist=Radiohead&api_key=REDACTED&format=json"
→ {"message":"Invalid Method - No method with that name in this package","error":3}
```

**Verdict: CONFIRMED removed, not merely "deprecated but working" — these all now 404 at the method
level (error 3), same as calling a nonexistent method name. This includes the whole events/venue
family, artist/album/track image-fetching, buy-links, shoutboxes, top-fans charts, and playlists.**

## Recognized-but-blocked: `artist.getImages` (anomaly)

Unlike `album.getImages` and `track.getImages` (which return plain error 3, i.e. the method name
itself is gone), `artist.getImages` returns a *different* error — the method is still registered in
Last.fm's router, but access to it has been cut off entirely:

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.getimages&artist=Radiohead&api_key=REDACTED&format=json"
→ {"message":"Authentication Failed - You do not have permissions to access the service","error":4}
```

**Verdict: CONFIRMED (testbed, 2026-07-07). This is a genuinely undocumented distinction: most
retired methods 404 as "Invalid Method" (error 3), but `artist.getImages` alone still exists in the
routing table and instead fails auth (error 4) for every caller — consistent with community reports
that Last.fm pulled its artist-image licensing/service around 2015–2016 (see e.g. discussion in
https://github.com/rembo10/headphones/issues/3207 about missing artwork) without deregistering the
method entirely.**

## Deprecated with explicit error code: `user.getArtistTracks`

The official error-code table (https://www.last.fm/api/errorcodes, retrieved 2026-07-07) documents
code 27 as `"Deprecated - This type of request is no longer supported"`, but does not say which
methods trigger it. Testbed probing found a live example:

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=user.getartisttracks&user=rj&artist=Radiohead&api_key=REDACTED&format=json"
→ {"message":"Deprecated - This type of request is no longer supported","error":27}
```

**Verdict: CONFIRMED (testbed, 2026-07-07). `user.getArtistTracks` is the concrete, currently-live
trigger for error 27 — useful for any integration/client library (e.g. older pylast/lastfm.js
bindings) that still exposes a `get_artist_tracks`-style call.**

## Not deprecated (checked and still current, despite community confusion)

`geo.getTopArtists` and `geo.getTopTracks` remain live and documented on the current official API
index (checked https://www.last.fm/api/show/geo.getTopArtists, retrieved 2026-07-07 — no deprecation
notice present). Some community threads conflate these with the removed `geo.getEvents` /
`geo.getMetro*` family; they are not the same methods and are unaffected.

`tag.getSimilar` also still works and returns a normal (if often empty) payload:

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=tag.getsimilar&tag=rock&api_key=REDACTED&format=json"
→ {"similartags":{"tag":[],"@attr":{"tag":"n/a"}}}
```
