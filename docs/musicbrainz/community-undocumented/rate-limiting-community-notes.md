# Rate limiting: community clarifications beyond the official numbers

Source: https://community.metabrainz.org/t/api-user-login-vs-public-requests-for-rate-limiting/751568 ("API user login vs public requests for rate limiting"); https://community.metabrainz.org/t/overcoming-api-rate-limiting/764224 ("Overcoming API rate limiting")
Kind: community claims — practical/operational guidance supplementing (not replacing) the official rate-limiting numbers on https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting. The specific throughput thresholds are UNVERIFIED here (verifying them would require deliberately exceeding the live rate limit, which this capture pass avoided as it conflicts with the read-only/considerate-use mandate); only the qualitative, safely-observable claims below are recorded, and are marked accordingly.
Retrieved: 2026-07-07
API version: MusicBrainz ws/2 (current production)

## Claim 1: authentication does not by itself raise your rate limit for ordinary data retrieval

Thread: "API user login vs public requests for rate limiting" (2025-03-27)

Original poster **32290jffjewlf** (building an Ampache integration) asked whether enabling login credentials for otherwise-public data requests would help with rate limiting.

**RustyNova** replied:
> "All you really require is a proper user agent for your app. The rate limit stays 1 per second too."
> "However I wouldn't mind to have a option to log in to your user account to increase the rate limit" (framed as a wishlist item, i.e. not current behavior).

**jesus2099** replied:
> Login is necessary "if you would like to fetch some of your private collection or tags" (i.e. auth is about *access to private data*, not about *unlocking a higher rate*), and any future rate-limit increase for logged-in users should be reserved "for really active editors, to prevent opportunist accounts."

**UNVERIFIED**: The specific "1 request per second" figure and whether authentication currently changes it were not independently load-tested in this pass (doing so would mean deliberately provoking rate-limiting against the live service, which was avoided). Treat this as the community's shared understanding, consistent with the qualitative behavior confirmed indirectly below, not as an independently measured number.

**Partially corroborated observation**: every read (unauthenticated) request made during this capture session — dozens of GETs across artist/release/collection/genre/annotation/url endpoints, paced roughly 1 request/second — succeeded without ever receiving an HTTP 503, consistent with (but not proof of) the "unauthenticated GETs are simply rate-limited, not blocked outright" characterization in the thread. No authenticated requests were made (this pass is read-only/unauthenticated per the testbed mandate), so the "does auth change the limit" half of the claim could not be tested either way.

## Claim 2: no built-in multi-entity batch endpoint outside of `/ws/2/url` — a token-bucket client-side pattern is the community's workaround

Thread: "Overcoming API rate limiting" (2025-06-02)

**sanojjonas** wanted to fetch large numbers of records without one request per entity.

**chaban** pointed out the only real batching mechanism is the `resource=` parameter on `/ws/2/url` (see `bulk-url-lookup-resource-parameter.md` in this directory for the verified details) — there is no equivalent for artists/releases/recordings/etc.

**RustyNova** described a client-side workaround pattern rather than a server feature: run a token-bucket rate limiter (e.g. 5 tokens capacity, 1 token/sec refill) so short bursts are possible while long-run average stays within the server's limit, but acknowledged this doesn't help much for large jobs (e.g. "when you need to fetch 5000 recordings…1 request per second" is still ~83 minutes minimum, batching aside).

**Victini** noted a 2017 community patch adding true multi-MBID batch lookup for arbitrary entity types was written but never merged into `musicbrainz-server` — confirming (as of that report) it remains unavailable in production. This was not independently re-verified against the current server in this pass beyond confirming (see `bulk-url-lookup-resource-parameter.md`) that only `/ws/2/url` supports repeated `resource=`; attempting the same pattern (`?mbid=...&mbid=...`) on another entity type was not tested here.

## Practical guidance for integrators (community-sourced, use with the above caveats)

- Don't expect authentication to raise your read-request throughput; its purpose for GETs is unlocking access to *private* data (your own collections/tags), not a higher rate ceiling.
- For bulk lookups, `/ws/2/url?resource=` (repeated, up to 100) is the only true server-side batching available; every other entity type still costs one request per MBID.
- For large jobs, budget for roughly one request per second sustained (per the community's shared understanding) — plan for a token-bucket-style client rate limiter with a small burst allowance rather than a hard fixed-interval sleep, and always back off fully on any HTTP 503.
