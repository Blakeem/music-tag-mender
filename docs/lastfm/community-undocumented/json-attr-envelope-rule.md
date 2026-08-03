Source: testbed capture against `https://ws.audioscrobbler.com/2.0/` (api_key=REDACTED), 2026-07-07.
Written to close a gap flagged in `../INDEX.md` Coverage notes: the JSON conversion rules stated in
`../official/rest-requests.md` (retrieved 2026-07-07) do not document the `@attr` envelope pattern.
This file is a testbed-derived empirical rule, not an official capture — every example below is a
live `curl` command run against the API, output pasted verbatim (api_key redacted).

# The JSON `@attr` pagination/attribute envelope: when it appears, precisely

## The gap in the official rule

`official/rest-requests.md` states 4 XML→JSON conversion rules, quoted here for reference:

> 1. Attributes are expressed as string member values with the attribute name as key.
> 2. Element child nodes are expressed as object members values with the node name as key.
> 3. Text child nodes are expressed as string values, unless the element also contains attributes, in
>    which case the text node is expressed as a string member value with the key `#text`. *
> 4. Repeated child nodes will be grouped as an array member with the shared node name as key.

None of these four rules mention an `@attr` key at all. Taken at face value, rule 1 says attributes
always become flat string members of the parent object — and the official page's own worked example
(`tag.search`, XML `<results for="disco">`) matches that reading: the JSON shows `"for": "disco"` as a
flat sibling of `"tagmatches"`, not nested under anything.

But every paginated **list** method actually captured in this doc set (`user.getRecentTracks`,
`library.getArtists`, `chart.getTopArtists`, `geo.getTopArtists`, etc.) returns its container
attributes (`page`, `perPage`, `totalPages`, `total`, plus any filter attribute like `user` or
`country`) nested under a `"@attr"` object instead. Rules 1–4 don't explain this, and don't mention
`@attr` as a JSON key at all.

**Note on the brief's suggested comparison method:** the brief's example of an "attribute-only"
element was `tag.search`'s `<results for="disco">`. That method is now dead on the live API:

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=tag.search&tag=disco&api_key=REDACTED&format=json&limit=3"
→ {"message":"Invalid Method - No method with that name in this package","error":3}
```

(Consistent with `deprecated-and-removed-methods.md` — `tag.search` is not
in the current `tag.*` method index.) `artist.search`, `album.search`, and `track.search` share the
exact same `<results for="...">` root shape and remain live, so they were used in its place below —
and the result is itself a finding (see "Finding 1").

## Finding 1: the official docs' own example is stale — `results.for` is `@attr`-wrapped today

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.search&artist=cher&api_key=REDACTED&format=json&limit=1"
```
```json
{"results":{"opensearch:Query":{"#text":"","role":"request","searchTerms":"cher","startPage":"1"},"opensearch:totalResults":"33505","opensearch:startIndex":"0","opensearch:itemsPerPage":"1","artistmatches":{"artist":[{"name":"Cher","listeners":"2243379","mbid":"bfcc6d75-a6a5-4bc6-8282-47aec8531818","url":"https://www.last.fm/music/Cher","streamable":"0","image":[{"#text":"https://lastfm.freetls.fastly.net/i/u/34s/2a96cbd8b46e442fc41c2b86b821562f.png","size":"small"},{"#text":"...","size":"medium"},{"#text":"...","size":"large"},{"#text":"...","size":"extralarge"}]}]},"@attr":{"for":"cher"}}}
```

The underlying XML for the same call (`format` omitted → default XML), showing the `for` attribute
lives directly on `<results>`, same as the official doc's `tag.search` example:

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.search&artist=cher&api_key=REDACTED&limit=1"
```
```xml
<?xml version="1.0" encoding="UTF-8" ?>
<lfm status="ok"><results for="cher"><opensearch:Query role="request" searchTerms="cher" startPage="1"></opensearch:Query>
<opensearch:totalResults>33505</opensearch:totalResults>
<opensearch:startIndex>0</opensearch:startIndex>
<opensearch:itemsPerPage>1</opensearch:itemsPerPage>
<artistmatches><artist><name>Cher</name>
<listeners>2243379</listeners>
<mbid>bfcc6d75-a6a5-4bc6-8282-47aec8531818</mbid>
<url>https://www.last.fm/music/Cher</url>
<streamable>0</streamable>
<image size="small">https://lastfm.freetls.fastly.net/i/u/34s/2a96cbd8b46e442fc41c2b86b821562f.png</image>
<image size="medium">https://lastfm.freetls.fastly.net/i/u/64s/2a96cbd8b46e442fc41c2b86b821562f.png</image>
<image size="large">https://lastfm.freetls.fastly.net/i/u/174s/2a96cbd8b46e442fc41c2b86b821562f.png</image>
<image size="extralarge">https://lastfm.freetls.fastly.net/i/u/300x300/2a96cbd8b46e442fc41c2b86b821562f.png</image>
</artist>
</artistmatches>
</results>
</lfm>
```

`album.search` and `track.search` behave identically (`"@attr":{"for":"believe"}` in both cases):

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=album.search&album=believe&api_key=REDACTED&format=json&limit=3"
→ {"results":{...,"albummatches":{"album":[...]},"@attr":{"for":"believe"}}}

curl -s "https://ws.audioscrobbler.com/2.0/?method=track.search&track=believe&api_key=REDACTED&format=json&limit=3"
→ {"results":{...,"trackmatches":{"track":[...]},"@attr":{"for":"believe"}}}
```

**Verdict: CONFIRMED (testbed, 2026-07-07).** The live `*.search` family now wraps the `for` attribute
under `@attr`, contradicting the flat `"for": "disco"` shown in `official/rest-requests.md`'s own
JSON example. Either the API's JSON serializer changed since that page was captured, or the page's
example was never a literal unedited response (it also silently omits the `opensearch:*` fields that
rule 2 says should appear as object members — those fields **do** appear in the current live JSON, as
shown above). Either way: **do not rely on the official page's JSON sample as accurate for current
`@attr` placement** — use the rule below instead.

## Finding 2: the pagination-attribute case (brief's `user.getRecentTracks` example)

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user=rj&api_key=REDACTED&format=json&limit=1"
```
```json
{"recenttracks":{"track":[{"artist":{"mbid":"0ef3f425-9bd2-4216-9dd2-219d2fe90f1f","#text":"Lenny Kravitz"},"streamable":"0","image":[{"size":"small","#text":"..."},{"size":"medium","#text":"..."},{"size":"large","#text":"..."},{"size":"extralarge","#text":"..."}],"mbid":"0fbd6a97-051c-34aa-aa30-daf79375f4be","album":{"mbid":"11bd170d-ed93-4eef-a77d-0990aebf8a97","#text":"5"},"name":"American Woman","url":"https://www.last.fm/music/Lenny+Kravitz/_/American+Woman","date":{"uts":"1783435868","#text":"07 Jul 2026, 14:51"}}],"@attr":{"user":"RJ","totalPages":"151375","page":"1","perPage":"1","total":"151375"}}}
```

Underlying XML, same call:

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user=rj&api_key=REDACTED&limit=1"
```
```xml
<?xml version="1.0" encoding="UTF-8"?>
<lfm status="ok">
  <recenttracks user="RJ" page="1" perPage="1" totalPages="151375" total="151375">
    <track>
      <artist mbid="0ef3f425-9bd2-4216-9dd2-219d2fe90f1f">Lenny Kravitz</artist>
      <name>American Woman</name>
      <streamable>0</streamable>
      <mbid>0fbd6a97-051c-34aa-aa30-daf79375f4be</mbid>
      <album mbid="11bd170d-ed93-4eef-a77d-0990aebf8a97">5</album>
      <url>https://www.last.fm/music/Lenny+Kravitz/_/American+Woman</url>
      <image size="small">https://lastfm.freetls.fastly.net/i/u/34s/3178b5958f3c20f5d8a312161b085f00.jpg</image>
      <image size="medium">https://lastfm.freetls.fastly.net/i/u/64s/3178b5958f3c20f5d8a312161b085f00.jpg</image>
      <image size="large">https://lastfm.freetls.fastly.net/i/u/174s/3178b5958f3c20f5d8a312161b085f00.jpg</image>
      <image size="extralarge">https://lastfm.freetls.fastly.net/i/u/300x300/3178b5958f3c20f5d8a312161b085f00.jpg</image>
      <date uts="1783435868">07 Jul 2026, 14:51</date>
    </track>
  </recenttracks>
</lfm>
```

`<recenttracks>` carries 5 attributes (`user`, `page`, `perPage`, `totalPages`, `total`) and a direct,
repeated `<track>` child — all 5 attributes land under `"@attr"` in the JSON, not as flat siblings of
`"track"`.

Also note within the same payload: `<artist mbid="...">Lenny Kravitz</artist>` — an attribute
(`mbid`) **plus plain text** (`Lenny Kravitz`) — converts to `{"mbid":"...","#text":"Lenny Kravitz"}`,
flat, exactly per official rule 3. Same for `<album mbid="...">5</album>` and
`<date uts="...">07 Jul 2026, 14:51</date>`. This is the control case: attribute + **text** stays flat.

## Finding 3: attributes + a single non-array child element — still `@attr`, not flat

This isolates whether `@attr`-wrapping requires a *repeated/array* child, or fires for *any* child
element. `artist.getCorrection`'s `<correction index="0">` wraps exactly one non-repeated `<artist>`
child (no arrays anywhere in this particular response):

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.getcorrection&artist=Guns+N+Roses&api_key=REDACTED&format=json"
```
```json
{"corrections":{"correction":{"artist":{"name":"Guns N' Roses","url":"https://www.last.fm/music/Guns+N%27+Roses"},"@attr":{"index":"0"}}}}
```
```
curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.getcorrection&artist=Guns+N+Roses&api_key=REDACTED"
```
```xml
<?xml version="1.0" encoding="UTF-8" ?>
<lfm status="ok"><corrections><correction index="0"><artist><name>Guns N&apos; Roses</name>
<url>https://www.last.fm/music/Guns+N%27+Roses</url>
</artist>
</correction>
</corrections>
</lfm>
```

`track.getCorrection` confirms the same with multiple attributes on one element (`index`,
`artistcorrected`, `trackcorrected`), still wrapping a single non-array `<track>` child:

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=track.getcorrection&artist=Guns+N+Roses&track=Mrbrownstone&api_key=REDACTED&format=json"
→ {"corrections":{"correction":{"track":{"name":"Mr. Brownstone","mbid":"00db6287-f466-3089-979d-ec8f7b292717","url":"https://www.last.fm/music/Guns+N%27+Roses/_/Mr.+Brownstone","artist":{"name":"Guns N' Roses","url":"https://www.last.fm/music/Guns+N%27+Roses"}},"@attr":{"index":"0","artistcorrected":"1","trackcorrected":"1"}}}}
```

**Verdict: CONFIRMED (testbed, 2026-07-07). `@attr`-wrapping does *not* require an array/repeated
child — a single nested object child is enough to trigger it.** This rules out "has a repeated child"
as the deciding factor; the deciding factor is whether the element has **any child element at all**
(object or array), as opposed to plain text.

## Finding 4: the rule applies recursively, at every nesting depth independently

`geo.getTopArtists`'s per-item `<artist rank="1">` (nested inside the paginated list) has its own
`rank` attribute, alongside a mix of plain-text children (`name`, `listeners`, `mbid`, `url`,
`streamable`) *and* a repeated `<image>` child — and gets its own, independent `@attr`, nested one
level below the list's own `@attr`:

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=geo.gettopartists&country=spain&api_key=REDACTED&format=json&limit=1"
```
```json
{"topartists":{"artist":[{"name":"Bad Bunny","listeners":"12751","mbid":"89aa5ecb-59ad-46f5-b3eb-2d424e941f19","url":"https://www.last.fm/music/Bad+Bunny","streamable":"0","image":[{"#text":"...","size":"small"},{"#text":"...","size":"medium"},{"#text":"...","size":"large"},{"#text":"...","size":"extralarge"},{"#text":"...","size":"mega"}],"@attr":{"rank":"1"}}],"@attr":{"country":"Spain","page":"1","perPage":"1","totalPages":"2315","total":"2315"}}}
```
```
curl -s "https://ws.audioscrobbler.com/2.0/?method=geo.gettopartists&country=spain&api_key=REDACTED&limit=1"
```
```xml
<?xml version="1.0" encoding="UTF-8" ?>
<lfm status="ok"><topartists country="Spain" page="1" perPage="1" totalPages="2315" total="2315"><artist rank="1"><name>Bad Bunny</name>
<listeners>12751</listeners>
<mbid>89aa5ecb-59ad-46f5-b3eb-2d424e941f19</mbid>
<url>https://www.last.fm/music/Bad+Bunny</url>
<streamable>0</streamable>
<image size="small">https://lastfm.freetls.fastly.net/i/u/34s/2a96cbd8b46e442fc41c2b86b821562f.png</image>
<image size="medium">https://lastfm.freetls.fastly.net/i/u/64s/2a96cbd8b46e442fc41c2b86b821562f.png</image>
<image size="large">https://lastfm.freetls.fastly.net/i/u/174s/2a96cbd8b46e442fc41c2b86b821562f.png</image>
<image size="extralarge">https://lastfm.freetls.fastly.net/i/u/300x300/2a96cbd8b46e442fc41c2b86b821562f.png</image>
<image size="mega">https://lastfm.freetls.fastly.net/i/u/300x300/2a96cbd8b46e442fc41c2b86b821562f.png</image>
</artist>
</topartists>
</lfm>
```

Note the `<image size="small">...</image>` children (attribute + text) each flatten to
`{"#text":"...","size":"small"}` per rule 3 — consistent with Finding 2 — while `<artist>`'s own
`rank` attribute (attribute + child elements) goes to its own `@attr`, and `<topartists>`'s 5
container attributes go to a separate, outer `@attr` at the parent level. Each element's `@attr` (if
any) sits immediately alongside that same element's own children — it does not bubble up or merge
with a parent's or child's `@attr`.

**Verdict: CONFIRMED (testbed, 2026-07-07).** `@attr` wrapping is applied per-element, independently,
at whatever depth that element occurs — not just at the outermost paginated-list container.

## Finding 5: collision with the child array's key name is not what triggers `@attr`

`artist.getSimilar`'s root `<similarartists artist="Cher">` has an attribute named `artist` that
happens to share its name with the repeated child element also named `<artist>` — a plausible reason
a serializer might need `@attr` (to avoid a key collision between the scalar attribute and the array).
But `geo.getTopArtists` (Finding 4) has no such collision — its attributes are `country`/`page`/
`perPage`/`totalPages`/`total`, none of which match its child key `artist` — and still uses `@attr`.
So collision-avoidance is not the deciding factor; it's a coincidental case of the same general rule:

```
curl -s "https://ws.audioscrobbler.com/2.0/?method=artist.getsimilar&artist=cher&api_key=REDACTED&format=json&limit=2"
→ {"similarartists":{"artist":[{"name":"Sonny & Cher",...},{"name":"Kylie Minogue",...}],"@attr":{"artist":"Cher"}}}
```

`tag.getSimilar` (already captured in `deprecated-and-removed-methods.md`)
adds one more data point: `@attr` appears even when the array it sits beside is **empty** —
`{"similartags":{"tag":[],"@attr":{"tag":"n/a"}}}` — so it's the element's schema/shape (that it *can*
carry a repeated `tag` child), not the runtime item count, that governs whether `@attr` is used.

## The precise rule

For any single XML element in a Last.fm API response:

1. **No attributes on the element** → no `@attr` key is emitted for it at all (trivial case).
2. **Attributes present, and the element's only other content is plain text** (official rule 3's
   case — e.g. `<artist mbid="...">Lenny Kravitz</artist>`, `<image size="small">http://...</image>`)
   → attributes stay **flat, sibling string members** of the resulting object, alongside a `"#text"`
   key holding the text value.
3. **Attributes present, and the element has one or more child *elements*** — whether that's a single
   nested object (`<correction index="0"><artist>...</artist></correction>`) or a repeated/array child
   (`<recenttracks user="..." page="..."><track>...</track>...</recenttracks>`) — **the element's own
   attributes are moved into a nested `"@attr"` object**, sibling to the child-element key(s), instead
   of being flattened into the parent object directly.
4. Rule 3 applies **per element, recursively, at every depth independently** — a list container and
   its individual list items each get their own `@attr` if each individually carries attributes plus
   child-element content (Finding 4).
5. This holds regardless of whether the attribute's name happens to collide with a child array's key
   name (Finding 5) and regardless of whether an eligible array is empty at runtime (Finding 5,
   `tag.getSimilar`) — it's governed by the response shape/schema for that element, not collision
   avoidance or item count.

Restated as a one-line heuristic for a client parser: **if a JSON object could contain an `@attr` key,
treat any of that object's own "container" attributes (pagination info, filter echoes like `for`/
`country`/`artist`, per-item info like `rank`) as living under `obj["@attr"]`, not as top-level keys of
`obj` — unless the object's only other content is a `"#text"` value, in which case those same
attributes are flat siblings of `"#text"` instead.**

## Practical implications for integrators

- **Pagination fields are essentially always under `@attr` for any list-returning method** —
  `page`, `perPage`, `totalPages`, `total` (and any per-call filter echo like `user`, `country`,
  `tag`, `artist`, `for`) — because every list method's container element necessarily has both
  attributes (the pagination info) and a child-element array (the results). A generic pagination
  helper should always look in `response[rootKey]["@attr"]`, never at `response[rootKey]` directly.
- **Do not assume `official/rest-requests.md`'s worked example is representative for search-style
  `results.for`** — it now goes under `@attr` too, per Finding 1. Treat that page's rules 1–4 as
  correct for the *text-vs-attribute* (`#text`) distinction only, not as a complete description of
  attribute placement.
- **Per-item chart/list metadata (e.g. `rank` on `geo.getTopArtists`/`chart.getTopArtists` items) is
  also under that item's own `@attr`**, not a flat `item.rank` field — confirmed in Finding 4.
- **The `<lfm status="...">` root wrapper never appears in `format=json` output at all** — every JSON
  response observed in this testbed session was rooted directly at the method's own element
  (`results`, `recenttracks`, `corrections`, `topartists`, `artists`, `similarartists`, …), with no
  `"lfm"` or `"status"` key present anywhere. `format=json` behaves as if `raw=true` were implicitly
  set for the envelope, even though `raw=true` is documented (`official/rest-requests.md`) only in
  the context of XML responses.
