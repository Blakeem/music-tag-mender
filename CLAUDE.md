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
tool + the `file_genre_status` workflow: `no_match`/`manual`/pending-by-absence). M3.5 shipped
too: `revert_commit` group undo (skip+report, empty-staging guard, dry-run; every
revert — even per-file `revert_tags` — is now its own `origin='revert'` commit) and
genre-status visibility (`list_files(genre_status=...)` filter + `get_library_stats`
genre counts via `store.derived_genre_status`, the mirror of `genres._select`).
M4 phase 1 shipped too: `artists.py` (`resolve_artists` — cascade-stages the
`artist.getCorrection` canonical name + MBID across `artist`/`albumartist`, with
feat/sentinel/empty + per-file multi-value guards, dry-run, and the empty-staging
precondition; results cache in the existing `lastfm_cache`). M4 phase 2 shipped: the
artist-axis twin of the genre-status workflow — a sticky per-file `file_artist_status`
(`manual` exclusion only; **no** `no_match` state) that `resolve_artists` always skips
(`skipped_manual`/`manual_files`); `set_artist_status`/`reset_artist_status` (scope by
file or by a value matched across BOTH `artist` and `albumartist`);
`list_files(artist_status=...)` + a `get_library_stats['artist']` block. Both axes'
`staged`/`done` are now **field-aware** (`store.has_staged_change_for` /
`has_auto_change_for`, the latter via SQLite JSON1 `json_extract` on the committed
`diff`): genre keys on `genre`, artist on `artist`/`albumartist`, so the two columns are
independent (a genre-only commit no longer reads as artist-`done`, and vice versa).
The year axis (`years.py`) shipped next (MusicBrainz `originaldate` blank-fill + sticky
`manual`/engine `no_match`, `list_files(year_status=...)` + a `get_library_stats['year']`
block). The **mismatch-fix** surface shipped last (decide run `fix-mismatches`, Run 2):
`detect_mismatches` gained sticky per-file dispositions (`file_mismatch_status` —
`legit_ignore`/`misfiled_deferred`, snapshot-and-go-stale), grouped output (`group=True`) +
exact-folder expansion + a staleness-aware skip-filter; `set_mismatch_status`/
`reset_mismatch_status`; `stage_tags_batch` (one atomic multi-file stage, always
`origin="manual"`); and `reopen_axes(commit_id)` — the first caller of
`store.void_auto_changes`, re-opening a fixed file's derived genre/year axes + clearing its
stale artist status. `list_files(mismatch_status=...)` + a `get_library_stats['mismatch']` block
round it out. The `detect_album_gaps` tool (`album_gaps.py` + the pure, standalone
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
`_build_target` now writes each field's own id field, so an `albumartist`-only correction no
longer overwrites `musicbrainz_artistid`. 32 MCP tools total. Schema is **v15** (additive: v11 adds
`musicbrainz_recording_cache`, v12 renames `file_album_status` → `file_year_status` in place —
dispositions preserved; v13 adds `tag_revisions.managed_set`, stamping which managed-tag set
governed each revision so a revert can restore emptiness on the widened fields; v14 adds
`files.reader_version` so an incremental scan re-reads a row an older tag reader wrote. v15 adds
`musicbrainz_artist_cache`, the by-MBID artist lookup's cache. An
older ledger upgrades in place). M6 organize/paths (`paths.py`)
is a paper sketch (its DDL ships in v6; logic deferred).

**The canonical tag namespace is TagMend's, not mutagen's.** mutagen's "easy" layer is an
incomplete normalizer, so `tags.py` owns the mapping wherever it is wrong: `EasyID3` points
`albumartistsort` at a `TXXX` frame Picard never writes (it uses `TSO2`), `EasyMP4` freeform atom
names are case-sensitive and its `releasecountry` atom is a word off from Picard's, and Vorbis has
**no easy layer at all** so FLAC/OGG names pass through raw. Read accepts every known spelling and
prefers the container-native one; write emits the native one and drops the alternate, so a file
never carries two contradicting values for one concept. Collapse a pair ONLY after measuring that
the two names never disagree in the wild — `organization`/`label` is left unmapped and unmanaged
because 245 real FLACs hold a different label in each. **`MANAGED_TAGS` is 25** (5 original + 13
identity + 7 release-stamp = managed-set version 3; `MANAGED_SETS` keeps every older set frozen
because stored revisions point at them). Any change to what `read_tags` produces bumps
`TAG_READER_VERSION` in the same commit, which is what makes the next incremental scan re-read a
stale row exactly once.

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
(compound nouns allowed: `album_gaps`, `library_stats`). The CLI mirrors the MCP name with `-` for
`_` (`check_health` → `tagmend check-health`); no aliases. CLI-only program commands (`mcp`,
`version`, `config*`) sit outside the grammar.

**The verb set is closed.** A tool is **mutating** if it changes any persisted state other than the
snapshot mirror (`files`/`file_tags`): staged rows, commits, status rows, watermarks, or the music
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

Rules, in order:

1. All read-only findings reports are ONE `detect_*` family — defined by call shape, never split by
   problem class or by whether a disposition table exists.
2. Reports name the **finding**, state ops name the **domain** (`stage_paths`, never `stage_moves`).
   One distinct plural finding noun per comparison (glossary below); qualify with the field when the
   finding lives in exactly one (`album_gaps`, `year_disagreements`, `path_deviations`), bare when
   it spans fields (`mismatches`). Reuse the repo's word for that concept if one exists; otherwise
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

Glossary — the comparison behind each finding noun: `mismatch` = tags ↔ folder path · `gap` = tag ↔
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
  mcp_server.py     FastMCP server (thin) — 32 tools
  engine/
    db.py           SQLite connection (WAL)
    schema.py       all DDL + PRAGMA user_version (v15)
    scan.py         filesystem discovery + signatures
    health.py       check_health / readiness + interrupted-commit report
    store.py        pure data access: files/file_tags + tag_revisions[_staged] + genre/artist status
    library.py      scan orchestration (3 modes) + stats + list_files/get_file
    tags.py         mutagen read/write of the managed tag set
    versioning.py   tag-revision baseline/append + revert + history
    commits.py      domain-neutral commit core: commits table + RevisionDomain + run_commit
    staging.py      tags domain (TagDomain) + stage/diff/commit_tags orchestration
    lastfm.py       Last.fm top-tags client: lastfm_cache + pacing (getCorrection → M4)
    musicbrainz.py  MusicBrainz client: release-group year, recording lookup, artist-by-MBID name
    classify.py     genre vocab/overlay loader + fold-key index + classify.classify_genres (pure)
    genres.py       resolve_genres orchestration + file_genre_status workflow
    artists.py      resolve_artists + set/reset_artist_status: MusicBrainz-by-MBID then getCorrection cascade-stage + file_artist_status workflow
    track_conflicts.py  detect_track_conflicts: intra-folder (disc, track) slot collisions
    years.py        resolve_years + set/reset_year_status: MusicBrainz originaldate blank-fill + file_year_status workflow
    paths.py        STUB + PathDomain paper sketch — M6 (organize/paths)
tests/              pytest; conftest isolates config + builds temp libraries (make_track)
```

## Roadmap pointer

Milestones M0–M6 are in `PLAN.md §14`. Move/rename (organize) design is **§18**;
settings **§19**; logging **§20**; quality gates **§21**.
