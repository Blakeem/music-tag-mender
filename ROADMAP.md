# TagMend — ROADMAP (forward-looking)

> Updated **2026-06-16**. This file lists only what **remains**. Everything shipped through
> **M4** has been removed so the list is purely forward-looking. `PLAN.md` is the design of
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
- **Schema v7. 21 MCP tools.** Both shipped axes (genre + artist) have working auto + manual
  exclusion pipelines with full revert. The review loop is: `resolve_*(dry_run)` → exclude what
  you don't want → `resolve_*` (real) → review the staged diff → `commit_tags`.

---

## Phase A — finish the metadata axes (the actual goal)

### A1. Generalize the per-axis machinery (refactor — first, and the workflow smoke test)
- [ ] Today genre and artist each carry a near-duplicate copy of the *same* status machinery.
      Adding album + song by copy-paste would make **four** copies. First, unify the pattern
      into **one parameterized `Axis` concept** so album/song slot in as data, not modules.
      **Additive, no DB rename** — `tags` stays `tags`, the `file_genre_status` /
      `file_artist_status` tables keep their names.
- [ ] The `Axis` must carry, per axis: `name`, `fields`, allowed user-statuses, workflow-statuses,
      and its **source-identity shape**. Preserve the two real asymmetries — **genre has a
      `no_match` state; artist does not**, and the `source_*` columns differ
      (`file_genre_status`: `source_artist`+`source_album`; `file_artist_status`:
      `source_artist`+`source_albumartist`).
- [ ] Collapse the duplicated surface behind the `Axis`:
      - `store.py`: `_<AXIS>_FIELDS`, `<AXIS>_WORKFLOW_STATUSES`, `<Axis>StatusRow`,
        `get/set/delete_<axis>_status`, `derived_<axis>_status`, `<axis>_status_counts`.
      - `genres.py`/`artists.py`: the `_select` skip predicate + `set/reset_<axis>_status`.
      - `library.py`: the `list_files(<axis>_status=…)` filter, the `get_file` status fields,
        the `library_stats` per-axis block.
      - `mcp_server.py`: the `set_/reset_<axis>_status` tools.
- [ ] **Behavior-preserving:** genre and artist outputs (statuses, counts, tool responses, revert)
      must be byte-for-byte identical after the refactor. No schema-version bump if no table
      changes. All four quality gates green.

### A2. Album axis — v1: original-year blank-fill via MusicBrainz (decided 2026-06-17)
Research (`docs/grounding-methods.md`) settled the approach: **MusicBrainz is the album authority** —
Last.fm has no reliable release year and **no `album.getCorrection`**. The **original** year lives in
`originaldate`/`originalyear` (MB release-group `first-release-date`, live-verified *Paranoid* = 1970),
distinct from a reissue's `date` (the edition year Windows shows). v1 is **additive blank-fill only**:
never overwrite an existing value, never touch `date`. Matches how Picard/beets handle original date.
- [ ] Minimal **MusicBrainz client** — release-group search + lookup; reuse the `lastfm_cache`-style
      cache + 1 req/s pacing + a descriptive `User-Agent` (no API key). See `docs/musicbrainz-api.md`.
- [ ] New **`file_album_status`** axis via the A1 `Axis` abstraction (grouped by album identity, like
      genre): `derived_album_status`, `list_files(album_status=…)`, `library_stats` `album` block,
      set/reset MCP tools, sticky `manual` + `no_match`.
- [ ] **`resolve_albums`** — fill `originaldate`/`originalyear` **where blank** from MB
      `first-release-date`; never touch `date`. Add `originaldate`, `originalyear`,
      `musicbrainz_albumid` (+ `musicbrainz_releasegroupid`?) to `MANAGED_TAGS` (verify mutagen-easy
      `originalyear` key support across formats). No-op on already-tagged files (the Black Sabbath set).
- **Follow-ups (separate features):** album-**name** blank-fill (sibling inference → folder parse →
  MB-by-track) and the shared **folder-parsing primitive** (reusable for artist + unknown-artist
  discovery). See `docs/grounding-methods.md` for the tiered grounding design.

### A3. Song axis (title + track number)
- [ ] New axis: add `title` and `tracknumber` (+ `musicbrainz_trackid`) to `MANAGED_TAGS`;
      `file_song_status`, `derived_song_status`, `resolve_songs`, filter/stats/tools — again via
      the `Axis` abstraction. Track-list verification (does the album's track set line up?) is the
      hardest correctness question; spec it in `PLAN.md` first.

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
- [ ] Drive genre + artist + album + song over the full ~130 GB / 256-artist library via MCP,
      chunked with `limit`, reviewing staged diffs before each commit. **This is the goal:** clean
      metadata so Navidrome tag-search works. Followed by a deliberate **review/testing break**
      before any filesystem work begins.

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
