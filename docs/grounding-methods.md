# Grounding & Derivation Methods — cross-axis overview

> **Purpose.** A single map of *how we decide what the "correct" value is* for each metadata
> axis TagMend mends — artist name, genre, and (proposed) album name + original year. The API
> mechanics live in [`musicbrainz-api.md`](./musicbrainz-api.md),
> [`LAST-FM-API-SPEC.md`](./LAST-FM-API-SPEC.md), and [`genre-tagging-spec.md`](./genre-tagging-spec.md);
> **this doc is the layer above that** — the grounding *strategy* per value, what's shipped vs.
> speculative, and where the gaps are. It exists so the album axis (and any future axis or
> manual-fix tool) reuses a consistent set of grounding primitives instead of reinventing them.
>
> Status: artist + genre describe **shipped** behavior (with `file:line` anchors). Album is
> **speculative** — a design surface, informed by live MusicBrainz testing and how Picard/beets
> solve the same problem. Decisions still open are collected at the end.

---

## The grounding primitives (shared vocabulary)

Every "what is the correct value" decision is built from a small set of reusable primitives.
Naming them once lets each axis pick a tier and lets us see coverage gaps at a glance. Roughly
ordered cheap→expensive and local→external:

1. **Sibling inference** — copy a value from *other tracks of the same album/folder* that already
   carry it. No network, high confidence, only fills blanks. (e.g. the *Crow* soundtrack: 7 tracks
   lack `date`, but their folder-mates carry `1996-09-24`.)
2. **Folder / path parsing** — extract `artist` / `album` / `year` from the directory name with a
   pattern (e.g. `(1970) Black Sabbath - Paranoid`). No network; depends on folder hygiene; the
   *patterns themselves* can be LLM-authored per observed folder format and cross-checked against
   known-good data. **Not used anywhere in the engine today** — a genuinely new primitive.
3. **External authority lookup** — query a music database for the canonical value:
   - **Last.fm `artist.getCorrection`** → canonical artist spelling (+ MBID). *Shipped.*
   - **Last.fm `artist.getTopTags` / `album.getTopTags`** → folksonomy genre tags. *Shipped.*
   - **MusicBrainz release-group** → canonical album title + **original** release year + MBIDs.
     *Proposed.* (Last.fm cannot do this — see Album §.)
4. **Controlled-vocabulary projection** — map a noisy external value onto a curated canonical set,
   dropping anything off-list. *Shipped for genre* (`genre_vocabulary.yml` + overlay).
5. **LLM arbitration / review (thin layer)** — pick among candidates, author folder-parse patterns,
   judge a fuzzy match, or exclude a bad suggestion. Sits *on top* of 1–4; never the sole source of
   truth. Today realized as the human/LLM review-and-exclude loop (`set_*_status`).

A given axis is "well-grounded" when it has at least one authoritative primitive (1–4) and the LLM
is only arbitrating, not guessing.

---

## ARTIST axis — *shipped*

**Goal:** correct a mis-spelled / variant artist name to its canonical form, across both `artist`
and `albumartist`, and capture the artist MBID.

**Grounding methods (in use):**

- **Last.fm `artist.getCorrection` (primary, sole authority).** One GET per *distinct string value*;
  returns `corrections.correction.artist.{name, mbid}`. The canonical name is *whatever Last.fm
  returns* — no second opinion. `lastfm.py:256–257`, parsed `lastfm.py:362–383`; orchestrated by
  `resolve_artists` `artists.py:154–216`.
- **MBID rides along (enrichment, not a lookup).** When a name change is made, the correction's
  `mbid` is written to `musicbrainz_artistid`. `artists.py:378–408`.

**Lookup key / grouping:** *none* — a flat, de-duplicated set of distinct `artist`+`albumartist`
strings; one API call per unique string, applied to every file that carries it. `_distinct_values`
`artists.py:264–287`.

**Guards:** empty, `feat`/`ft`/`featuring` (`_FEAT_RE`), compilation sentinels (`various artists`/
`various`/`va`), multi-value files skipped, sticky `manual` exclusion, empty-staging precondition.
`artists.py:57–86, 222–241, 373–375`.

**Gaps / potential additions:**
- **No fallback when Last.fm has no correction.** A genuine variant Last.fm doesn't know stays
  unchanged. *Folder parsing* (primitive 2) and *MB artist aliases* could be fallbacks.
- **No grouping** means a misspelled name is looked up in isolation; *sibling inference* could
  propagate a confident correction across an album's tracks.
- **Folder names are never consulted** — yet `(year) Artist - Album` folders encode the artist
  cleanly. The same folder-parse primitive proposed for album would ground artist too (and could
  surface *unknown* artists not yet on Last.fm).

---

## GENRE axis — *shipped*

**Goal:** assign a small set of canonical genres from community tags, never inventing a genre
outside a curated vocabulary.

**Grounding methods (in use):**

- **Last.fm `artist.getTopTags` (primary).** Always consulted; weighted folksonomy tags.
  `lastfm.py:154–170`.
- **Last.fm `album.getTopTags` (secondary, opt-in).** Only when `genre_use_album_tags` and the file
  has an `album`. `lastfm.py:172–178`, `genres.py:363–368`.
- **Controlled-vocabulary projection (the actual grounding).** Each tag is fold-keyed
  (`[^a-z0-9]+`→removed, lowercased) and matched against `genre_vocabulary.yml` (MusicBrainz-derived)
  + `genre_overlay.yml`; below-threshold or off-vocabulary tags are dropped; survivors merged by max
  weight, ordered, capped. `classify.py:242–271`. **A genre never written unless it's in the vocab.**

**Lookup key / grouping:** `(lookup_artist, lookup_album)` where `lookup_artist = albumartist[0]`
else `artist[0]`. All tracks of an album share one set of API calls + one resolved genre list.
`_identity` `genres.py:67–85`, grouping `genres.py:292–297`.

**Guards / status:** no-artist skip, field-aware done (`has_staged_change_for`/`has_auto_change_for`
on `genre`), sticky `manual`, and a **staleness-aware `no_match`** (re-opens when `artist`/`album`
changes). `genres.py:145–187`, `axis.py:104–124`.

**Gaps / potential additions:**
- **Vocabulary coverage** is the ceiling — a correct Last.fm tag missing from the vocab is silently
  dropped. Overlay edits are the lever.
- **No MB genre fallback.** MusicBrainz now has genres; could backstop artists Last.fm tags poorly.

---

## ALBUM axis — *speculative (design surface)*

**Goal (per the agreed scope):** populate **missing** `album` and **missing original year** —
*additive only, never overwrite an existing value*, and **never touch a reissue's edition date**.
We do **not** attempt to "correct" a present-but-wrong album name automatically (no reliable
authority to do so safely — see below).

### Why MusicBrainz, not Last.fm (verified live)

Last.fm is **not adequate** for albums: `album.getInfo` returns no reliable release year (the
legacy `releasedate` field is empty in current responses) and there is **no `album.getCorrection`**
(only `artist`/`track` have one). MusicBrainz is decisively the authority, and cleanly models the
exact thing that bit the *Black Sabbath* library:

- **Release group** → the *album concept*: canonical `title` + `first-release-date` = the
  **ORIGINAL** year. Live: *Paranoid* RG `first-release-date = 1970-09-18`.
- **Release** → a specific *edition*: its own `date`. Live: a *Paranoid* reissue dated **2000**;
  the RG spans 70 releases (1970…2009) all sharing `first-release-date = 1970-09-18`.
- **Release-group aliases** (`inc=aliases`) → the album analog to artist name-correction (live:
  *"Paranoid (Remastered 2009)"*, *"Solid Gold Black Sabbath"*).
- **No API key; 1 req/sec; descriptive `User-Agent` required.** Slots in beside the Last.fm client
  with the same cache/pace shape.

This reproduces the user's symptom exactly: Windows Explorer reads the **release** date (2000);
Navidrome reads the **release-group first-release-date** (1970). MB gives both, explicitly labeled.
See [`musicbrainz-api.md`](./musicbrainz-api.md) for endpoint detail.

### The empirical reality of *our* library (file autopsy)

- The *Black Sabbath* reissues are **already correctly dual-tagged** — `date`/`TDRC` = reissue
  (2000/2001…), `originaldate`/`TDOR` + `originalyear`/`TXXX:originalyear` = original (1970/1971…),
  with album MBIDs present. **They are a reference for "correct," not a fix target.**
- Folders encode ground truth: `(1970) Black Sabbath - Paranoid` → original year + album name.
- **Real blank cases exist organically** (no need to manufacture):
  - **8 files with no `album`** — `Maphra/Maphra - YouTube/` rips (have title/artist/date/genre).
  - **7 files with no `date`** — `Soundtracks/Crow, The City Of Angels/`; folder-mates carry
    `1996-09-24`, and the album name is a dash-variant (`The Crow- City Of Angels`).

### Grounding methods (proposed, tiered)

- **Original year, blank-fill → MusicBrainz release-group `first-release-date`.** Look up by
  artist + album, select the right RG (filter: `primary-type = Album`, no `Live`/`Compilation`
  secondary types, alias-aware title match), take the leading year. Write to
  `originaldate`/`originalyear` **only if blank** — *never* to `date`. This is exactly Picard's and
  beets' definition of "original date" and the de-facto tag standard (below). On the Black Sabbath
  files this is a **no-op** (already populated); on files missing original-year it gives Navidrome
  the right grouping without disturbing the edition date.
- **Album name, blank-fill (hard) — tiered, low-confidence:**
  1. *Sibling inference* — if folder-mates share an `album`, copy it (the safest fill).
  2. *Folder parsing* — extract from `(year) Artist - Album`-style names.
  3. *MB confirmation* — query by artist + track title to propose a release-group title.
  4. *LLM arbitration* — choose among 1–3 / decline. The YouTube-rip case shows the ceiling: no
     folder hint, no siblings, single tracks → often **leave blank** rather than guess.
- **Edition year (optional, later)** → the chosen `release.date`, if we ever want to *fill* a blank
  `date`. Out of scope for v1 (we only add original year).

### Tag mapping (follow Picard / beets — the de-facto standard)

| Concept | Vorbis/FLAC | ID3v2.4 | ID3v2.3 | We write it? |
|---|---|---|---|---|
| Edition date | `DATE` | `TDRC` | `TYER`/`TDAT` | **No** (never overwrite reissue info) |
| Original date | `ORIGINALDATE` | `TDOR` | `TORY` (year) | **Yes**, blank-fill |
| Original year | `ORIGINALYEAR` | (in TDOR) | — | **Yes**, blank-fill |
| Album title | `ALBUM` | `TALB` | `TALB` | **Yes**, blank-fill only |
| Album MBID | `MUSICBRAINZ_ALBUMID` / releasegroupid | TXXX | TXXX | **Yes** (enrichment) |

Both Picard and beets write the **original** year to `originaldate`/`originalyear`
(→ `TDOR`/`TORY`/`ORIGINALDATE`) and the **edition** year to `date` (→ `TDRC`). beets' `original_date`
config and Picard's "`originaldate` defaults to the earliest release in the release group" are the
same release-group-first-release behavior we'd adopt.

### How existing tools do it (answering "does anything work this way?")

- **MusicBrainz Picard** — matches via **AcoustID acoustic fingerprinting** (`fpcalc` → AcoustID →
  MB recording → release) *plus* metadata lookup; original date defaults to the release group's
  earliest release.
- **beets autotagger** — primarily **metadata-driven** MB matching (album+artist, distance-scored
  candidates); fingerprinting is the optional `chroma` plugin; `original_date` toggles writing the
  RG original date into the main `year` while always storing `original_*` separately.
- **Takeaway:** the mature tools confirm the model — MB release-group for canonical album + original
  year, `originaldate`/`originalyear` as the tag home. Fingerprinting (AcoustID) is how they ground
  *unknown* files with no usable tags; that's a heavier future primitive we have not committed to.

### Proposed two-pass shape (mirrors the genre status workflow)

- **Validation / flag pass** — group files by album identity; flag those with a blank `album` or
  blank original-year, and (optionally) those whose tags *disagree* with MB (e.g. a present album
  name that doesn't match any RG/alias) **for review, not auto-change**.
- **Update pass (blank-fill)** — for flagged blanks with a confident grounding, stage the additive
  change (`origin='auto'`), reusing the existing stage→diff→commit→revert spine and a new
  `file_year_status` axis (sticky `manual` exclusion; `no_match` when MB has nothing). Identity
  grouping = `(albumartist|artist, album-or-folder)`, like genre.

---

## Coverage gaps at a glance

| Axis | Authoritative source | Blank-fill source | Folder-parse used? | Sibling-infer used? | LLM role |
|---|---|---|---|---|---|
| **Artist** | Last.fm `getCorrection` ✅ | — (only corrects existing) | ❌ (gap) | ❌ (gap) | review/exclude |
| **Genre** | Last.fm tags + vocab ✅ | n/a | ❌ | ❌ | review/exclude |
| **Album name** | MB release-group ⬜ | siblings / folder / MB ⬜ | ⬜ proposed | ⬜ proposed | arbitrate/decline |
| **Original year** | MB `first-release-date` ⬜ | folder year / MB ⬜ | ⬜ proposed | ⬜ proposed | arbitrate |

The two **new shared primitives** (folder parsing, sibling inference) are first needed by album but
apply backward to artist (fallback when Last.fm has no correction; surfacing unknown artists) — a
good argument for building them as an axis-agnostic helper, not album-only code.

---

## Open decisions before building the album axis

1. **"Blank" for year = which tag?** Recommended: fill `originaldate`/`originalyear` when *those*
   are blank; never read/write `date`. (Most non-MB-tagged files lack original-year, so this
   populates widely while preserving every edition date.) Confirm.
2. **Managed-tag additions:** `album`, `originaldate`, `originalyear`, `musicbrainz_albumid`
   (+ `musicbrainz_releasegroupid`?). Confirm the set; verify mutagen-easy key support for
   `originalyear` across ID3/Vorbis/MP4.
   - **Resolved (mismatch-fix foundation, schema v9):** `MANAGED_TAGS` was widened to the
     full 18-field wrong-release "stamp" — `album`, `originaldate`, `musicbrainz_albumid`,
     and `musicbrainz_releasegroupid` are all managed now (write/revert coverage; the two
     MP4 freeform ids use the Picard atom names). `originalyear` stays **out** (no confirmed
     need). This is coverage, not a new workflow axis — the album axis still blank-fills only
     `originaldate`.
3. **Album-name blank-fill ambition:** siblings + folder only (safe), or also MB-by-track-title
   (riskier)? The YouTube rips may simply stay blank.
4. **Scope of the MB client now:** minimal (release-group search + lookup, 1 req/s, User-Agent,
   reuse `lastfm_cache`-style caching) vs. broader. Keep v1 minimal.
5. **Do we flag tag-vs-MB disagreements** (present-but-suspect album names) for review, or stay
   strictly blank-fill in v1? Recommended: blank-fill only in v1; add a review flag later.
6. **Folder-parsing primitive:** build it now (shared helper, LLM-authored patterns validated
   against known-good rows) or defer until after album blank-fill proves the spine? Recommended:
   defer; land MB blank-fill first.
