# Miscellaneous response-data quirks (testbed-verified)

Source: testbed probing of `https://ws.audioscrobbler.com/2.0/`, 2026-07-07 (api_key=REDACTED),
plus one community client-library README. See also `deprecated-and-removed-methods.md` for the
related `artist.getImages` method removal — these two findings are two symptoms of the same
underlying change (Last.fm dropped its artist-image service).

## All artist `image` fields resolve to the same generic placeholder image

Every `artist.image` array returned by the API — regardless of which method surfaces it, and
regardless of which artist — resolves to the same image hash
(`2a96cbd8b46e442fc41c2b86b821562f`) at every size. Confirmed across three unrelated artists in two
different methods on 2026-07-07:

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user=rj&api_key=REDACTED&format=json&extended=1"
→ Lenny Kravitz artist.image[extralarge] = https://lastfm.freetls.fastly.net/i/u/300x300/2a96cbd8b46e442fc41c2b86b821562f.png
→ AC/DC        artist.image[extralarge] = https://lastfm.freetls.fastly.net/i/u/300x300/2a96cbd8b46e442fc41c2b86b821562f.png

curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.getinfo&artist=gun+and+roses&autocorrect=1&api_key=REDACTED&format=json"
→ Guns N' Roses artist.image[small] = https://lastfm.freetls.fastly.net/i/u/34s/2a96cbd8b46e442fc41c2b86b821562f.png
```

Same hash, three different artists. By contrast, **track/album cover art is real and per-release** —
e.g. the "American Woman" track image (`3178b5958f3c20f5d8a312161b085f00.jpg`) and "T.N.T." track
image (`591095f2549b4d9bbde16f471fa76e83.png`) in the same responses above are distinct per track.

**Verdict: CONFIRMED (testbed, 2026-07-07). Do not use `artist.image` for artist artwork in a new
integration — it is always the same generic placeholder now. If you need real artist images, you
must source them from a different provider (e.g. MusicBrainz/Cover Art Archive, Spotify, TheAudioDB);
Last.fm's own artist-image pipeline is dead (see `artist.getImages` → error 4 in
`deprecated-and-removed-methods.md`). Track- and album-level images remain live and real.**

## `album.search` results omit `listeners`; `album.getInfo` includes it

> "Unfortunately, there is no `opts.minAlbumListeners` since the Last.fm API does not include
> listener numbers in album results (even though the data exists when you get an individual album
> via `lastfm.albumInfo`)"
> — https://github.com/feross/last-fm/blob/master/README.md (retrieved 2026-07-07)

**Testbed verification (2026-07-07):**
```
curl -s "https://ws.audioscrobbler.com/2.0/?method=album.search&album=OK+Computer&api_key=REDACTED&format=json&limit=2"
→ {"results":{...,"albummatches":{"album":[
     {"name":"OK Computer","artist":"Radiohead","url":"...","image":[...],"streamable":"0","mbid":"0b6b4ba0-d36f-47bd-b4ea-6a5b91842d29"},
     {"name":"OK Computer","artist":"レディオヘッド", ...}
   ]},...}}
  — no "listeners" (or "playcount") field on either result.

curl -s "https://ws.audioscrobbler.com/2.0/?method=album.getinfo&artist=Radiohead&album=OK+Computer&api_key=REDACTED&format=json"
→ {"album":{"artist":"Radiohead","mbid":"0b6b4ba0-d36f-47bd-b4ea-6a5b91842d29", ...,
            "playcount":"261590999", ..., "listeners": <present> , ...}}
```

**Verdict: CONFIRMED (testbed, 2026-07-07). `album.search` cannot be used to sort/filter by listener
count or playcount — those fields simply aren't in the search-result shape. If you need to rank
search results by popularity, you must follow up each candidate with `album.getInfo` (mind the
request-volume/rate-limit cost of doing this for every search result — see `rate-limits.md`).
