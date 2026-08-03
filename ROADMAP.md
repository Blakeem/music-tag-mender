# TagMend — ROADMAP (forward-looking)

> Updated **2026-08-02**. This file lists only what **remains** — everything shipped has been
> removed (through the album-gaps detector: schema v11, 31 MCP tools; see `CLAUDE.md` for the
> shipped-state summary and `PLAN.md` for the design of record).
>
> **Direction:** finish the **metadata** mission first. Filesystem moves/renames (M6) and the
> **CLI surface** are deliberately last — CLI is pushed back until everything else is complete
> and finalized (reaffirmed 2026-07-05).

---

## Phase B — the primary deliverable (in order)

> **`music/` is already the safety copy** (confirmed 2026-07-05): the working folder is a full
> **135 GB copy** of the real library — the original sits untouched elsewhere and can be re-copied
> anytime. All live testing, scanning, and fix work happens on the copy; the close of Phase B is
> promoting the mended copy over the actual library once everything is verified (B3).

### B0. Live mismatch fix pass (next up)
- [ ] Drive the fix flow over the **19 flagged folders / 144 files**: grouped detect → research
      the correct release per folder → `stage_tags_batch` → review `diff_tags` →
      `commit_tags(path=folder)` (one revertible commit per release) → `repend_axes`.
      **Do this BEFORE the full resolve run (B2)** — identity fixes re-pend derived genre/year,
      so fixing identity first avoids resolving axes against wrong artists.
- Known per-folder routing from live testing (2026-07-04, `docs/live-test-findings.md`):
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
      `stage_tags_batch → diff_tags → commit_tags → repend_axes` spine (one commit per folder).
      Do this before/alongside B2 so `resolve_albums` (originaldate) can see the filled albums.

### B2. First full-library resolve run over all metadata axes
- [ ] Drive genre + artist + album over the full 11,196-file library via MCP, chunked with
      `limit`, reviewing staged diffs before each commit. **This is the goal:** clean metadata so
      Navidrome tag-search works. Scope the work with `list_artists(limit=…)` /
      `list_albums(album_status=…, limit=…)` (actionable groups = `blank_originaldate > 0`).
      Followed by a deliberate **review/testing break** before any filesystem work begins.

### B3. Promote the result (user action, not code)
- [ ] Once B0 + B2 are verified perfect on the working copy, overwrite the actual library with
      the mended copy.

---

## Deferred until after Phase B

- **M6 — Organize (opt-in moves/renames):** `paths` is the second `RevisionDomain`; `moves.py`
  is a stub, `path_revisions` DDL ships since v6. Design stays on the books (revertible per-file
  moves; PLAN.md §18). Three open seam questions: intra-batch move ordering, collision policy
  (§15), folder-rename atomicity. **First real customers already queued:** every
  `misfiled_deferred` disposition from B0.
- **CLI surface — deliberately LAST (user decision, reaffirmed 2026-07-05):** all tools are
  MCP-first. Eventual pass exposes the axis verbs (`resolve-*`, `set-*-status`, `diff`/`commit`,
  `revert`/`revert-commit`, `list --*-status …`) once everything else is complete and finalized.
- **Song axis (title + track number) — after the CLI; scope re-measured 2026-07-05.** Two
  distinct flavors, deliberately sequenced late:
  - **Blank-fill (small, text-only):** the full 11,196-file library has **61 files with no
    title, 203 with no tracknumber, 10 placeholder "Track NN" titles** (the old "0 missing"
    audit was the 720-file dev library). Much of this may fall out of B0 (wrong-release fixes
    rewrite title/tracknumber) or be fixable from filenames via the folder-parsing primitive.
    Re-measure after B0/B2 before building anything.
  - **Wrong-title correction (the real lift):** can't be done from text metadata at all (the
    tags lie; nothing to compare against) — needs **acoustic fingerprinting**, mirroring
    Picard's "Scan": **fpcalc/Chromaprint** (LGPL-2.1+, subprocess, prebuilt static binaries)
    likely via MIT `pyacoustid` → **AcoustID** web service (free app API key) → MB recording
    IDs → canonical title/track. New axis on the existing `Axis` abstraction (`file_song_status`,
    `resolve_songs`, filter/stats/tools). **Spec the track/recording reconciliation in PLAN.md
    first** (one fingerprint ↔ many recordings; scoring is the hard correctness question).
    New deps the text axes don't have: an external binary on PATH (needs a `doctor` check) + a
    second API key. Re-verify fpcalc/AcoustID details at build time.
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
