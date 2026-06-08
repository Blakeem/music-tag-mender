# Genre Tagging Spec — Last.fm tags → controlled genre vocabulary

**Status:** Draft / agreed design (2026-06-08). Supersedes the small hand-curated
allow-list + weight-threshold approach sketched in `PLAN.md §9`. Implementation pending
(`engine/lastfm.py`, `engine/classify.py` are stubs).

**Audience:** humans and LLMs working on TagMend's genre path. This is the authoritative
design for *how genres are sourced, normalized, deduplicated, and written*. Artist/album
**name** resolution and verification flags are a separate, later phase — captured as open
questions in §11, not designed here.

---

## 1. Goal & scope

Re-derive clean, consistent **genre** tags for each file from **Last.fm community tags**,
filtered and spelled against the **MusicBrainz controlled genre vocabulary**, written as a
multi-value `genre` tag, fully revertible through the existing commit/staging engine.

**In scope:** genre sourcing (artist + album tags), the vocabulary file format, the
fold/alias matching model, the merge/dedup/threshold pipeline, the write format, settings.

**Out of scope (here):** artist-name normalization (`artist.getCorrection`), the
auto-vs-review workflow, and "verified / ignore" flags for artists & albums — see §11.

---

## 2. What the Last.fm API actually gives us (verified 2026-06-08)

All confirmed live against our free key. Free key only; no batch endpoint; ~5 req/sec IP
limit; we pace at 1 req/sec and cache every response in `lastfm_cache`.

| Endpoint | Use | Weighted? | Notes |
|---|---|---|---|
| `artist.getTopTags` | genre source | ✅ `count` 0–100, **normalized** (top tag = 100) | works on obscure artists |
| `album.getTopTags` | genre source | ✅ same scale | **distinct, valuable** — see §2.1 |
| `album.getInfo.tags` | — | ❌ ≤5 tags, no weights | strictly worse; **do not use** |
| `artist.getCorrection` | name (Phase 2) | — | not used for genres |

**`count` is a normalized 0–100 weight, not a raw vote count** — the top tag is always
100 and the rest are relative. A `count` of 1 = "1% as strongly tagged as the top tag"
(the weak tail), which is why a single weight threshold cleanly handles both the
"drop 1-vote" and "drop bottom 1%" intuitions.

### 2.1 Why album tags matter (not just a duplicate source)

For mixed-catalog artists, album tags capture the actual sound of *that record* and
diverge from the artist profile — so we **merge both**, we don't pick one:

```
Daft Punk (artist):              electronic(100) house(63) dance(36) techno(26) ...
  └ Discovery (album):           electronic(100) house(96) french house(10) ...
  └ Random Access Memories:      electronic(87) DISCO(43) FUNK(32) house(12) ...   ← disco/funk
```

Only the album-level tags know RAM is disco/funk. There is **no fallback and no
precedence** — artist and album top tags are unioned.

### 2.2 The noise problem (why a controlled vocabulary is mandatory)

Raw Last.fm tags are folksonomy, not genres. Album tags are noisier than artist tags —
**year tags are frequently the #1 weighted tag**: RAM's top tag is `2013(100)`, OK
Computer has `1997(35)`, Discovery `2001(33)`. Also mood/list junk (`favourite albums`,
`Masterpiece`), decades (`90s`), and artist-name-as-tag (`radiohead`). A naive "take the
top tag" would write **"2013"** as a genre. The vocabulary allow-list is what drops all of
this: a tag is kept **iff it matches the controlled list**, not because of its weight.

### 2.3 Why Last.fm, not MusicBrainz, supplies the genre *tags* (decided)

MusicBrainz supplies the **vocabulary** (§4); it does **not** supply the per-file genre
tags. We evaluated using MusicBrainz genres (`inc=genres`, which returns clean, weighted,
already-canonical genres) and rejected it as the tag *source* because its coverage is too
sparse for an obscure library. Head-to-head on the user's actual music (2026-06-08):

| Artist | MusicBrainz genres | Last.fm (after vocab filter) |
|---|---|---|
| Thermostatic | **none** | synth-pop, electronic, bitpop, electropop, chiptune |
| Ours | **none** | alternative rock, rock, indie rock |
| Download | industrial(3), electronic(2) | industrial, idm, experimental, electronic, ebm … |
| Therapy? | alt rock(5), alt metal(4) … | alternative rock, rock, hard rock, metal, alt metal |

Two of four had **zero** MusicBrainz genres but rich Last.fm tags. Last.fm's folksonomy is
far denser for obscure music (more taggers), and once filtered through the MusicBrainz
vocabulary it is also clean. **Runtime genre sourcing = Last.fm only + the bundled MB
vocabulary file (no runtime MusicBrainz calls).** MusicBrainz's real value is *identity /
disambiguation*, handled in Phase 2 (§11).

---

## 3. Core model: fold-key vs. canonical spelling

Two distinct values per genre string. Conflating them is the main source of bugs.

| | **Fold-key** (match/dedup only) | **Canonical spelling** (what we write) |
|---|---|---|
| Purpose | collapse spelling/spacing/punct variants so they compare equal | the single standard name on disk |
| Derivation | `lower()` then strip everything non-`[a-z0-9]` | the MusicBrainz genre name (lowercase, as-is) |
| Human-readable? | no (`rhythmandblues`) | yes (`r&b`) |
| Stored on disk? | **no** — derived in memory at load | yes — it *is* the vocabulary `name` |

**Fold function (canonical definition):**

```python
import re
def fold(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())
```

**Matching:** fold the incoming Last.fm tag, look the fold-key up in the in-memory
`fold-key → name` index, and **write the vocabulary's `name`** (never the Last.fm
spelling). Real example: Last.fm returns `synthpop` and `synth pop`; both fold to
`synthpop`; the MusicBrainz canonical is `synth-pop`; we write **`synth-pop`**.

**Casing decision:** we write the MusicBrainz lowercase name verbatim (`synth-pop`,
`r&b`, `alternative rock`). No Title-case transform — both authoritative lists are
lowercase, and Title-casing has too many ambiguous edge cases (`r&b` → `R&B`?
`italo-disco` → ?). Navidrome groups genres case-insensitively, so lowercase displays
fine.

### 3.1 Why folding alone is not enough — aliases are required

Folding catches spacing/punctuation/case variants. It does **not** catch abbreviations or
worded synonyms. Proven with r&b's real MusicBrainz aliases:

```
r&b               -> fold "rb"
r'n'b, rnb        -> fold "rnb"            ← does NOT fold to "rb"
rhythm and blues  -> fold "rhythmandblues" ← does NOT fold to "rb"
```

`rnb` (a common Last.fm spelling) would be **dropped** without an explicit alias mapping
it to `r&b`. So the vocabulary needs both: folding (free, handles variants) **and** an
alias list (handles synonyms folding can't reach).

---

## 4. The genre vocabulary file

One bundled file, `data/genre_vocabulary.yml`, is the single source of truth. Loaded once
at startup into the `fold-key → name` index. Human- and AI-editable.

### 4.1 Schema

```yaml
# data/genre_vocabulary.yml
version: 1
source: musicbrainz            # provenance of the `name` set
generated_at: 2026-06-08
genres:
  - name: r&b                  # canonical spelling WRITTEN to files (MB lowercase name)
    mbid: 31be54b2-4d0c-42df-aa44-c496c7b4c3c3
    aliases:                   # readable synonyms whose fold-key differs from fold(name)
      - rnb                    # fold "rnb"            (folding alone wouldn't reach r&b)
      - rhythm and blues       # fold "rhythmandblues"
      - rhythm & blues         # fold "rhythmblues"
  - name: synth-pop
    mbid: 53951612-9c98-4d6f-...
    aliases: []                # "synthpop"/"synth pop" fold to fold(name); folding handles them
```

- `name` — **required.** The canonical spelling written to files, and the match target.
- `mbid` — optional but recommended (provenance, dedup, refresh key, future MBID linkage).
- `aliases` — optional list of **readable** synonyms. Stored as readable strings, not
  fold-keys (see §4.3).

### 4.2 Alias deduplication (the "strip duplicates / smaller file" rule)

We do **not** store every raw MusicBrainz alias. At **build time**, an alias is kept only
if it contributes a **new fold-key**:

1. Drop any alias where `fold(alias) == fold(name)` — folding already matches it (e.g.
   `synthpop`, `synth pop` under `synth-pop`).
2. Among the rest, dedup by fold-key, keeping the **first readable** representative
   (e.g. r&b's `r'n'b` and `rnb` both fold to `rnb` → keep one).

Concretely, r&b's 7 raw strings (name + 6 MB aliases) collapse to 5 fold-keys; after also
dropping `fold==fold(name)`, only ~3–4 readable aliases need storing. The file stays small
**and** human-editable.

### 4.3 Why readable aliases, not stored fold-keys

The user asked whether to store fold-keys directly (`rhythmandblues`) to shrink the file.
We store **readable** aliases and fold them at load instead, because:

- **Editability** — the AI/human maintains aliases; `rhythm and blues` edits naturally,
  `rhythmandblues` does not. This is the whole point of "let the AI manage aliases."
- **Provenance/debug** — we can report "Last.fm tag `rhythm & blues` matched `r&b`."
- **Lossless** — fold is one-way; storing only the key throws away the human spelling.
- **Size is a non-issue** — 2,156 genres with a handful of aliases each is a small file.

The fold-key dedup of §4.2 already delivers the size benefit without these costs.

### 4.4 Fold-key collisions (safety requirement)

Because many strings fold to the same key, different genres can claim the same fold-key.
Left unhandled this would silently mis-map a tag, so the build/refresh step **must detect
every collision and resolve it deterministically — never silent last-writer-wins**. Two
kinds occur, and real MusicBrainz data contains both:

- **Name vs. name** — two genre *names* fold to the same key (e.g. `hyper techno` and
  `hypertechno` are both real MB genres). These are **merged** to one canonical spelling,
  not treated as errors. The winner is the most readable form, chosen deterministically:
  most separator characters first (`hyper techno` over `hypertechno`), then shortest, then
  alphabetical. The losers' aliases are pooled into the winner, and the merge is reported.
  *(Originally specced as a fatal error; the first real refresh proved that wrong — MB ships
  ~dozens of such near-duplicate spellings.)*
- **Alias vs. anything** — an alias folds to a key already owned by another genre (via its
  name or an earlier alias). The alias is **skipped and reported** (names always win over
  aliases; earlier aliases win over later ones, in a deterministic name-sorted order).

Every merge/skip is emitted as a warning at build time so the outcome is auditable. At
load time `classify.py` rebuilds the same `fold-key → name` index and asserts it is
single-valued (the file is already collision-free by construction).

### 4.5 The user/AI overlay (`genre_overlay.yml`)

`genre_vocabulary.yml` is GENERATED and overwritten on every MusicBrainz refresh, so it is
**never** hand-edited. A second bundled file, `genre_overlay.yml`, is the **durable,
editable layer** that survives refreshes. `classify.py` loads both and **merges the
overlay over the vocabulary in memory** (the build script does not bake it in — they stay
separate so a refresh can't clobber user data). Two uses:

1. **Genres MusicBrainz omits** but are common in the wild. MusicBrainz deliberately has
   no bare `alternative` or `indie` (only qualified forms like `alternative rock`,
   `indie pop`), so the filter would drop those very common tags. The overlay adds them.
2. **Extra spellings** for an existing genre — `name` is set to the MusicBrainz canonical
   spelling and `aliases` lists the variants (e.g. `chiptune` ← `8-bit`, `8bit`).

Same fold-key + collision rules as §4.2/§4.4. An overlay entry whose `name` folds to an
existing genre **adds its aliases**; otherwise it becomes a **new genre** (no MBID).
Overlay/vocabulary collisions are reported, never silent. This file is what the **LLM
grows** from Last.fm tags that match nothing (§5.1) — the primary place the alias/genre
vocabulary improves over time.

---

## 5. Building & refreshing the vocabulary

A maintenance command (`tagmend refresh-vocab`, not part of the runtime tagging path)
regenerates `genre_vocabulary.yml`. The **runtime never calls MusicBrainz.**

| Data | Source | Method |
|---|---|---|
| Genre **names** + MBIDs | MusicBrainz `ws/2/genre/all?fmt=json` | paginated (limit 100; ~22 pages); 2,156 genres; **CC0** |
| Genre **aliases** | **not in the WS2 API** (verified — `inc=aliases` is ignored for genres) | see §5.1 |

### 5.1 Alias sourcing — the constraint and the plan

**Verified 2026-06-08:** the MusicBrainz WS2 API does *not* expose genre aliases.
`ws/2/genre/<mbid>?inc=aliases` returns only `id`/`name`/`disambiguation`, even for r&b
which has 6 aliases on the website. Aliases live only on the website and in the DB dump.

So aliases are **not** auto-pulled at refresh. Instead, two tiers:

1. **Curated seed (ship now).** A hand- + AI-authored alias set covering the high-value
   cases folding misses — abbreviations and worded synonyms (`rnb→r&b`, `dnb→drum and
   bass`, `hip-hop` variants, etc.). Small, readable, maintained in the YAML.
2. **AI-grown (ongoing).** When the tagging run encounters a Last.fm tag that folds to no
   known key, it goes to an **unmatched bucket** (ties into §11). The LLM decides: add it
   as an alias of an existing genre, add a new genre, or ignore as junk. This is the
   primary mechanism by which the vocabulary improves over time.
3. **Optional bulk import (future, documented, not required).** A one-time offline script
   could seed the full MusicBrainz alias set by parsing the MB **database dump**
   (`genre_alias` table) — robust, unlike scraping 2,155 website pages. Flagged as an
   enhancement so we don't block the feature on it.

---

## 6. Genre resolution pipeline

Per file (artist + album come from the file's existing tags):

```
1. Fetch    artist.getTopTags(artist)         ── cache in lastfm_cache
            album.getTopTags(artist, album)   ── cache in lastfm_cache (if enabled & album present)
            → list of (tag, weight 1..100) per source

2. Map      for each tag: name = index.get(fold(tag))   ── drop if no match (kills 2013, Masterpiece, ...)

3. Threshold drop tags with weight < genre_min_weight   ── per source (kills the count==1 weak tail)

4. Merge    union artist ∪ album
            merged_weight[name] = max(weight across the sources it appeared in)

5. Order    sort by merged_weight desc, then name asc (stable)

6. Cap      if genre_max_count is set, keep the top N

7. Write    genre = [name, name, ...]  ── multi-value tag, via the staging/commit engine
```

Notes:
- **Merge weight = max**, not sum — each source is independently normalized to 100, so max
  means "strongest evidence from either source" and never exceeds 100. Used only for
  ordering and the cap; never written to disk.
- Steps 1–2 are cached per `(method, artist[, album])`, so re-runs are free.
- The whole result is staged, diffed, and committed through the **existing** revertible
  engine — genre writing is just another `MANAGED_TAGS` change. No new write path.

### 6.1 Worked example — Daft Punk / Random Access Memories

```
artist survivors (min_weight=2): electronic 100, house 63, dance 36, techno 26, electronica 11, electro 2
album  survivors (min_weight=2): electronic 87,  disco 43, funk 32, house 12, dance 5
                                  (2013 dropped: not in vocab; pop(1) dropped: below min_weight)

merged (max):  electronic 100 · house 63 · disco 43 · dance 36 · funk 32 · techno 26 · electronica 11 · electro 2
written genre: electronic; house; disco; dance; funk; techno; electronica; electro
with genre_max_count=5:  electronic; house; disco; dance; funk
```

This also shows why a `genre_max_count` cap is useful — eight genres may be more than a
user wants on one file.

---

## 7. Settings

Added to `Settings` / `settings.json` (read via `config.load_settings()`):

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `genre_min_weight` | int | `2` | Drop Last.fm tags below this normalized weight. `2` removes the `count==1` tail ("bottom 1%" / 1-vote). `0` keeps all matches. |
| `genre_max_count` | int \| null | `null` | Cap on genres written per file. `null` = unlimited. Set small for "top genres only". |
| `genre_use_album_tags` | bool | `true` | Include `album.getTopTags` in the merge. |

All three are tunable later; defaults are conservative. Real-world accuracy on the user's
obscure library is unknown until tested, so these knobs are deliberately exposed rather
than hard-coded (the dropout threshold especially may want raising after testing).

---

## 8. File-format & Navidrome considerations

- **Multi-value `genre`** is written through the existing `tags.write_managed_tags`
  (`genre ∈ MANAGED_TAGS`). ID3v2.4 (MP3) and Vorbis comments (FLAC/OGG) hold multiple
  genre values cleanly; MP4/`.m4a` (`©gen`) is weaker — verify per-format multi-value
  round-trips during implementation.
- **Navidrome** (the user's target) reads multi-valued ID3v2.4/Vorbis tags (with
  `Scanner.Extractor=taglib`) or splits a single field on its default separators
  **`;` `/` `,`**. Implication: **never emit a genre string containing `;`, `/`, or `,`**
  or Navidrome will split it wrongly. (No MusicBrainz genre in our probes contains these.)
- Navidrome consumes only the genre **strings**; Last.fm weights are never stored and are
  invisible to it.

---

## 9. How this changes `PLAN.md`

`PLAN.md §9` describes a *small hand-curated allow-list* + *weight thresholds for
auto/review*. This spec replaces that with a *comprehensive 2,156-entry MusicBrainz
vocabulary* where:
- the **vocabulary** does the filtering (weights no longer gate noise);
- **weights** are demoted to ordering + an optional tail cutoff;
- the **LLM's value** shifts from "curate the whole vocabulary" to "resolve the unmatched
  bucket and grow the alias list" (§5.1, §11).

`PLAN.md §8–9` should be updated to point here. (Follow-up, not done in this doc.)

---

## 10. Open implementation checklist

- [ ] `refresh-vocab` command: pull `genre/all` (paginated) → write `name`/`mbid`; merge
      curated aliases; run §4.2 dedup + §4.4 collision check.
- [ ] `data/genre_vocabulary.yml` v1: 2,156 names + seed aliases.
- [ ] `classify.py`: load vocab → `fold-key → name` index (+ collision assertion);
      `resolve_genres(artist_tags, album_tags, settings) -> ordered list[name]`.
- [ ] `lastfm.py`: `get_top_tags(artist)` / `get_top_tags(artist, album)` with
      `lastfm_cache` + 1 req/sec pacing; error-6 (not found) → empty list.
- [ ] Settings: `genre_min_weight`, `genre_max_count`, `genre_use_album_tags`.
- [ ] Wire resolved genres into the existing stage → commit path; tests across all four
      formats using `make_track`.

---

## 11. Phase 2 — artist & album resolution + verification (MBID-anchored)

**Being planned now (separate plan doc).** The genre path above queries Last.fm by the
file's `artist`/`album` strings. That works for the common case but has a real failure
mode proven on the user's library: **homonyms**. A MusicBrainz search for `Ours` returns 3
distinct artists; `Download` returns 3 (Canadian industrial, a US metal band, a trance
producer). Last.fm has **no disambiguation** — query by name and you silently get the
*most popular* artist's tags, which may be the wrong act.

This is where **MusicBrainz earns its place — for identity, not genres** — and the bridge
is clean:

- **`artist.getTopTags` (and album) accept an `mbid`** (verified). So once an artist is
  resolved to a MusicBrainz MBID, we query Last.fm **by MBID**, eliminating name ambiguity.
- The LLM **never types a corrected name** — it supplies/confirms an **MBID** (or another
  stable id), and the engine dereferences it to the canonical name. Corrections become
  *verifiable* (resolve the MBID and see what it is) and *deterministic* (no typos).
- Direction for the flag model: a single **status** per artist/album
  (`unresolved → matched → verified → ignored`) plus the `mbid` link, over the
  already-planned `artist_cache.status`/`mbid` columns (PLAN §7.2), with explicit
  `flag`/`view`/`modify` MCP tools. `ignored` parks entities genuinely absent from the
  databases so they stop resurfacing.

Open questions for the plan doc: album identity key (`(artist, album)` vs release-group
MBID); how `ignored` interacts with re-scans; how the unmatched-tag bucket (§5.1) and the
unmatched-artist bucket share one review surface.

Decisions in §2–§7 are settled.
