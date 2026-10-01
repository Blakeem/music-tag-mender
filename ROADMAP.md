# TagMend — ROADMAP (forward-looking)

> Updated **2026-10-01**. This file lists only what **remains**. Everything shipped has been
> removed. See `CLAUDE.md` for the shipped-state summary and `PLAN.md` for the design of record.
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
- [ ] Drive the fix flow over the **19 flagged folders / 130 files** (re-measured 2026-08-29,
      unchanged on disk: the `folder_context` bucket moved 14 of the original 144 out of
      `flagged`): grouped detect → research the correct release per folder → `stage_tags_batch`
      → review `diff_tags` → `commit_tags(path=folder)` (one revertible commit per release).
      **Do this BEFORE the full resolve run (B2)** — identity fixes re-pend derived genre/year,
      so fixing identity first avoids resolving axes against wrong artists.
- New tooling for the research step, shipped since 2026-08-02, replacing the out-of-band script:
  `release_by_mbid` returns the release a file names, tracklist included.
  `detect_release_disagreements` reports every field where the file contradicts that release. `diff_tags` flags `stale_identity`
  before the commit. The seven release-stamp fields are now managed, so one commit can replace the
  whole wrong-release block.
- Known per-folder routing from live testing (2026-07-04):
  - [ ] **Skrillex/Gypsyhook ("Sonny", 8 files):** Last.fm `getCorrection("Sonny")` returns
        *already canonical*, so the Last.fm tier leaves the alias alone. The MusicBrainz name
        tier now runs ahead of it, and all 8 files carry Skrillex's `musicbrainz_artistid`.
        Re-check `resolve_artists` first. It stages the correction if MusicBrainz records "Sonny"
        as an alias of that id, and holds `name_id_disagreement` if it does not. Otherwise the fix
        flow (research the Gypsyhook EP identity), or `legit_ignore` if the Sonny credit is wanted.
  - [ ] **Soundtracks folders (Crow: City of Angels, Freddy vs. Jason — 35 files):** deeper than
        the container false positive — files carry the *score* release's titles/tracknumbers/
        MB-IDs over soundtrack audio (e.g. filename "Hole - Gold Dust Woman" stamped
        `title="La Masquera"`). Needs per-file re-identity via the fix flow, not `legit_ignore`.
        `detect_release_disagreements` cannot route this one: the files agree with the release
        they name, because the id they carry is itself the wrong release.
        (Dispositions set during testing were reset — both folders are `pending` again.)
  - [ ] **Tool [Discography] (2 Alice In Chains files):** genuinely misfiled →
        `misfiled_deferred`, never a tag write. The files move in the Phase C live path pass.

### B1. Live album-gap fill pass (92 blank-`album` files, measured 2026-07-05)
- [ ] Drive `detect_album_gaps` over the library: bulk-stage the `green` sibling proposals,
      confirm each `confirm`/`review` proposal per folder, then the usual
      `stage_tags_batch → diff_tags → commit_tags` spine (one commit per folder).
      Do this before/alongside B2 so `resolve_years` (originaldate) can see the filled albums.

### B2. First full-library resolve run over all metadata axes
- [ ] Drive genre + artist + year + song over the full 11,196-file library via MCP, chunked with
      `limit`, reviewing staged diffs before each commit. **This is the goal:** clean metadata so
      Navidrome tag-search works. Scope the work with `list_artists(limit=…)` /
      `list_albums(year_status=…, limit=…)` (actionable groups = `blank_originaldate > 0`).
      Followed by a deliberate **review/testing break** before any filesystem work begins.
      Re-measure the song blank-fill counts (61 no-title, 203 no-tracknumber, measured 2026-08-29)
      after B0 and after this run, since wrong-release fixes rewrite them.

---

## Phase C — path canonicalization (decided 2026-08-02; runs after Phase B is clean)

> End state: every file's path is **generated from its tags** via a configurable naming
> pattern (e.g. `Artist\Artist - Year - Album\NN - Title.ext`) — including cases like a bare
> album folder in the library root moving under its artist folder. Tags are the source of
> truth; paths are derived output. **Nothing renames until every path↔tag disagreement is
> either fixed or carries a deliberate ignore disposition.**

### C1. Live path pass over the working copy
- [ ] Drive `detect_mismatches` to `gate_open`: fix each flagged file through the
      stage→commit flow or record a `set_mismatch_status` decision. Then save the naming
      pattern with `set_naming_pattern`, review `detect_path_deviations`, and run
      `stage_paths` → `diff_paths` → `commit_paths`. The first customers are the
      `misfiled_deferred` files from the B0 mismatch fix pass.

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
