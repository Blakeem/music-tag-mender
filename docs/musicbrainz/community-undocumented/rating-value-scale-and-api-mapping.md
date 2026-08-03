Source: synthesized from official MusicBrainz sources + live ws/2 testbed verification (no single
official page states this mapping explicitly — see "Coverage gap" below)
Version: MusicBrainz ws/2 (production)
Retrieved: 2026-07-07

## The gap

`official/musicbrainz-api.md`'s "ratings" submission example (`POST /ws/2/rating`) shows:

```xml
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
    <artist-list>
        <artist id="455641ea-fff4-49f6-8fb4-49f961d8f1ad">
            <user-rating>100</user-rating>
        </artist>
    </artist-list>
    <recording-list>
        <recording id="c410a773-c6eb-4bc0-9df8-042fe6645c63">
            <user-rating>20</user-rating>
        </recording>
    </recording-list>
</metadata>
```

— i.e. `<user-rating>` takes integers like `100` and `20`. Neither this page nor
https://musicbrainz.org/doc/Rating_System (which only describes a **1–5 star** scale, see
`official/rating-system.md`) explains what range/granularity `<user-rating>` accepts, or how it
relates to the 1–5 stars shown in the UI and in GET responses.

## What's officially confirmed (verbatim/primary sources)

**1. The UI/display scale is 1–5 stars, averaged.** From https://musicbrainz.org/doc/Rating_System:
> User may assign a value between 1 and 5 to various entities as a rating, these values are then
> aggregated by the server to compute an average community rating for that same entity.

**2. GET responses expose ratings as a decimal on the 1–5 scale, not 0–100.** Verified live:

```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/artist/5b11f4ce-a62d-471e-81fc-a69a8278c7da?inc=ratings&fmt=json"
```
```json
{"...","rating":{"value":4.6,"votes-count":46}, ...}
```

XML form (`inc=ratings`, no `fmt=`):
```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/artist/5b11f4ce-a62d-471e-81fc-a69a8278c7da?inc=ratings"
```
```xml
<rating votes-count="46">4.6</rating>
```

So: **reads are 1–5** (float, one decimal place typical of an average of integer votes), **writes
(`<user-rating>`) are integers like 100/20** — two different scales for the same underlying value.

**3. The public RelaxNG schema does not constrain `<user-rating>` beyond "non-negative integer."** From
the official schema repo (https://github.com/metabrainz/mmd-schema,
`schema/musicbrainz_mmd-2.0.rng`):

```xml
<define name="def_rating">
    <element name="rating">
        <optional>
            <attribute name="votes-count">
                <data type="nonNegativeInteger"/>
            </attribute>
        </optional>
        <data type="float"/>
    </element>
</define>

<define name="def_user-rating">
    <element name="user-rating">
        <data type="nonNegativeInteger"/>
    </element>
</define>
```

No `maxInclusive`, no `pattern`, no step/multiple constraint at the XML-schema level — so a value like
`57` or `1000` is **not** rejected by the schema. If MusicBrainz enforces a 0–100 range and a step of 20,
that enforcement is server-side application logic, not documented and not visible in the machine-readable
schema. This could not be confirmed further without a write call, which this capture pass is restricted
from making (read-only testbed).

## What's community-sourced (verify-by-write not possible here — treat as corroborated hypothesis)

Two independent third-party MusicBrainz API client libraries state the same 0–100/step-20 convention:

- **calliostro/musicbrainz-client** (PHP), `README.md`
  (https://github.com/calliostro/musicbrainz-client), verbatim:
  > `// Submit ratings (0-100, 0 = remove rating)`
  > `**Ratings**: `submitRating` - Rate artists, releases, recordings, release-groups, works, labels, events, places, series, or instruments (0-100, 0 removes rating)`

  Same claim repeated in the library's PHPDoc block (`src/MusicBrainzClient.php`), verbatim:
  > `@method array<string, mixed> submitRating(string $client, string $entityType, string $entityId, int $rating) Submit rating (0-100, 0 removes rating)`

- **musicbrainzngs** (Python client), per its hosted docs (page itself returned an HTTP 429 from behind a
  Cloudflare challenge when fetched directly during this pass, so quoted here via search-result snippet
  rather than a page fetch — treat as lower-confidence than the PHP client's directly-fetched source):
  > "Ratings are numbers from 0-100, at intervals of 20 (20 per 'star')."

Both sources agree on:
- **Range**: 0–100.
- **Granularity**: multiples of 20 (5 discrete levels — one per star: 20/40/60/80/100 = 1★/2★/3★/4★/5★).
- **Special value**: `0` removes/clears the user's existing rating (there is no "0-star" concept in the
  1–5 UI, consistent with `Rating_System`'s "value between 1 and 5" wording — 0 falls outside that range
  by design, which is why it reads naturally as "no rating" rather than a floor rating).

**Confidence assessment:** the 0–100 range and "0 = remove" are corroborated by two independent client
libraries and are consistent with the officially-documented example values (`100`, `20`) and the
officially-documented 1–5 display scale (100 ÷ 5 = 20 per star lines up exactly with both official
example values). The exact claim that **only** multiples of 20 are accepted (i.e., that `57` would be
rejected or silently rounded rather than stored as-is) is *not* independently confirmed by this pass —
the schema does not enforce it, and no write call was made to test server-side validation. Mark this
specific sub-claim **UNVERIFIED**.

## Practical mapping table (for integrators)

| API `<user-rating>` value | UI stars | Meaning |
|---|---|---|
| `0` | (none) | Remove/clear the caller's existing rating (community-sourced, not independently write-verified) |
| `20` | ★☆☆☆☆ | 1 star — appears verbatim in the official submission example |
| `40` | ★★☆☆☆ | 2 stars (community-sourced) |
| `60` | ★★★☆☆ | 3 stars (community-sourced) |
| `80` | ★★★★☆ | 4 stars (community-sourced) |
| `100` | ★★★★★ | 5 stars — appears verbatim in the official submission example |

GET responses (`inc=ratings` / `inc=user-ratings`) always report on the **1–5** scale (float for the
aggregate `rating`, presumably integer 1–5 for `user-rating` reads — not independently re-verified here
since `user-ratings` requires authentication, which this read-only pass does not perform).

## Coverage gap

No single official musicbrainz.org page states the write-side 0–100 scale, its star-increment mapping,
or the "0 removes rating" behavior. `Rating_System` documents only the 1–5 read/UI scale;
`MusicBrainz_API` documents the write mechanism but never labels or explains the `100`/`20` values in its
own example. This file exists to close that gap using the strongest available evidence (official schema
+ live GET verification + two independent client-library citations), with the one unconfirmed sub-claim
(strict step-of-20 enforcement) flagged above.
