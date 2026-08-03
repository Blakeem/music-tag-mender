# `user.getRecentTracks` — `extended` parameter and now-playing quirks

Source: testbed probing of `https://ws.audioscrobbler.com/2.0/`, 2026-07-07 (api_key=REDACTED),
plus community reports cited below. `extended` is a genuinely under-documented parameter: the
official docs list it tersely as "Includes extended data in each artist, and whether or not the
user has loved each track. 0|1" without showing an example diff.

## What `extended=1` actually changes (verified diff)

Same call, same track (`user=rj&limit=1`), only `extended` toggled — captured 2026-07-07:

**`extended=0` (or omitted):**
```
curl -s "https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user=rj&api_key=REDACTED&format=json&limit=1&extended=0"
→ {"recenttracks":{"track":[{
     "artist":{"mbid":"0ef3f425-9bd2-4216-9dd2-219d2fe90f1f","#text":"Lenny Kravitz"},
     "streamable":"0","image":[...track cover art...],
     "mbid":"0fbd6a97-051c-34aa-aa30-daf79375f4be","album":{...},
     "name":"American Woman","url":"...","date":{"uts":"1783435868","#text":"07 Jul 2026, 14:51"}
   }],"@attr":{...}}}
```

**`extended=1`:**
```
curl -s "https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user=rj&api_key=REDACTED&format=json&limit=1&extended=1"
→ {"recenttracks":{"track":[{
     "artist":{"url":"https://www.last.fm/music/Lenny+Kravitz","name":"Lenny Kravitz",
               "image":[...generic placeholder, see below...],"mbid":""},
     "date":{"uts":"1783435868","#text":"07 Jul 2026, 14:51"},
     "mbid":"0fbd6a97-051c-34aa-aa30-daf79375f4be",
     "name":"American Woman","image":[...track cover art...],"url":"...",
     "streamable":"0","album":{...},"loved":"0"
   }],"@attr":{...}}}
```

Concrete, verified differences:

1. **Track gains a top-level `"loved":"0"|"1"` field.** (This is the documented half of `extended`.)
2. **`artist` expands from a compact `{"mbid":..., "#text": name}` shape to a full object**
   `{"url":..., "name":..., "image":[4 sizes], "mbid":...}`.
3. **The artist's `mbid` can *change* between the two shapes for the same track.** In this capture,
   non-extended returned artist mbid `0ef3f425-9bd2-4216-9dd2-219d2fe90f1f` for Lenny Kravitz;
   extended returned artist `mbid` as an **empty string** for the same track/artist in the same
   response. Do not treat the extended-mode artist mbid as authoritative if you already have it from
   the compact form — prefer the non-extended field, or cross-check with `artist.getInfo`.
4. **The artist `image` array added in extended mode is not real per-artist art.** Every artist we
   sampled (Lenny Kravitz, AC/DC) returned the exact same image hash
   (`2a96cbd8b46e442fc41c2b86b821562f`) for all four sizes — Last.fm's generic default-artist
   placeholder, not artwork specific to that artist. (Consistent with Last.fm having pulled its
   artist-image service — see `deprecated-and-removed-methods.md`, `artist.getImages`.) Track-level
   `image` (album art) is unaffected and remains real per-release art.

**Verdict: CONFIRMED (testbed, 2026-07-07). `extended=1` is worth requesting for the `loved` flag and
the artist `name`/`url`, but do not rely on it for artist images (always the same placeholder) or for
artist `mbid` (may come back empty even when the non-extended call returned one).**

## Community-reported quirks not independently reproducible via read-only testbed

These two claims come from an active 2025 support-forum thread and could not be confirmed here
because they require an account with a track *currently* playing (a live "now playing" scrobble
session), which this read-only, no-scrobbling testbed cannot manufacture. Recorded as-is, marked
UNVERIFIED:

> "The most recent track will not include a date field if it is currently playing" ... "I also
> noticed that `limit=1` returns two tracks instead of one."
> — vyfor, https://support.last.fm/t/user-getrecenttracks-the-most-recent-track-will-not-include-a-date-field-if-it-is-currently-playing/115900,
> 2025-08-18 (retrieved 2026-07-07). The thread references this as a recurrence of a previously-known
> regression ("date missing from nowplaying in user.getrecenttracks").

**Status: UNVERIFIED. We probed several accounts (`rj`, `lastfm`, `eavesdropper`) on 2026-07-07 for a
live now-playing entry and none were currently scrobbling at request time, so the `nowplaying`
attribute / missing-date behavior could not be exercised. Practical guidance for integrators: do not
assume every track in `getRecentTracks` has a `date` field — code defensively for a missing `date` on
the first element, and verify `limit=1` actually returns exactly one element rather than assuming it.**

## Historical pagination change: now-playing track used to be prepended at `page=1`

> "Behaviour of API used to be that when `page=1`, the response would be the same as `page=0`
> prepended with the 'now playing' track." As of the report, `page=1` no longer includes the
> now-playing track at all (it appears as its own unnumbered entry with `@attr nowplaying=true`
> instead, per current official docs).
> — https://github.com/inflatablefriends/lastfm/issues/78, 2015-09-01 (retrieved 2026-07-07)

**Status: historical context only, not independently re-verified (no live now-playing session
available during this pass — see above). Cross-reference `pagination-and-limit-quirks.md` for the
current, testbed-confirmed `page=0` rejection.**
