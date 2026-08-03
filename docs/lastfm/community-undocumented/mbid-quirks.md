# `mbid` (MusicBrainz ID) parameter/field quirks

Source: GitHub issues of major client libraries + MetaBrainz community forum, cross-checked against
`https://ws.audioscrobbler.com/2.0/` on 2026-07-07 (api_key=REDACTED) where a read-only GET could
test the claim.

## Claim: Last.fm returns a MusicBrainz *Track* MBID where a *Recording* MBID is expected

MusicBrainz distinguishes a "Recording" entity (the abstract audio) from a "Track" entity (that
recording's placement on one specific release) — they get different MBIDs. ListenBrainz found
Last.fm's data conflates the two:

> "Since around 4 or 5 months, scrobbles import from Last.fm into LB are track MBID instead of
> recording MBID." A MusicBrainz editor commented bluntly: "Never trust any MBIDs from the Last.fm
> API." A concrete example given: the same identifier `69a49e39-32d9-3724-a7a4-f40f4f25de35` resolves
> at `musicbrainz.org/track/...` but not at `musicbrainz.org/recording/...` — i.e. Last.fm is handing
> out a Track MBID in a field callers expect to be a Recording MBID.
> — https://community.metabrainz.org/t/lb-431-last-fm-api-returns-track-mbid-instead-of-recording-mbid-for-new-scrobbles/431016,
> reported 2019-06-07, example dated 2020-08 (retrieved 2026-07-07)

ListenBrainz's fix was client-side, not a Last.fm change:

> "We're storing MBIDs from last.fm in a field that is not the default recording mbid field."
> (iliekcomputers, 2020-08-14, same thread)

**Verdict: UNVERIFIED via this testbed. This claim is fundamentally about which *MusicBrainz* entity
type a given UUID resolves to — confirming it requires cross-referencing MusicBrainz's own database
(a different service, out of scope for the Last.fm-only read-only testbed used here). The Last.fm API
response itself has no field that labels its `mbid` values as "recording" vs. "track" type, so this
can't be settled by inspecting Last.fm responses alone. Recorded as a credible, well-corroborated
community finding: treat any `mbid` returned by Last.fm as untyped/unverified and re-resolve it
against MusicBrainz yourself before assuming it's a Recording MBID.**

## Claim: a track with no MBID silently returns its *artist's* MBID instead (client-library bug)

> "when calling `.get_mbid()` on a track that has no listed MBID, the MBID of the corresponding
> _artist_ will be returned (with no warning that you're getting the wrong data)." Reporter's repro:
> `network.get_track('static-x','set it off').get_mbid()` returned `8ac9e35c-e92d-4030-99ef-ba4127b1555c`
> — the artist's MBID, not a track MBID. Expected behavior: return `None`.
> — https://github.com/pylast/pylast/issues/146, opened 2015-09-04 (retrieved 2026-07-07)

**Testbed re-verification attempt (2026-07-07) — NOT reproducible with the original example today:**
```
curl -s "https://ws.audioscrobbler.com/2.0/?method=track.getinfo&artist=static-x&track=set+it+off&api_key=REDACTED&format=json"
→ {"track":{"name":"Set It Off", ... "artist":{"name":"Static-X","url":"..."}, ...}}
  (no "mbid" key present anywhere in the response — not on the track, not on the artist)
```
Compare to a track/artist pair that *is* fully MusicBrainz-linked today, showing the two `mbid`
fields are correctly separate when both are present:
```
curl -s "https://ws.audioscrobbler.com/2.0/?method=track.getinfo&artist=Radiohead&track=Creep&api_key=REDACTED&format=json"
→ {"track":{"name":"Creep","mbid":"012e70cd-4ef6-37bf-8ebf-02d318fe5151", ...,
            "artist":{"name":"Radiohead","mbid":"a74b1b7f-71a5-4011-9441-d0b5e4122711", ...}, ...}}
```

**Verdict: PARTIALLY VERIFIED / STALE. The specific 2015 repro case no longer reproduces — Last.fm's
current `track.getInfo` for Static-X/"Set It Off" omits `mbid` entirely (on both track and artist)
rather than exposing the artist mbid where the track mbid should be, meaning either the underlying
metadata linkage changed over the ~10 years since the report, or Last.fm fixed the field-omission
behavior. What we *can* confirm today: `mbid` is not guaranteed to be present at all — for
well-linked tracks (Creep) both track and artist mbid appear as distinct, correctly-scoped UUIDs; for
under-linked tracks (this Static-X example) neither appears. Client code should treat a missing
`mbid` as "no MBID available" and never assume a present `mbid` at the track level is definitely a
track-level identifier without checking it's not identical to the sibling artist `mbid` — the 2015
pylast bug shows this kind of field-crossing has happened in at least one client library's parsing
layer, even if not reproducible as a raw-API bug against today's data.**
