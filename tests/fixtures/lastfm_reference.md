# Last.fm reference data (for future M2 tag-resolution tests)

Public-domain / Creative-Commons artists confirmed to have Last.fm pages with usable
`artist.getCorrection` + `artist.getTopTags` responses. Captured 2026-06-02 so M2 tests
can assert against known artists without us needing to legally redistribute audio.

**Why these artists:** all are public-domain (expired copyright) or Creative-Commons
licensed, so if we ever want to commit *real* sample audio (instead of the silent
templates in `templates/`) we legally can. They span the genre-resolution cases M2 must
handle (clean auto, junk-mixed, sparse, and a name-normalization case).

> **No secret here.** The Last.fm API key is never stored in the repo — it lives only in
> the OS config dir (`settings.json`). These are the *responses*, not credentials.
> Tag weights drift over time; treat counts as approximate and assert on *presence /
> ordering of the dominant tag*, not exact values.

| Artist (as queried) | `getCorrection` canonical | MBID | Top tags (name, weight) | Test role |
|---|---|---|---|---|
| Kevin MacLeod | **Kevin Macleod** | `d0429304-ae84-40a9-8e3e-14745354862a` | instrumental 100, ambient 75, jazz 26, folk 19, soundtrack 8, piano 4 | **Name normalization** — correction changes `MacLeod`→`Macleod` |
| Scott Joplin | Scott Joplin | `aec8a328-d2e8-4780-b2ea-318c7f8d6f75` | ragtime 100, jazz 66, piano 32, instrumental 18, classical 7 | **Clean auto** — one dominant genre tag |
| Komiku | Komiku | `fca800c1-6fc3-4bfb-a5de-8c2398c27bc0` | french 100 | **Sparse / no usable genre** — should route to needs_review |
| Broke For Free | Broke For Free | `069a1c1f-14eb-4d36-b0a0-77dffbd67713` | electronic 100, instrumental 58, glitch hop 15, glitch 15, chillwave 8 | **Multi-genre** — vocabulary must pick/allow several |
| Kai Engel | Kai Engel | `aa55b20b-36d8-4093-84f3-c5c7942ff2c8` | piano 100, instrumental 100, modern classical 52, ambient 40, neoclassical 2 | **Tie at the top** — two tags at weight 100 |
| Podington Bear | Podington Bear | `2c15f704-d0f3-4175-80c4-97f395ad7d9a` | electronica 100, electronic 100, downtempo 66, chillout 37, dream pop 19 | **Synonym collapse** — electronica vs electronic |
| United States Marine Band | United States Marine Band | `a2474772-f4bc-4e6a-b85c-5569e1437788` | military band 100, american 36, classical 11, military 11, patriotic 5, shout 1, All 1 | **Junk-mixed** — `american`/`shout`/`All` must be filtered |
| Scott Holmes | Scott Holmes | `43ad772e-7a01-4b7f-83c7-64a3dfcc3084` | ambient 100, post-rock 44 | **Two clean tags** — small, tidy result |

## Endpoints used
- `artist.getCorrection` → canonical name + MBID (the artist-name normalization feature).
- `artist.getTopTags` → ranked community tags with 0–100 `count` weight (genre source + confidence).

Base: `https://ws.audioscrobbler.com/2.0/` · `format=json` · `autocorrect=1`.

When M2 lands, these become the cache-seed / VCR-style fixtures for the Last.fm client,
so tests run offline and deterministically.
