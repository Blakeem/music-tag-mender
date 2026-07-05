# Live MCP tool test — findings log (2026-07-04)

Happy-path pass over all 30 MCP tools, driven through the live MCP server against the dev
library (`E:\music-tag-mender\music`, 11,196 files) and the live ledger
(`C:\Users\Blake\AppData\Local\tagmend\tagmend.sqlite3`). Write-path tests used benign
genre changes on 3 Blue Stahli remix files (ids 1265–1267), verified ON DISK with mutagen,
and reverted through the engine. Network tests hit live Last.fm + MusicBrainz on small
scopes (dry-run where staging wasn't the point). Final state: library back to baseline —
all axes `pending`, staging empty, no status rows; the ledger intentionally retains test
commits 1–4 (commit/revert × 2 — tracked history, by design).

**Result: 30/30 tools PASS on the happy path. Zero CRITICAL, zero HIGH. 4 MEDIUM + 4 LOW
findings for triage below.**

Severity legend:
- **CRITICAL** — wrong data written / data loss / revert broken.
- **HIGH** — a tool fails or returns wrong results on its happy path.
- **MEDIUM** — works but behavior is misleading, inconsistent, or surprising.
- **LOW** — UX/polish: naming, docs, payload shape, discoverability.

---

## Issues

### 1. MEDIUM — `list_artists` / `list_albums` have no `limit` (or any) parameter; payloads are context-hostile at scale
Measured live: `list_artists` → ~500 entries / ~20 KB; `list_albums` → **1,023 groups /
139 KB** in one response. Both tools take zero narrowing params. Every other listing tool
(`list_files`, `list_commits`, `detect_mismatches`) got `limit`/scoping, and the mismatch
surface was explicitly designed compact-first for context efficiency — these two predate
that convention. An LLM caller cannot scope a genre/album campaign without paying the full
payload each time. Suggest: `limit` + `offset`-or-`prefix` (or `artist=`/`album_status=`
filters) on both.

### 2. MEDIUM — `resolve_artists`: "already canonical" outcomes are invisible; buckets don't sum to `processed`
Live: `resolve_artists(artist="Sonny", dry_run=true)` → `processed: 1` but corrected 0,
`no_correction` 0, all skip buckets 0, `mappings` [] — the value vanished into an
undocumented-in-the-response "already canonical" bucket (the docstring does mention it,
but the payload gives no way to distinguish "Last.fm says this name is already right"
from "nothing happened"). Compare `SkrilleX` → clean `corrected_values: 1` mapping with
MBID. Suggest: an `already_canonical` count (making buckets sum to `processed`), ideally
with the values listed like `no_correction_values`.

### 3. MEDIUM — `list_albums` `album_status: "pending"` conflates "actionable" with "already has a year"
Live: group *36 Crazyfists / A Snow Capped Romance* shows `pending` in `list_albums`, but
`resolve_albums` on it processes 0 groups and reports `skipped_present: 11` — every file
already has `originaldate`. "Pending" on the album axis means "no status row", not "needs
work", so the 1,023-group list gives no way to find the groups that actually need a year
(I had to query the ledger directly to find one). Suggest: a per-group
`blank_originaldate` count (or a derived `present` state) in `list_albums`.

### 4. MEDIUM — `scan_library` counters mislead after commit/revert: `updated: N, tags_read: 0`
First incremental scan after the write tests: `updated: 3, tags_read: 0` — reads as "3
files changed but their tags were not read". Verified NOT a staleness bug: the snapshot
was current (commit/revert refresh it in-band) and a second scan was fully idempotent
(`updated: 0`). Two components: (a) the commit path doesn't refresh the `files` row's
mtime/size, so the next scan re-flags every committed file as `updated`; (b) whatever
`tags_read` counts, it isn't "files whose tags were (re)checked". After a real fix pass
(~144 files) the next scan will say `updated: 144, tags_read: 0`, which will look like a
scan failure. Suggest: refresh the stat row at commit time, and/or make the counter
semantics explicit in the payload docs.

### 5. LOW — `library_stats` docstring documents only the `genre` block
The response carries four per-axis blocks (`genre`, `artist`, `album`, `mismatch`), but
the tool docstring (the LLM-facing contract) describes only the genre block and its
drill-down. Same for the per-extension/`unprocessed` fields being fine — it's only the
axis blocks that are stale. One sentence listing all four + their drill-downs fixes it.

### 6. LOW — `get_file` docstring return shape is stale (missing album + mismatch fields)
Docstring lists `genre_status/genre_source_*` and `artist_status/artist_source_*`; the
actual response also includes `album_status/album_source_*` and
`mismatch_status/mismatch_source_field/mismatch_source_value` (verified live).

### 7. LOW — `disagreement_rate` returned at full float precision
`detect_mismatches` returns `"disagreement_rate": 0.01250861814242096` in every response.
Round at the serialization edge (~4 decimals); keep the engine float.

### 8. LOW — fully-silenced folders vanish from the grouped detect view (by design; confirm it's wanted)
When EVERY flagged row of a folder is dispositioned, the folder disappears from `groups`;
the only traces are the report-level `suppressed` map + the summary tail ("(23 silenced
by disposition)"). Per-group `suppressed` appears only for PARTIALLY silenced folders
(verified both cases live). Nothing is hidden — `list_files(mismatch_status=...)`
recovers the list — but a reviewer scanning groups has no per-folder trace of fully
silenced folders. If that bites during the live fix pass, an `include_suppressed=true`
view is the additive fix. Triage decision, not a bug.

---

## Observations for the live fix pass (library data / flow notes, not tool bugs)

- **Class C (Skrillex/"Sonny") will NOT be fixed by `resolve_artists`:** Last.fm
  `getCorrection("Sonny")` returns no correction (verified live, issue 2 above). The
  decide-run assumption "route class C to resolve_artists" doesn't hold for this case —
  the Gypsyhook folder needs the fix flow (or `legit_ignore` if the Sonny credit is
  wanted as-is). `SkrilleX → Skrillex` (21 files) DOES correct cleanly, with MBID.
- **The Soundtracks corruption is deeper than `albumartist`.** In
  `Soundtracks\Crow, The City Of Angels`, file 8291's filename says
  "01 - Hole - Gold Dust Woman…" but its stamp is the Graeme Revell *score* release
  (`title="La Masquera"`, `tracknumber=13/15`, score MB-IDs). These are class A/B
  wrong-release stamps (soundtrack vs score, same name) — the folder needs per-file
  re-identity, not just `legit_ignore`.
- `list_albums` shows groups keyed by `""` and `" "` (empty vs whitespace-only artist) as
  two distinct groups — worth a normalization thought someday.
- `list_artists` is a ready-made worklist for `resolve_artists`: SkrilleX/Skrillex,
  Alice In/in Chains, Lamb Of/of God, InnerPartySystem/Innerpartysystem, Aes Dana/AES
  Dana, four Lusine casings, Jean Michel/Jean‐Michel Jarre, Wumpscut/:wumpscut:, etc.
- Test artifacts left in the live ledger on purpose: commits 1 (batch test) / 2 (its
  revert) / 3 (single-file test) / 4 (its revert). All history, all revertible — nothing
  to clean up.

---

## Verified behaviors (spot checks worth recording)

- Ledger auto-upgraded v9 → v10 on first tool call; `mismatch` block present in stats.
- Grouped detect: 144 rows → 19 folders; tallies/tiers match the decide-run measurements
  exactly (Ozzy Jem:8/Ozzy:4, Cure 18 high, etc.).
- Disposition lifecycle: `set_mismatch_status(value="Graeme Revell")` scoped exactly the
  23 flagged files across 2 folders; suppression visible at report level (and per-group
  for partial folders); tier/limit compose with `group=true`; `reset` restored
  flagged=144 precisely; `list_files(mismatch_status=...)` + snapshot fields correct.
- `stage_tags_batch`: merge semantics right (diff genre-only, 17 other managed fields
  ride along), `origin="manual"`, note propagated through staged row → revision.
- `commit_tags(path=folder)` grouped the batch under ONE commit; write verified on disk
  with mutagen; `revert_commit` (dry-run then real) restored the original tags on disk
  (verified); `revert_tags(version=0)` ditto for the single-file path; every revert is
  its own `origin='revert'` commit (ids 2, 4).
- `repend_axes(commit_id=1)` → `{files: 2, artist_status_cleared: 0}` — correct no-op
  shape on a ledger with no auto history yet.
- History: v0 `scan` baseline captured at stage time; v1 `manual` with `commit_id`;
  field-scoped diffs; `genre_status` flips to `staged` on stage and back on unstage.
- `stage_genres` (live Last.fm): staged vocabulary genres
  (`industrial/electronic/alternative/electronica` for Blue Stahli), `origin="auto"`,
  provenance note ("lastfm: …"), correct `skipped.manual` bucket for the excluded file.
- `resolve_albums` (live MusicBrainz): *White Zombie / Astro-Creep: 2000* → `1995`,
  would-stage 11, dry-run staged nothing.
- All six `set_/reset_*_status` verbs round-trip with correct `affected` counts.
- `scan_library` incremental over 11,196 files completed in-call; second run idempotent.

---

## Coverage checklist

| Tool | Tested | Result |
|------|--------|--------|
| health_check | ✅ | PASS |
| scan_library (incremental ×2) | ✅ | PASS (issue #4) |
| library_stats | ✅ | PASS (issue #5) |
| list_files (path, mismatch_status) | ✅ | PASS |
| get_file | ✅ | PASS (issue #6) |
| detect_mismatches (flat/group/folder/tier/limit) | ✅ | PASS (issues #7, #8) |
| set_mismatch_status (file_ids, value) | ✅ | PASS |
| reset_mismatch_status (file_ids, value) | ✅ | PASS |
| stage_tags | ✅ | PASS |
| stage_tags_batch | ✅ | PASS |
| unstage_tags | ✅ | PASS |
| diff_tags (scoped + all) | ✅ | PASS |
| commit_tags (scoped + all) | ✅ | PASS |
| repend_axes | ✅ | PASS |
| list_commits | ✅ | PASS |
| get_commit | ✅ | PASS |
| history_tags | ✅ | PASS |
| revert_tags | ✅ | PASS |
| revert_commit (dry_run + real) | ✅ | PASS |
| stage_genres (live Last.fm, manual-skip) | ✅ | PASS |
| set_genre_status | ✅ | PASS |
| reset_genre_status | ✅ | PASS |
| list_artists | ✅ | PASS (issue #1) |
| resolve_artists (dry_run, correction + none) | ✅ | PASS (issue #2) |
| set_artist_status | ✅ | PASS |
| reset_artist_status | ✅ | PASS |
| list_albums | ✅ | PASS (issues #1, #3) |
| resolve_albums (dry_run, present-skip + mapping) | ✅ | PASS |
| set_album_status | ✅ | PASS |
| reset_album_status | ✅ | PASS |
