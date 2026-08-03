# `autocorrect` parameter and `getCorrection` methods (testbed-verified)

Source: testbed probing of `https://ws.audioscrobbler.com/2.0/`, 2026-07-07 (api_key=REDACTED). The
`autocorrect` parameter (0|1) is listed tersely in official per-method docs ("Transform misspelled
artist and track names into correct artist and track names") on many but not all methods that accept
it, without showing what actually changes in the response. Captured here with a concrete before/after.

## Behavior of `autocorrect=0` vs `autocorrect=1`

Test input: an intentionally misspelled artist name, `"gun and roses"` (should resolve to "Guns N'
Roses").

**`autocorrect=0` (default) — the literal, uncorrected name is echoed back with an empty/placeholder
record:**
```
curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.getinfo&artist=gun+and+roses&autocorrect=0&api_key=REDACTED&format=json"
→ {"artist":{"name":"GUN AND ROSES",
             "url":"https://www.last.fm/music/+noredirect/GUN+AND+ROSES",
             "image":[{"#text":"","size":"small"}, ...all four sizes empty...],
             "streamable":"0","ontour":"0", ...}}
```

**`autocorrect=1` — resolves to the canonical artist with full data:**
```
curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.getinfo&artist=gun+and+roses&autocorrect=1&api_key=REDACTED&format=json"
→ {"artist":{"name":"Guns N' Roses",
             "url":"https://www.last.fm/music/Guns+N%27+Roses",
             "image":[{"#text":"https://lastfm.freetls.fastly.net/i/u/34s/2a96cbd8b46e442fc41c2b86b821562f.png","size":"small"}, ...],
             ...}}
```

**Undocumented URL quirk: `/+noredirect/` prefix.** When `autocorrect=0` (or omitted) fails to match
an exact artist name, the returned `url` field is prefixed with a literal `+noredirect/` path segment
(`https://www.last.fm/music/+noredirect/GUN+AND+ROSES`) instead of the normal `https://www.last.fm/music/<Artist+Name>`
shape. This is not mentioned in any official parameter doc found. It signals "this page will not
301-redirect to a canonical/corrected artist page" and is a cheap way to detect client-side that a
name did not resolve cleanly, without needing to separately call `artist.getCorrection`.

**Verdict: CONFIRMED (testbed, 2026-07-07).**

## `artist.getCorrection` returns the same suggestion `autocorrect=1` applies silently

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.getcorrection&artist=gun+and+roses&api_key=REDACTED&format=json"
→ {"corrections":{"correction":{"artist":{"name":"Guns N' Roses","url":"https://www.last.fm/music/Guns+N%27+Roses"},
                                  "@attr":{"index":"0"}}}}
```

**Verdict: CONFIRMED (testbed, 2026-07-07). `artist.getCorrection` / `track.getCorrection` are useful
when you want to *show the user* the suggested correction and let them confirm it, versus
`autocorrect=1` which silently substitutes the corrected entity into whatever method you called it
on. Both draw from the same underlying correction data.**
