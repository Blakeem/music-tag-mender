# TagMend — ROADMAP (forward-looking)

> Updated **2026-06-26**. This file lists only what **remains**. Everything shipped through
> **A2** has been removed so the list is purely forward-looking. `PLAN.md` is the design of
> record; this is the punch-list of what's left.
>
> **Direction (2026-06-16):** finish the **metadata** mission first — it's the whole point of
> the app ("mending tags"). Filesystem moves/renames are deferred until *after* all metadata
> axes are clean and a full review/testing pass is done. Navidrome scans by **metadata**, so
> folders are cosmetic for the primary use case.

## The mental model (read this before touching names)

There are **three** distinct levels. Conflating them is the main naming hazard:

- **Identity — the `files` table.** One row per audio file (`folder`, `filename`, `size`,
  `mtime`). It is the durable anchor (`files.id`); **every** revision hangs off it. It is *not*
  a tracked-change domain.
- **Domain — the revertible-change seam (`commits.RevisionDomain`).** Exactly two:
  - **`tags`** — `TagDomain` (shipped). Writes metadata into `tag_revisions`. **Keep this name.**
    It is generic on purpose; genre/artist/album/song all live *inside* it.
  - **`paths`** — `PathDomain` (stub only, M6, deferred). Would write `path_revisions`.
- **Axis — a field-group *within* the `tags` domain.** Each axis has its own resolve step,
  sticky status table, and field-aware `done`/`staged` (via `json_extract` on the committed
  `diff`). Shipped: **genre** (`genre`), **artist** (`artist`/`albumartist`). Planned:
  **album** (`album` + year), **song** (`title` + track number).

**Consequence:** adding album/song is **not** a rename and **not** a new domain — it is two more
*axes* on the existing `tags` domain. The schema change is purely additive (a new
`file_<axis>_status` table + a couple of new `MANAGED_TAGS` keys), exactly like artist (v7) was.
(Note: `MANAGED_TAGS` membership is just write/revert **coverage** and is independent of having a
workflow axis — the mismatch-fix foundation (schema v9) widened it to the full 18-field identity
"stamp", including `title`/`tracknumber`, without adding a song axis.)

> The old "review-one-at-a-time" tools (`approve_mapping`, `commit_artist`,
> `list_pending_review`, `get_artist_candidate`, `review_stats`) are **not being built** — they
> were superseded by the dry-run + exclusion + staged-diff model.

---

## Where we are (shipped & tested)

- **M0–M1** readiness + read path. **M2** genre **axis** (Last.fm → vocabulary → stage).
  **M3 / M3.5** git-like stage→commit→history→revert core, `revert_commit`, genre-status
  visibility.
- **M4 — artist **axis** + review state (DONE).** `resolve_artists`, sticky per-file artist
  exclusion, per-axis visibility (`list_files(artist_status=…)`, `library_stats` `artist`
  block), **field-aware** `done`/`staged` so genre and artist read independently.
- **A1 — Axis abstraction refactor (DONE).** One parameterized `Axis` (`engine/axis.py`):
  `GENRE_AXIS`/`ARTIST_AXIS`/`ALBUM_AXIS` are data, not copied modules. The two asymmetries are
  preserved in each axis's `decision_blocks` predicate (genre/album have `no_match`+staleness;
  artist's `manual` is sticky) and `source_columns`. Generic `get/set/delete/derived_status`.
- **A2 — Album axis (DONE).** Minimal MusicBrainz release-group client (`engine/musicbrainz.py`,
  cached in `musicbrainz_cache`, 1 req/s, descriptive User-Agent, no key) + `resolve_albums`
  (`engine/albums.py`): blank-fill `originaldate` from MB `first-release-date` **only where
  blank**, never overwriting, never touching `date`. Sticky `manual` + engine-owned `no_match`,
  `list_albums`, `list_files(album_status=…)`, `library_stats` `album` block, set/reset tools.
  **Scope note:** the album axis *resolve* step still writes **only `originaldate`** (blank-fill).
  It does not itself write the MusicBrainz IDs. (`MANAGED_TAGS` was later widened by the
  mismatch-fix foundation — see below — so `musicbrainz_albumid`/`releasegroupid` are now
  write/revert-covered, but the album resolve flow does not touch them.)
- **Schema v9. 25 MCP tools.** Three shipped axes (genre + artist + album) have working auto +
  manual exclusion pipelines with full revert. The review loop is: `resolve_*(dry_run)` → exclude
  what you don't want → `resolve_*` (real) → review the staged diff → `commit_tags`.
- **NOTE (2026-06-26):** the A1 + A2 work above is **complete and all four gates pass**
  (392 tests, ruff, mypy) but is currently **uncommitted** in the working tree — commit it before
  starting A3.

---

## Phase A — finish the metadata axes (the actual goal)

> **A1 (Axis abstraction) and A2 (Album axis) are DONE** — see "Where we are" above.
> A3 (song axis) is the only remaining metadata axis; A4 is a small readiness add-on.
>
> **Carried-forward follow-ups from A2 (separate, lower-priority features):** album-**name**
> blank-fill (sibling inference → folder parse → MB-by-track) and the shared **folder-parsing
> primitive** (reusable for artist + unknown-artist discovery). See `docs/grounding-methods.md`
> for the tiered grounding design. Also: A2's album *resolve* step writes only `originaldate`;
> `MANAGED_TAGS` itself was widened by the mismatch-fix foundation (schema v9) to the full
> 18-field wrong-release identity "stamp" — the six MusicBrainz IDs + title/album/date/track/
> disc + sort names — for write/revert **coverage** (no new workflow axis). `originalyear`
> stays out.

### A3. Song axis (title + track number) — DROPPED FROM PHASE A; deferred past the CLI (2026-06-26)
The metadata axes the library actually needs — **genre, artist, album** — are all shipped. A live
audit of the real 720-file library (2026-06-26) found **0 missing/blank titles and 0 missing/blank
track numbers**; no generic `"Track NN"` or numeric placeholders. The only version of a song axis that
resembles the album code (blank-fill a missing title by track position) would be a **no-op** here, and
the version that adds real value — **correcting a *wrong* title** — can't be done from text metadata at
all (the tags lie; there's nothing to compare against). That requires **acoustic fingerprinting**, a
new capability class. So the song axis is **deferred to after the CLI surface** — see *Deferred* below.

### A4. Live Last.fm readiness check (small — slot in anywhere)
- [ ] Add a Last.fm connectivity ping to `doctor` (one cheap `artist.getTopTags` call) so a long
      run fails fast on a bad key / no network. `doctor.py` checks settings, music folder, and
      ledger only — not Last.fm.

---

## Phase B — review & full-library run (the primary deliverable, then a pause)

### B1. Pre-run safety
- [x] Round-trip on real audio (scan → stage → commit → verify on disk → `revert_commit`) proven
      live for both genre and artist, across formats.
- [ ] **Recommend a filesystem-level copy of `music/` before the first full run** — cheap
      insurance beyond the managed-tag v0 baseline. *(User action, not code.)*

### B2. First full-library run over all metadata axes
- [ ] Drive genre + artist + album over the full ~130 GB / 256-artist library via MCP,
      chunked with `limit`, reviewing staged diffs before each commit. **This is the goal:** clean
      metadata so Navidrome tag-search works. Followed by a deliberate **review/testing break**
      before any filesystem work begins. *(Song/title is out of scope — see A3; titles + track
      numbers are already complete library-wide.)*

---

## Deferred until after Phase B

- **M6 — Organize (opt-in moves/renames):** `paths` is the second `RevisionDomain`; `moves.py`
  is a stub, `path_revisions` DDL ships. **Kept, explicitly deferred** — low priority for a
  Navidrome library, but the design (revertible per-file moves, the Miami Nights demo) stays on
  the books. Unblocked by M4's canonical artist names + the album/song axes. Three open seam
  questions remain (intra-batch move ordering, collision policy §15, folder-rename atomicity) —
  see `moves.py` and PLAN.md §18.
- **CLI surface** — deliberately **last**; all tools are MCP-first. Eventual pass exposes the
  axis verbs (`resolve-*`, `set-*-status`, `diff`/`commit`, `revert`/`revert-commit`,
  `list --*-status …`).
- **Song axis (title) via acoustic fingerprinting — *after* the CLI surface, add only if needed.**
  Sequenced here deliberately (decision 2026-06-26): the current library needs none of it (0 missing
  titles / track numbers across 720 files), and it's a bigger lift than the text-lookup axes. Unlike
  genre/artist (Last.fm) and album (MusicBrainz text search), a *wrong* title can't be detected from
  metadata — it needs the audio itself. The approach mirrors **MusicBrainz Picard's "Scan"**:
  - **fpcalc / Chromaprint** (the open-source, **LGPL-2.1+** fingerprinting tool; cross-platform
    prebuilt binaries for Windows/macOS/Linux, statically linked with FFmpeg so one self-contained
    exe decodes mp3/flac/m4a/ogg). Called as a **subprocess**, so the LGPL imposes no obligations on
    our code. Likely via the MIT-licensed **`pyacoustid`** wrapper (the beets stack).
  - **AcoustID web service** (free application **API key**, like `lastfm_api_key`): submit
    `fpcalc`'s fingerprint+duration → get MusicBrainz **recording IDs** → canonical title/track.
  - New axis via the existing `Axis` abstraction: `title` (+ `tracknumber`?, `musicbrainz_recordingid`?)
    in `MANAGED_TAGS`, `file_song_status`, `resolve_songs`, filter/stats/tools. **Spec the
    track/recording reconciliation in `PLAN.md` first** (a fingerprint can match multiple recordings —
    score and pick; track-set line-up is the hard correctness question).
  - **New deps it introduces** that the text axes don't: an **external binary** on the user's PATH
    (can't pure-pip it; needs a `doctor` check) + a **second API key**. Re-verify current fpcalc
    release + AcoustID API details at build time rather than trusting today's notes.
- **M5 — Polish:** genre vocabulary tuning, album-level genre override, README, packaging
  (`uv tool install` / `uvx tagmend mcp` / `pipx`).

---

## Low-priority / defensive (no blockers today)

- [ ] **No-identity worklist:** files with no `artist` AND no `albumartist` are silently skipped
      (`skipped_no_artist`) and bucket as plain `pending`. Give them a first-class state so they're
      visible. *Zero such files exist in the library today.*
- [ ] **Bulk manual-stage convenience** for a genuine `no_match` (artist truly not on Last.fm).
      The per-file escape hatch already exists (`stage_tags` + `commit_tags`); a bulk
      artist/folder-scoped manual stage would be ergonomics. *Defer until a real `no_match` appears.*

## Known limitations — deliberately deferred (revisit if they bite)

- **getCorrection maps junk album-artist labels to the MusicBrainz `[unknown]` placeholder.**
  e.g. `Original Soundtrack` → `[unknown]` (+MBID `125ec42a…`); affects ~16 soundtrack files.
  Left **as-is** per decision 2026-06-16 ("adjust if it becomes an issue"). If it does, the
  targeted fix is a parser guard that rejects corrections resolving to a MB special-purpose
  placeholder name/MBID (`[unknown]`, `[no artist]`) → treat as `no_correction`. Compilation rows
  are otherwise protected today by the `various artists`/`various`/`va` sentinel skip.
