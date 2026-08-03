# Last.fm API rate limits (community-known, testbed-verified where possible)

Source: multiple community sources (see per-claim citations). Retrieved 2026-07-07.
Testbed: `https://ws.audioscrobbler.com/2.0/` (api_key=REDACTED).

## Official position: no published numeric limit

The current live Terms of Service page states rate limiting is entirely at Last.fm's discretion,
with no published number:

> Section 4.4 (fetched 2026-07-07 from https://www.last.fm/api/tos):
> "Last.fm sets and enforces limits on use of the API to prevent abuse and ensure reliability of
> service (e.g. limiting the number of API requests that you may make or the number of users you
> may serve), in our sole discretion."

Section 4.3 also defines a "Reasonable Usage Cap":

> "The 'Reasonable Usage Cap' is a maximum of 100 MB, as may be updated by Last.fm from time to
> time. If you would like a larger portion of Last.fm Data, prior written consent from Last.fm is
> required"

**The current ToS text does NOT contain a specific requests-per-second number.**

## Community-cited historical number: 5 req/s per IP, averaged over 5 minutes

A specific numeric limit is widely repeated across client-library docs and Q&A sites, worded almost
identically everywhere, strongly suggesting it was lifted verbatim from an older version of the ToS
(or an older FAQ) that no longer reads this way live:

> "You will not make more than 5 requests per originating IP address per second, averaged over a
> 5 minute period, without prior written consent."
> — quoted by the `lastfm-java` client library wiki, https://github.com/jkovacs/lastfm-java/wiki/Getting-Started
> (retrieved 2026-07-07), which adds: "Note that the API bindings do not protect you from violating
> against the 5-requests-per-second limit. Scheduling and optimising your API calls so that your app
> complies to the TOS is your responsibility." The same page also states: "You agree to cache similar
> artist and any chart data (top tracks, top artists, top albums) for a minimum of one week."

The same "5 requests per originating IP address per second, averaged over a 5 minute period" wording
is repeated by multiple independent secondary sources (Quora answers, blog posts) as a paraphrase of
the ToS, but none of them link to a live copy of the ToS containing that exact sentence today.

**Verdict: UNVERIFIED against the live ToS text (the sentence is not present in the ToS as fetched
2026-07-07) but corroborated as a widely-and-consistently-cited historical figure by an independent
client-library source. Cannot be verified by testbed GETs either — deliberately exceeding the limit
to find the true enforced threshold would violate the read-only/politeness constraint on this task.
Treat "~5 req/s per IP, keep well under it" as the safe community-consensus working assumption, not
a confirmed current number.**

## Error 29 (Rate Limit Exceeded) — real-world reports

The rate-limit error is commonly hit by long-running scrobblers/clients making artist/album metadata
lookups in a loop, at volumes that don't look obviously abusive to the reporter:

> "no artist biography info shown on web interface" ... "biography was showing fine up until 2-3 days
> ago" — logs show repeated failures on `artist.getInfo` and `artist.getSimilar` with:
> `last.fm error(29): Rate Limit Exceeded - This application has made too many requests in a short
> period. If this is your API key, see https://www.last.fm/api/tos#4.4 for information about raising
> the limit.`
> — https://github.com/navidrome/navidrome/issues/2421 (retrieved 2026-07-07)

> "Last.FM returned an error: Rate Limit Exceeded - This application has made too many requests in a
> short period." — reporter states they had only added two artists and neither received artwork.
> — https://github.com/rembo10/headphones/issues/3207, opened 2019-05-09 (retrieved 2026-07-07)

> A user running an automated export script making "5-6 API requests hourly, each with a 10-second
> cooldown between them" and exporting "no more than 500 scrobbles each time" reported being rate
> limited daily, with no technical explanation given by staff before the thread auto-closed.
> — https://support.last.fm/t/big-issue-with-api-limitation/115469 (Nesm0n, 2025-08-11; retrieved
> 2026-07-07)

**Takeaway for integrators (community consensus, not officially confirmed numbers): the enforced
threshold is stricter/burstier than the raw "5/s averaged over 5 min" figure suggests — a batch of
requests with multi-second gaps between each has still triggered error 29 for some users. Client
libraries commonly self-throttle to 1 request per ~0.2–1 second (`sleep(0.2)`–`sleep(1)` between
calls) as a defensive measure, per implementations discussed on
https://mintlify.wiki/cdxker/rotations/guides/lastfm-integration and general pylast usage guidance
(retrieved 2026-07-07).**

## HTTP status vs. error body mismatch

> "Some API calls return HTTP 200 OK status codes even when the response contains an error. For this
> reason make sure to check your response payload to validate it."
> — Unofficial Last.fm API docs, https://lastfm-docs.github.io/api-docs/codes/ (retrieved 2026-07-07)

Practical implication: do not rely solely on the HTTP status code to detect failures (including rate
limiting) — always parse the response body for an `"error"` key regardless of HTTP status.

## Testbed verification performed

We did not attempt to trigger or measure the actual rate-limit threshold (forbidden by task scope —
read-only, ~1 req/s, must back off on 429/error 29). All calls made during this research stayed under
~1 request/second and none returned error 29 or HTTP 429.
