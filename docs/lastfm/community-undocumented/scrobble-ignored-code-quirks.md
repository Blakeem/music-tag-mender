# Scrobble "ignored" code quirks (community-reported, staff-confirmed; not testbed-verifiable)

Source: Last.fm support community + GitHub issue of a scrobbler client. Retrieved 2026-07-07.
**Not independently verified against the live API**: `track.scrobble` is a write/signed method and
this testbed is restricted to read-only GETs with no session key — per task scope, write methods are
never called. Recorded because it is staff-confirmed in the source thread, which is the strongest
corroboration available without calling the write endpoint.

## Ignored code 1 ("artist ignored") is mis-reported for scrobbles older than 14 days

> Original report: all scrobbles were being rejected with ignored-code 1 ("artist ignored"),
> including for well-known, unambiguous artists (Toto, Joji, Lorde), with the reporter unable to see
> why those artists would be ignored.
>
> Root cause identified by community member tapenoon: "it is not possible to submit scrobbles older
> than 14 days." The rejected scrobbles all had timestamps more than 14 days in the past; recent ones
> succeeded.
>
> Last.fm staff member hjbardenhagen confirmed this is a genuine API bug, not expected behavior:
> "Rumour has it that sometimes there are still bugs in the API. You're right, you should be shown
> the attributes code for a too old timestamp." — i.e. the API returns ignored-code **1** (artist
> ignored) instead of the correct ignored-code **3** (timestamp too old / outside acceptable range)
> when a submitted scrobble's timestamp exceeds the ~14-day submission window.
> — https://support.last.fm/t/all-scrobbles-ignored-with-code-1-artist-ignored-why/6754 (retrieved
> 2026-07-07)

**Practical implication for integrators:** if a batch-import/backfill tool gets `ignoredMessage` code
1 ("artist ignored") for scrobbles you know reference real, well-known artists, check the scrobble
timestamps before assuming an artist-matching problem — timestamps older than ~14 days are a far more
likely cause, mis-reported under the wrong ignored-code. A common workaround used by other scrobbling
tools (at the cost of losing accurate historical timestamps) is to submit old plays using the current
time instead of the original play time.

**Verdict: Staff-confirmed bug report (as strong a corroboration as is available for a write-only
endpoint under this task's read-only constraint); NOT independently reproduced here because doing so
would require calling `track.scrobble`, which is out of scope. Recorded as UNVERIFIED-BY-TESTBED but
credible.**

## Related, less-corroborated report: unusual characters in track title triggering silent ignore

> A scrobble from SoundCloud with unusual characters/formatting in the title showed a green "Now
> Playing" icon but was then rejected on scrobble submission with a red "Ignored --- <title>"
> tooltip and no further detail. The issue reporter could not determine whether the rejection came
> from the scrobbler client or from Last.fm's service itself, and the issue was ultimately labeled
> "non-issue" by the repo maintainers without a confirmed root cause.
> — https://github.com/web-scrobbler/web-scrobbler/issues/1579 (retrieved 2026-07-07)

**Verdict: UNVERIFIED / inconclusive even in the source thread. Included only as a data point that
track titles with unusual characters/formatting have been anecdotally associated with silent scrobble
rejection; no confirmed mechanism, no staff response, and not testable here (write endpoint).**
