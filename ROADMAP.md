# TagMend — ROADMAP (forward-looking)

> Updated **2026-06-16**. This file lists only what **remains**. Everything shipped through
> **M4** has been removed so the list is purely forward-looking. `PLAN.md` is the design of
> record; this is the punch-list of what's left before (and after) the first full-library run.

## Where we are (shipped & tested)

- **M0–M1** readiness + read path. **M2** genre pipeline (Last.fm → vocabulary → stage).
  **M3 / M3.5** git-like stage→commit→history→revert core, `revert_commit`, genre-status
  visibility.
- **M4 — artist-name normalization + review state (DONE).** `resolve_artists`
  (`artist.getCorrection` cascade-stage of `artist`/`albumartist` + MBID, with
  feat/sentinel/empty + multi-value guards, dry-run, empty-staging precondition); sticky
  per-file **artist exclusion** (`set_artist_status`/`reset_artist_status`, value-scoped across
  both name fields) + per-axis visibility (`list_files(artist_status=…)`, `library_stats`
  `artist` block), with **field-aware** `done`/`staged` so genre and artist read independently.
- **Schema v7. 21 MCP tools.** Both missions (genre + artist-name) have working auto + manual
  exclusion pipelines with full revert. The review loop is: `resolve_*(dry_run)` → exclude what
  you don't want → `resolve_*` (real) → review the staged diff → `commit_tags`.

> The old "review-one-at-a-time" tools (`approve_mapping`, `commit_artist`,
> `list_pending_review`, `get_artist_candidate`, `review_stats`) are **not being built** — they
> were superseded by the dry-run + exclusion + staged-diff model. Removed from this list.

---

## Still needed — ordered by what gates the first real run

### 1. Live Last.fm readiness check (small)
- [ ] Add a Last.fm connectivity ping to `doctor` (one cheap `artist.getTopTags` call) so a long
      run fails fast on a bad key / no network. `doctor.py` checks settings, music folder, and
      ledger only — not Last.fm. *The live API itself already works; this just makes it a
      repeatable pre-flight.*

### 2. Pre-run safety (mostly done — one user action left)
- [x] Round-trip on real audio (scan → stage → commit → verify on disk → `revert_commit`)
      proven live for both genre and artist, across formats.
- [ ] **Recommend a filesystem-level copy of `music/` before the first full run** — cheap
      insurance beyond the managed-tag v0 baseline. *(User action, not code.)*

### 3. The first full-library run (the actual goal)
- [ ] Drive the genre + artist pipelines over the full ~130 GB / 256-artist library via MCP,
      chunked with `limit`, reviewing staged diffs before each commit. Everything above de-risks
      toward this. *(Operation, not code.)*

---

## Low-priority / defensive (no blockers today)

- [ ] **No-identity worklist:** files with no `artist` AND no `albumartist` are silently skipped
      (`skipped_no_artist`) and bucket as plain `pending` with no worklist. Give them a
      first-class state so they're visible. *Zero such files exist in the library today.*
- [ ] **Bulk manual-stage convenience** for a genuine `no_match` (artist truly not on Last.fm).
      The per-file escape hatch already exists (`stage_tags` + `commit_tags`); a bulk
      artist/folder-scoped manual stage would be ergonomics. *Defer until a real `no_match`
      appears.*

## Known limitations — deliberately deferred (revisit if they bite)

- **getCorrection maps junk album-artist labels to the MusicBrainz `[unknown]` placeholder.**
  e.g. `Original Soundtrack` → `[unknown]` (+MBID `125ec42a…`); affects ~16 soundtrack files.
  Left **as-is** per decision 2026-06-16 ("adjust if it becomes an issue"). If it does, the
  targeted fix is a parser guard that rejects corrections resolving to a MB special-purpose
  placeholder name/MBID (`[unknown]`, `[no artist]`) → treat as `no_correction`. Compilation
  rows are otherwise protected today by the `various artists`/`various`/`va` sentinel skip.

---

## Deferred milestones

- **CLI surface** — deliberately **last**; all tools are MCP-first. The eventual pass would
  expose: `stage-genres`, `resolve-artists`, `set-artist-status`/`set-genre-status`,
  `diff`/`commit`, `revert`/`revert-commit`, `list --genre-status … --artist-status …`.
- **M5 — Polish:** genre vocabulary tuning, album-level genre override, README, packaging
  (`uv tool install` / `uvx tagmend mcp` / `pipx`).
- **M6 — Organize (opt-in moves/renames):** `moves.py` is a stub; its `path_revisions` DDL
  ships in the schema. Now unblocked — it needs M4's canonical artist names, which exist. The
  dirty `Miami Nights` folders are the intended demo.
