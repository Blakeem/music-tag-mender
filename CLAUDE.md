# CLAUDE.md — TagMend working notes

TagMend (`tagmend`) is a CLI + MCP tool that cleans up **genre** and **artist-name**
tags in a music library using **Last.fm**, with an **append-only, fully revertible**
history per file, plus an opt-in **file/folder reorganization** feature (also
tracked & revertible). Engine-first: all logic lives in `tagmend.engine`; the CLI and
MCP server are thin wrappers. **Read `PLAN.md` for the full design** — this file is
just how to work in the repo.

Current status: **M1 read path + M3 write-path core + M2 genre pipeline shipped.**
The git-like stage → commit → history → revert engine is built and tested: a
domain-neutral commit core (`engine/commits.py` — the `commits` table, the
`RevisionDomain` seam, the crash-safe `run_commit` loop, **resume-free** recovery),
the tags domain (`engine/staging.py` — `stage_tags`/`unstage_tags`/`diff_tags`/
`commit_tags`, v0 baseline captured at stage time), plus `versioning.py`
(revert/history) and the full tags MCP family + discovery + commit inspection. M2's
genre side is live: `lastfm.py` (cached/paced artist+album top-tags), `classify.py`
(vocab/overlay + the pure `classify.classify_genres`), `genres.py` (the `resolve_genres`
tool). M3.5 shipped
too: `revert_commit` group undo (skip+report, empty-staging guard, dry-run; every
revert — even per-file `revert_tags` — is now its own `origin='revert'` commit) and
genre-status visibility (`list_files(genre_status=...)` filter + `get_library_stats`
genre counts).
M4 phase 1 shipped too: `artists.py` (`resolve_artists` — cascade-stages the
`artist.getCorrection` canonical name + MBID across `artist`/`albumartist`, with
feat/sentinel/empty + per-file multi-value guards, dry-run, and the empty-staging
precondition; results cache in the existing `lastfm_cache`). M4 phase 2 shipped the artist status tools:
`set_artist_status`/`reset_artist_status` (scope by file or by a value matched across BOTH
`artist` and `albumartist`), `list_files(artist_status=...)` and a `get_library_stats['artist']`
block. The year axis (`years.py`) shipped next (MusicBrainz `originaldate` blank-fill,
`list_files(year_status=...)` and a `get_library_stats['year']` block). **One outcome-row status
model** covers the four tag axes (genre, artist, year, song). A `file_<axis>_status` row records
`done`, `no_match` or `manual` with a snapshot of the axis identity and of the field value it
settled. `store.derived_status` reads a present file's status, first match wins: `staged` when a
staged change alters the axis fields, a sticky `manual`, `no_identity`, the row's status while
both snapshots still match the tags, else `pending`. A revert, a rescan after an external edit, an
unstage or an identity change therefore re-opens a `done` or `no_match` file with no writer. Resolvers write
`done`/`no_match`. The commit writer writes `manual` for a human change to an axis field, keyed on
`tag_revisions_staged.changed_fields` (the fields the stage changed against disk), so a commit
re-applied after a crash still records it. `axis_status.py` is the one implementation behind
every `set_/reset_<axis>_status` pair. The song axis (`songs.py`) identifies each file's recording
by its AcoustID fingerprint. A folder whose files carry `musicbrainz_albumid` is checked against
those releases, a folder without ids converges on the one Official release most of its files share
and fills only blank `title`/`tracknumber`/`discnumber` as `auto`, and a folder whose audio is not
on its tagged release is reported in `rebind_folders`. `resolve_songs(release_id=...)` stamps a
whole folder onto one chosen release as a single `manual` batch. The **mismatch-fix** surface
records one path decision per file in `file_mismatch_status`. `legit_ignore` keeps the folder and
renders the filename. `misfiled_deferred` lets the tags render every path level. No status keeps a
filename. The decision's JSON snapshot holds the names it `covers`, their tag inputs, the file's
path version and, on a keep, its folder key. A covered name stays silenced while its tags hold and
its binding holds: the folder key for a keep's folder-level name, the path version for every other
name. A changed tag, a committed move or a new uncovered flag re-surfaces the file with
`was`/`changed`.
`set_mismatch_status` requires `covers`, takes one release-folder group per call, and refuses a
keep that leaves a group member out. The report's `gate_open` holds when nothing is flagged and no
exception is undecided. `mismatch.gate_state`, `check_files` and `planner_keep` are the gate the
path domain will read. `list_files(mismatch_status=...)` and `get_library_stats['mismatch']` read the
same classifier. `stage_tags_batch` stages several files atomically, always as
`origin="manual"`. `reopen_axes(commit_id)` deletes the `done`/`no_match` rows of the files of a
commit holding no `auto` revision, on all four tag axes, and keeps `manual`. The `detect_album_gaps` tool (`album_gaps.py` + the pure, standalone
`parsing.py`) groups blank-`album` files by folder and proposes sibling / folder-parse fills
plus a review-only MusicBrainz `(artist, title)` recording tier (`mb_recording`, opt-out via
`use_musicbrainz=False`, cached in `musicbrainz_recording_cache`) for the `stage_tags_batch →
diff → commit → reopen_axes` spine. `resolve_artists` then gained a **MusicBrainz name tier**
ahead of the Last.fm one. `musicbrainz.py`'s `artist_by_mbid` looks an artist up directly by
the `musicbrainz_artistid` (or `musicbrainz_albumartistid`) the file already carries, a lookup
by id and never a search, cached in `musicbrainz_artist_cache`. A fold over casing, typography
and dash-vs-space then settles the value against that artist's canonical name (`source:
musicbrainz`) or a registered alias (`source: musicbrainz_alias`). MusicBrainz casing is
trusted here. Last.fm's still is not, so the Last.fm tier sees only values with no MBID, or an
MBID MusicBrainz does not know. A name MusicBrainz records under neither form lands in the new
`name_id_disagreement` bucket, held, as does a value the library pairs with two different MBIDs.
`_build_target` now writes each field's own id field AND its own sort field, so an
`albumartist`-only correction no longer overwrites `musicbrainz_artistid`, and a rewritten name
no longer leaves `artistsort` describing the old spelling. Only the MusicBrainz tier supplies a
sort name. Last.fm publishes none, so its tier leaves the sort field untouched rather than
guessing at or destroying a value. The `detect_album_conflicts` tool
(`album_conflicts.py`) is the release-level sibling of `detect_track_conflicts`. It flags files
whose album identity differs from their folder siblings'. A file's album identity is
`musicbrainz_albumid` when it carries one, and otherwise the display album artist, the album
title and the year. The display album artist falls back `albumartist` → `Various
Artists` when the `compilation` flag is set → `artist`. Comparison folds casing, typographic
character choice and whitespace runs, and nothing else. It is deliberately NOT `mismatch.fold`,
which strips every non-alphanumeric character and so erases the punctuation splits this
detector exists to find. Three tiers: `high` when the release ids differ or only some files
carry one, `medium` for a name or year disagreement with no ids involved, `low` when one title
carries a `(disc N: …)` suffix the others do not. Only the MINORITY is flagged, and every row
carries the majority identity. Blank-`album` files are skipped, since `detect_album_gaps`
already owns them. `musicbrainz.py` then gained `release_by_mbid`, a direct
`/ws/2/release/<mbid>?inc=recordings+artist-credits` lookup behind the `MBReleaseSource`
Protocol. It returns an `MBRelease` (the album title, the album-artist credit, and every
`MBMedium`'s `MBTrack` tracklist), cached in `musicbrainz_release_cache` as one JSON payload
because a release is a nested document and nothing queries inside it. The
`detect_release_disagreements` tool (`release_disagreements.py`) is the third comparison in the coherence
family: `detect_mismatches` compares a file's tags against its PATH (every folder level and the filename),
`detect_album_conflicts` and `detect_track_conflicts` compare a file against its folder
SIBLINGS, and this compares a file against an EXTERNAL authority, the release its own
`musicbrainz_albumid` names. That id is what lets it say what a tag SHOULD be rather than only
that something is wrong. Inside the release a file finds its track by
`musicbrainz_releasetrackid`, or failing that by `musicbrainz_trackid`. Position is never a
fallback, because a wrong track number is one of the defects reported here. Release-level
fields (`album`, `albumartist`, `date`, `releasecountry`, `musicbrainz_albumstatus`) are
checked even for a file carrying no track id. Track-level fields (`title`, `artist`, `tracknumber`,
`discnumber`) need a matched track and are skipped without one. `albumartist` and `artist` are not
compared by name when the credit names one artist and the file's own id field holds exactly that
id, since `resolve_artists` owns the spelling of a single identified artist. A blank field is a **fill**,
not a disagreement: `flagged` counts only fields where the file says one thing and the release
says another, while `fill_rows` collects what the release can supply for free. `detect_year_disagreements` (`year_disagreements.py`)
compares a file's year tags with the first-release year of its album's MusicBrainz release group,
found by the album identity (album artist else artist, album). `high` means the `originaldate` year
differs, `medium` means the `date` year is earlier than the first release. `release_limit` (default
200) caps the uncached lookups one call makes, and cache writes are its only ledger writes.
47 MCP tools total. Schema is **v25** (additive: v11 adds
`musicbrainz_recording_cache`, v12 renames `file_album_status` → `file_year_status` in place —
dispositions preserved; v13 adds `tag_revisions.managed_set`, stamping which managed-tag set
governed each revision so a revert can restore emptiness on the widened fields; v14 adds
`files.reader_version` so an incremental scan re-reads a row an older tag reader wrote. v15 adds
`musicbrainz_artist_cache`, the by-MBID artist lookup's cache. v16 adds
`musicbrainz_release_cache`, the by-MBID release lookup's cache, holding the parsed release as
one JSON payload. v17 adds the stage-time base signature on `tag_revisions_staged`, append-only
triggers on both revision logs and their `commit_id` indexes. v18 adds `files.path_key` with a
UNIQUE index. `path_keys.py` is the one place that decides when two paths name the same file, and
every folder argument resolves through `path_keys.resolve_folder_arg` (`folder_arg_key` for its
key). An older ledger upgrades in place, except that the v18 upgrade refuses one where two file
rows share a path key and names them. v19 restamps a `manual` commit whose revisions are all `auto`
as `auto`, since a commit's origin is now derived from the rows it sweeps. v20 renames in place
(`musicbrainz_cache` to `musicbrainz_release_group_cache`, `*_id` MBID columns to `*_mbid`,
`tag_revisions.reverted_from` to `reverted_to_version`), gives `lastfm_correction_cache` typed
columns, drops the unused `files.status`, and makes `tag_revisions.managed_set` required. v21 gives each tag-axis status row a
`source_value` snapshot, replays manual revisions into `manual` rows, drops `voided_auto`, and adds
`tag_revisions_staged.changed_fields`. v22 adds `fingerprint_cache` (one fpcalc result per file,
keyed to its size and mtime) and `acoustid_cache` (AcoustID lookups keyed by a fingerprint hash,
with a 7-day expiry on an empty answer). v23 adds `file_song_status` and
`tag_revisions_staged.supplied_keys`, the keys the caller passed that survive a `fill_only` drop,
which the stale-identity warning in `diff_tags` treats as confirmed. v24 rewrites each
`file_mismatch_status` row as a JSON decision snapshot and drops `source_field`. v25 adds the
path staging columns (`to_key`, the stage-time signature, `reverted_from`). A newer ledger is
refused). The paths domain (`paths.py`) is the second `RevisionDomain`. `stage_paths_batch`,
`unstage_paths`, `diff_paths`, `commit_paths`, `history_paths` and `revert_paths` move files with one
`files.id` across every move, never overwrite a target, and prune emptied source folders. A move
that landed before a crash is finished by the next `commit_paths`. The naming pattern
(`naming.py`, settings key `naming_pattern`, empty for the default) renders each file's path from
its tags. `set_naming_pattern` saves it and `container_folders`. `detect_path_deviations`
(`path_deviations.py`) reports each file against its render, and previews a candidate pattern
unsaved. `stage_paths` stages entire folders as `auto` rows. Both read one planner,
`paths.plan_library`, which applies every hold. `diff_paths` flags an `auto` row `stale` when the
render moved. A rendered path never flags in `detect_mismatches`, which a round-trip test checks.

**The canonical tag namespace is TagMend's, not mutagen's.** mutagen's "easy" layer is an
incomplete normalizer, so `tags.py` owns the mapping wherever it is wrong: `EasyID3` points
`albumartistsort` at a `TXXX` frame Picard never writes (it uses `TSO2`), `EasyMP4` freeform atom
names are case-sensitive and its `releasecountry` atom is a word off from Picard's, and Vorbis has
**no easy layer at all** so FLAC/OGG names pass through raw. Read accepts every known spelling and
prefers the container-native one; write emits the native one and drops the alternate, so a file
never carries two contradicting values for one concept. Collapse a pair ONLY after measuring that
the two names never disagree in the wild — `organization`/`label` is left unmapped and unmanaged
because 245 real FLACs hold a different label in each. **`MANAGED_TAGS` is 26** (5 original + 13
identity + 7 release-stamp + the `artists` list = managed-set version 4; `MANAGED_SETS` keeps every
older set frozen because stored revisions point at them). A snapshot covers only its own set's
fields, so staging and revert first append a `scan` re-baseline (`versioning.observe_widened_fields`)
to a file whose latest revision predates the current set, and the commit refuses a row staged
before it. A drift-free re-baseline never blocks `revert_commit`. A revert takes a field its target never
governed from the first later revision that governs it when that revision is a re-baseline, and
otherwise keeps the current value. `artists` is the Picard ARTISTS list
Navidrome links artists from: `TXXX:ARTISTS` on ID3, the `ARTISTS` freeform atom on MP4, `ARTISTS`
on Vorbis. Any change to what `read_tags` produces bumps
`TAG_READER_VERSION` in the same commit, which is what makes the next incremental scan re-read a
stale row exactly once. Every write verifies its temp copy before the atomic swap (audio payload
hash except on Ogg, unmanaged entries, ID3v1/APEv2 presence, managed read-back) and raises
`TagWriteError` on any difference. A container the verifier has no layout for, or a non-Ogg file
whose audio payload cannot be located, is refused, and staging refuses such a file up front.

## Python

- **Target Python 3.12** (`requires-python = ">=3.12"`). Write 3.12 code: built-in
  generics (`list[str]`, `dict[str, object]`), `X | None` unions, `pathlib`, modern
  typing. The `py` launcher default and the venv are both 3.12.

## Golden rules

- **Use the logger, never `print`.** `from tagmend.log import get_logger` →
  `logger = get_logger(__name__)`. Use lazy `%`-style args (`logger.info("x=%s", x)`),
  not f-strings. User-facing CLI text uses `typer.echo` (that's output, not logging).
  In the MCP server, logs go to **stderr only** — stdout is the JSON-RPC channel.
- **Engine holds the logic; CLI/MCP stay thin.** New behavior goes in
  `tagmend/engine/*`, then gets a thin CLI subcommand and/or MCP tool.
- **Settings live on disk, not in env.** The MCP server can't see the CLI's shell.
  Read config via `tagmend.config.load_settings()`; never read env/JSON directly.
- **`music/` is the live-testing sandbox — unit tests NEVER touch it.** It is a full
  **copy** of Blake's real 135 GB / 11,196-file library (the original sits untouched
  elsewhere and can be re-copied anytime). Live testing, scanning, resolve/fix runs, and
  problem discovery deliberately run against this copy so everything is proven perfect
  before the real library is overwritten with the result (ROADMAP B3). It's gitignored
  and copyrighted. Unit tests use **generated files only**: `tmp_path` / the
  `temp_library` fixture for snapshot tests, or `make_track` (copies a silent
  `.mp3`/`.flac`/`.m4a`/`.ogg` template + writes tags) for real-audio
  read/write/commit/revert coverage across all four formats.

## Tool naming

Every MCP tool is `verb_object[_qualifier]`, lowercase snake_case, **verb always first, no
exceptions**. The verb names the operation; the object names the domain, axis, entity, or finding
(compound nouns allowed: `album_gaps`, `library_stats`). MCP is the primary surface. The CLI
carries only the four commands worth running by hand (`check-health`, `scan-library`,
`get-library-stats`, `detect-mismatches`), and each mirrors its MCP name with `-` for `_`. A new
tool needs no CLI command. Adding one means mirroring the MCP name exactly, with no aliases.
CLI-only program commands (`mcp`, `version`, `config*`) sit outside the grammar.

**The verb set is closed.** A tool is **mutating** if it changes any persisted state other than the
snapshot mirror (`files`/`file_tags`): staged rows, commits, status rows, or the music
files themselves.

- Observing: `check, scan, list, get, detect, diff, history` (`diff`/`history` are git-style nouns
  in the verb slot; `scan` refreshes only the snapshot mirror)
- Mutating: `stage, unstage, commit, revert, resolve, set, reset, reopen`

A verb may span two call/return shapes when the object disambiguates (`get_file` vs
`get_library_stats`; `revert_tags` vs `revert_commit` — the commit ledger is domain-neutral). A new
verb requires an operation no existing verb covers.

| Shape | Template |
|---|---|
| readiness / ingest | `check_health` · `scan_library` |
| enumerate / fetch | `list_<plural>` · `get_<singular>` · `get_library_stats` |
| read-only findings | `detect_[<field>_]<plural finding noun>` |
| stage→commit cycle | `stage_/unstage_/diff_/commit_/history_/revert_<domain>` — domains `tags`, `paths` |
| atomic multi-target | `<stage-verb>_<domain>_batch` (`_batch` reserved, reusable) |
| commit ledger | `list_commits` · `get_commit` · `revert_commit` (bare — one `commits` table, no domain column) |
| lookup → stage | `resolve_<axis>s` |
| axis status | `set_<axis>_status` / `reset_<axis>_status` |
| post-commit reopen | `reopen_axes` (keyed by `commit_id`) |
| setting write | `set_<setting>` (`set_naming_pattern`) |

Rules, in order:

1. All read-only findings reports are ONE `detect_*` family — defined by call shape, never split by
   problem class or by whether a disposition table exists.
2. Reports name the **finding**, state ops name the **domain** (`stage_paths`, never `stage_moves`).
   One distinct plural finding noun per comparison (glossary below). Qualify with the field when the
   finding lives in exactly one (`album_gaps`, `year_disagreements`, `path_deviations`). Qualify with
   the thing compared when the finding spans several fields AND a sibling tool shares the finding
   noun (`track_conflicts` compares track slots, `album_conflicts` compares album identity). Leave it
   bare when it spans fields and nothing shares the noun (`mismatches`). Reuse the repo's word for that concept if one exists; otherwise
   coin exactly one noun and add it to the glossary in the same commit. A token that already means
   something else in the repo is not a reuse.
3. Prefer an existing verb over a new one.
4. **Tool identity:** a report is one tool per (left source, right source, conformance criterion)
   triple. More fields on the same triple = body change, same name. A new comparison or a third
   input (e.g. the naming pattern) = a new tool.
5. Number is fixed by slot: axis singular in `set_/reset_<axis>_status`, plural in
   `resolve_<axis>s`; domain and finding nouns always plural.
6. The `<axis>` token equals `Axis.name` in `engine/axis.py`, exactly. Renaming an axis is a code
   change first (`Axis.name` + `file_<name>_status` via `ALTER TABLE … RENAME TO` + the engine
   module), tool rename second — never one without the other.
7. One concept = one term, both directions, across tool names, CLI commands, engine
   module/function/class names, `Axis.name` values, status tables, and prose.

Glossary — the comparison behind each finding noun: `mismatch` = tags ↔ path (folders and filename) · `gap` = tag ↔
absent · `disagreement` = tag ↔ external source (MusicBrainz) · `conflict` = tag ↔ sibling tags in
the same folder (coined) · `deviation` = current path ↔ canonical path generated from tags by the
naming pattern (coined).

## Quality gates — all four must pass before anything is "done"

Run from the repo root (venv at `.venv`):

```powershell
.\.venv\Scripts\ruff.exe check .          # lint (near-all rules; see pyproject)
.\.venv\Scripts\ruff.exe format .         # autoformat (use --check in CI)
.\.venv\Scripts\mypy.exe                  # strict static typing
.\.venv\Scripts\pytest.exe                # tests
```

Lint, format, types, and tests are **required** every change. Ruff is configured with
`select = ["ALL"]` minus a few formatter-conflicting rules — fix issues, don't
broaden the ignore list without reason. `cli.py` intentionally omits
`from __future__ import annotations` because Typer evaluates annotations at runtime.

## Running the tool

```powershell
.\.venv\Scripts\tagmend.exe check-health                  # readiness check
.\.venv\Scripts\tagmend.exe config-set music_path "E:\path\to\music"
.\.venv\Scripts\tagmend.exe config-path                  # where settings.json lives
.\.venv\Scripts\tagmend.exe mcp                          # run MCP server (stdio)
```

Settings file (this machine): `C:\Users\Blake\AppData\Local\tagmend\settings.json`.
SQLite ledger: `C:\Users\Blake\AppData\Local\tagmend\tagmend.sqlite3`.
(`music_path` is set to `E:\music-tag-mender\music` — the full-library working copy; see
the golden rule above.)

## MCP Inspector (from the command line)

The MCP server is `tagmend mcp` (stdio). Test it non-interactively with the
Inspector's **CLI mode** (Node/npx required, both present):

```powershell
$tag = "E:\music-tag-mender\.venv\Scripts\tagmend.exe"

# List tools
npx -y @modelcontextprotocol/inspector --cli $tag mcp --method tools/list

# Call the readiness tool (expect "ok": true, isError: false)
npx -y @modelcontextprotocol/inspector --cli $tag mcp --method tools/call --tool-name check_health
```

For the interactive browser UI, drop `--cli` and the `--method ...`:

```powershell
npx -y @modelcontextprotocol/inspector $tag mcp
```

## How end-users install it (documented for README later)

- CLI: `uv tool install tagmend` → `tagmend …`
- MCP (in a client config): run `uvx tagmend mcp` (or the installed `tagmend mcp`)
- Fallback: `pipx install tagmend`

## Layout

```
src/tagmend/
  log.py            shared logger (use everywhere)
  config.py         settings.json (platformdirs) + typed Settings
  cli.py            Typer CLI (thin)
  mcp_server.py     FastMCP server (thin) — 47 tools
  engine/
    db.py           SQLite connection (WAL)
    schema.py       all DDL + PRAGMA user_version (v25)
    path_keys.py    path identity keys, subtree key ranges, the folder-argument normalizer
    text_keys.py    the shared text fold keys (alnum, display, artist name, loose, title)
    scan.py         filesystem discovery + signatures
    health.py       check_health / readiness + interrupted-commit report
    store.py        pure data access: files/file_tags + tag_revisions[_staged] + tag-axis derived status + mismatch status
    library.py      scan orchestration (3 modes) + stats + list_files/get_file
    tags.py         mutagen read/write of the managed tag set
    versioning.py   tag-revision baseline/append + revert + history
    commits.py      domain-neutral commit core: commits table + RevisionDomain + run_commit
    staging.py      tags domain (TagDomain) + stage/diff/commit_tags orchestration
    lastfm.py       Last.fm top-tags client: lastfm_cache + pacing (getCorrection → M4)
    acoustid.py     fpcalc Fingerprinter + AcoustidClient (gzip POST lookup, paced) + their caches
    release_match.py  pure release-matching helpers (track text keys, positions, disc expectation)
    musicbrainz.py  MusicBrainz client: release-group year, recording lookup, artist-by-MBID name, release-by-MBID tracklist
    axis.py         the parameterized Axis: one outcome-row model for genre/artist/year/song, plus the mismatch axis entry
    axis_status.py  the one set_/reset_<axis>_status implementation, parameterized by Axis
    classify.py     genre vocab/overlay loader + fold-key index + classify.classify_genres (pure)
    genres.py       resolve_genres + set/reset_genre_status
    artists.py      resolve_artists + set/reset_artist_status: MusicBrainz-by-MBID then getCorrection cascade-stage + file_artist_status workflow
    songs.py        resolve_songs + set/reset_song_status: AcoustID folder consensus, anchored check, rebind report, manual release path
    years.py        resolve_years + set/reset_year_status: MusicBrainz originaldate blank-fill + file_year_status workflow
    mismatch.py     detect_mismatches + set/reset_mismatch_status + layout_of + the path gate: tags vs every path level, tiered
    path_text.py    clean_value and the part rules: what a tag value and a path part may hold
    track_conflicts.py  detect_track_conflicts: intra-folder (disc, track) slot collisions
    album_conflicts.py  detect_album_conflicts: intra-folder album-identity splits, tiered
    release_disagreements.py  detect_release_disagreements: tags vs the MusicBrainz release the file's album id names, tiered
    year_disagreements.py  detect_year_disagreements: year tags vs the release group's first-release year, tiered
    album_gaps.py   detect_album_gaps: blank-album files grouped by folder + tiered fill proposals
    parsing.py      pure folder/filename → (artist, album) parsing for the album-gap fills
    naming.py       the naming pattern: grammar, album grouper, renderer (pure)
    path_deviations.py  detect_path_deviations: current path vs the rendered path, plus the discovery header
    paths.py        PathDomain + the path tools + the planner (plan_library, stage_paths, set_naming_pattern): tracked, revertible, no-clobber moves
tests/              pytest; conftest isolates config + builds temp libraries (make_track)
```

## Roadmap pointer

Milestones M0–M6 are in `PLAN.md §14`. Move/rename (organize) design is **§18**;
settings **§19**; logging **§20**; quality gates **§21**.
