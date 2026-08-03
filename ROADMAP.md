# TagMend — ROADMAP (forward-looking)

> Updated **2026-08-02**. This file lists only what **remains** — everything shipped has been
> removed (through the album-gaps detector: schema v12, 31 MCP tools; see `CLAUDE.md` for the
> shipped-state summary and `PLAN.md` for the design of record).
>
> **Direction (updated 2026-08-02):** finish the **metadata** mission (Phase B), then the
> **path-canonicalization** mission (Phase C: prove path↔tag coherence for every path-encoded
> field, then pattern-driven renames/moves, then promote). Tags are the source of truth;
> paths become derived output. The **CLI surface** stays deliberately last (reaffirmed
> 2026-07-05).

---

## Phase B — the primary deliverable (in order)

> **`music/` is already the safety copy** (confirmed 2026-07-05): the working folder is a full
> **135 GB copy** of the real library — the original sits untouched elsewhere and can be re-copied
> anytime. All live testing, scanning, and fix work happens on the copy; the close of Phase B is
> promoting the mended copy over the actual library once everything is verified (B3).

### B0. Live mismatch fix pass (next up)
- [ ] Drive the fix flow over the **19 flagged folders / 144 files**: grouped detect → research
      the correct release per folder → `stage_tags_batch` → review `diff_tags` →
      `commit_tags(path=folder)` (one revertible commit per release) → `reopen_axes`.
      **Do this BEFORE the full resolve run (B2)** — identity fixes re-pend derived genre/year,
      so fixing identity first avoids resolving axes against wrong artists.
- Known per-folder routing from live testing (2026-07-04):
  - [ ] **Skrillex/Gypsyhook ("Sonny", 8 files):** Last.fm `getCorrection("Sonny")` returns
        *already canonical* — the alias will NOT be fixed by `resolve_artists`. Fix flow
        (research the Gypsyhook EP identity) or `legit_ignore` if the Sonny credit is wanted.
  - [ ] **Soundtracks folders (Crow: City of Angels, Freddy vs. Jason — 23 files):** deeper than
        the container false positive — files carry the *score* release's titles/tracknumbers/
        MB-IDs over soundtrack audio (e.g. filename "Hole - Gold Dust Woman" stamped
        `title="La Masquera"`). Needs per-file re-identity via the fix flow, not `legit_ignore`.
        (Dispositions set during testing were reset — both folders are `pending` again.)
  - [ ] **Tool [Discography] (2 Alice In Chains files):** genuinely misfiled →
        `misfiled_deferred` (never a tag write; the files move when M6 exists).

### B1. Live album-gap fill pass (92 blank-`album` files, measured 2026-07-05)
- [ ] Drive `detect_album_gaps` over the library: bulk-stage the `green` sibling proposals,
      confirm each `confirm`/`review` proposal per folder, then the usual
      `stage_tags_batch → diff_tags → commit_tags → reopen_axes` spine (one commit per folder).
      Do this before/alongside B2 so `resolve_years` (originaldate) can see the filled albums.

### B2. First full-library resolve run over all metadata axes
- [ ] Drive genre + artist + year over the full 11,196-file library via MCP, chunked with
      `limit`, reviewing staged diffs before each commit. **This is the goal:** clean metadata so
      Navidrome tag-search works. Scope the work with `list_artists(limit=…)` /
      `list_albums(year_status=…, limit=…)` (actionable groups = `blank_originaldate > 0`).
      Followed by a deliberate **review/testing break** before any filesystem work begins.

---

## Phase C — path canonicalization (decided 2026-08-02; runs after Phase B is clean)

> End state: every file's path is **generated from its tags** via a configurable naming
> pattern (e.g. `Artist\Artist - Year - Album\NN - Title.ext`) — including cases like a bare
> album folder in the library root moving under its artist folder. Tags are the source of
> truth; paths are derived output. **Nothing renames until every path↔tag disagreement is
> either fixed or carries a deliberate ignore disposition.**

### C1. Full path↔tag coherence detector (the pre-rename gate)
- [ ] Widen mismatch detection from today's albumartist-only signal to EVERY path-encoded
      field: top folder ↔ artist/albumartist (exists today), release-folder leaf ↔ album +
      year-in-leaf (via `parsing.parse_folder`), filename ↔ tracknumber + title (via
      `parsing.parse_filename_track`). Reuses `fold` matching + the fix-or-ignore disposition
      pattern (`file_mismatch_status`). The C4 gate is ZERO unresolved rows: every
      disagreement fixed through the stage→commit flow or explicitly ignored.

### C2. Year-disagreement report (review-only) — `detect_year_disagreements`
- [ ] Report files whose stored `date`/`originaldate` disagrees with the MusicBrainz
      release-group first-release date (the comparator `resolve_years` already fetches and
      caches). Review-only, never auto-staged: re-releases and soundtracks make a wrong
      `(artist, album)` → release-group match plausible, so a human confirms every correction.

### C3. Song axis — AcoustID fingerprint verification (title + tracknumber)
- [ ] The audio-truth tier (promoted from deferred 2026-08-02 — track numbers must be proven
      before C4 renames them into filenames). Local **fpcalc/Chromaprint** (LGPL-2.1+,
      subprocess, prebuilt static Windows binary) via MIT `pyacoustid` → **AcoustID** web
      service (free app API key, ~3 req/s) → MB recording IDs → canonical title +, via the
      chosen MB release's tracklist, tracknumber. Covers both flavors: blank-fill (61
      no-title / 203 no-tracknumber / 2 placeholder titles, measured 2026-08-02; re-measure
      after B0/B2 — wrong-release fixes rewrite these) and wrong-value verification that text
      cannot do (the tags lie consistently; only the audio is independent). New axis on the
      existing `Axis` abstraction (`file_song_status`, `resolve_songs`, `set_song_status`/
      `reset_song_status`, filter/stats; no `list_songs`) + persistent
      fingerprint/lookup caches so re-runs are network-free. **Spec the
      recording→release/tracknumber reconciliation in PLAN.md first** (one recording ↔ many
      releases; scoring is the hard correctness question — anchor on the folder's files
      converging on one release's tracklist). New deps: fpcalc on PATH (needs a
      `check_health` check) + a second API key; re-verify fpcalc/AcoustID details at build
      time. Coverage caveat by design: bootlegs/remixes/YouTube rips are often absent from
      AcoustID — they fail safe (no match → worklist/ignore), never wrong-match.

### C4. Organize — pattern-driven renames/moves (M6 realized)
- [ ] `paths` becomes the second live `RevisionDomain` (`paths.py` stub; `path_revisions` DDL
      ships since v6): a naming-pattern setting generates every file's canonical path from its
      tags; `detect_path_deviations` (grouped, read-only) proposes the moves (root-level bare
      album folders → their artist folder included); `stage_paths`/`stage_paths_batch` →
      `diff_paths` → `commit_paths` → `history_paths`/`revert_paths` (`unstage_paths` to drop
      one), mirroring the tags domain per file (PLAN.md §18). First customers: every
      `misfiled_deferred` disposition from B0. Open seam questions: intra-batch move
      ordering, collision policy (§15), folder-rename atomicity.

### C5. Promote the result (user action, not code — was B3)
- [ ] Once Phases B + C are verified perfect on the working copy (everything clean except
      deliberately-ignored files), overwrite the actual library with the mended, renamed copy.

---

## Deferred until after Phases B + C

- **CLI surface — deliberately LAST (user decision, reaffirmed 2026-07-05):** all tools are
  MCP-first. Eventual pass mirrors each MCP tool name with `-` for `_` (`resolve-*`,
  `set-*-status`, `diff-tags`/`commit-tags`, `revert-tags`/`revert-commit`,
  `list-files --*-status …`) once everything else is complete and finalized.
- **M5 — Polish:** genre vocabulary tuning, album-level genre override, README pass, packaging
  (`uv tool install` / `uvx tagmend mcp` / `pipx`).

---

## Low-priority / defensive (no blockers today)

- [ ] **Bulk manual-stage convenience** for a genuine `no_match` (artist truly not on Last.fm).
      The per-file escape hatch exists (`stage_tags` + `commit_tags`); a bulk artist/folder-scoped
      manual stage would be ergonomics. *Defer until a real `no_match` appears.*

## Known limitations — deliberately deferred (revisit if they bite)

- **`list_files` reports a stored axis status even when stale** (by design — staleness affects
  skip/reprocess decisions, not display); compare the `*_source_*` fields to spot staleness.
