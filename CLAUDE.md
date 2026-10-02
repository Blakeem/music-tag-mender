# CLAUDE.md: TagMend working notes

TagMend (`tagmend`) is a CLI and MCP tool that mends the tags and paths of a music library. It
fills genres from Last.fm, normalizes artist names against MusicBrainz and Last.fm, and fills
original release dates from MusicBrainz. It identifies songs by AcoustID fingerprint and stamps a
chosen MusicBrainz release onto a folder. The read-only `detect_*` tools report gaps, conflicts,
disagreements, mismatches and path deviations. A naming pattern renders each file's path from its
tags. Every tag change and every move is tracked per file and revertible.

## Feature map

Each entry names a subsystem, its MCP tools and its modules under `src/tagmend/engine/`.

- Scan and library: `check_health`, `scan_library`, `get_library_stats`, `list_files`,
  `get_file`, `list_artists`, `list_albums`. Modules `health.py`, `scan.py`, `library.py` and
  `store.py`.
- Tag staging: `stage_tags`, `stage_tags_batch`, `unstage_tags`, `diff_tags`, `commit_tags`,
  `history_tags`, `revert_tags`. Modules `staging.py` (`TagDomain`), `versioning.py` and
  `tags.py`. `stage_tags_batch` stages many files in one transaction, always as `manual`.
  `diff_tags` reports `stale_identity` when a change rewrites a name or id and keeps a coupled
  name, id, sort or stamp field.
- Commit ledger: `list_commits`, `get_commit`, `revert_commit`. `commits.py` holds the `commits`
  table, the `RevisionDomain` seam and the crash-safe, resume-free `run_commit` loop.
  `versioning.revert_commit` undoes a tag or path commit as one new `revert` commit.
- Path staging: `stage_paths_batch`, `unstage_paths`, `diff_paths`, `commit_paths`,
  `history_paths`, `revert_paths`. Module `paths.py` (`PathDomain`). A file keeps one `files.id`
  across every move. The next `commit_paths` finishes a move that landed before a crash.
- Sidecars: a folder's non-audio files (cover art, `.cue`, `.log`, `Scans/`) follow its audio
  under their own names when every audio file of the folder moves to one new folder.
  `sidecar_moves` logs each sidecar move. A release folder above disc folders
  (`mismatch.layout_of`) that never held audio directly carries its own files and audio-free
  subfolders when every disc folder still holding audio moves to one folder. A release folder
  whose discs move in two commits sends its art with the later disc. `commit_paths` lists in
  `sidecars_held` each non-audio file left with no staged move in a folder the commit's audio
  left.
- Naming pattern and path deviations: `set_naming_pattern`, `detect_path_deviations`,
  `stage_paths`. `naming.py` holds the grammar, the album grouper, the renderer and
  `DEFAULT_PATTERN`. The settings keys are `naming_pattern` (empty for the default) and
  `container_folders`. `paths.plan_library` is the one planner. It applies every hold.
  `diff_paths` flags an `auto` row `stale` when its render moved.
- Tag axes (genre, artist, year, song): `resolve_<axis>s`, `set_<axis>_status` and
  `reset_<axis>_status`, 12 tools. `axis.py` defines each `Axis`, `MISMATCH_AXIS` included.
  `axis_status.py` is the one set and reset implementation. `axis_resolver.py` runs the group
  lookups of genre and year.
  - Genre: `genres.py` and `classify.py`. Last.fm top tags are matched by fold key to the
    MusicBrainz genre vocabulary (`src/tagmend/data/genre_vocabulary.yml`). The overlay
    (`src/tagmend/data/genre_overlay.yml`) adds genres and aliases. Its `deny:` rules drop a
    genre for named artists or for every artist. `classify.classify_genres` is pure.
  - Artist: `artists.py`. A value whose file carries a MusicBrainz artist id is settled against
    that artist's canonical name or a registered alias. MusicBrainz casing is trusted. Last.fm
    `artist.getCorrection` sees only the values left over, since Last.fm casing is not trusted.
    A correction is cascade-staged across `artist`, `albumartist` and each `artists` element,
    each field with its own id field. Only the MusicBrainz tier also writes the sort field,
    since Last.fm publishes no sort name. A name MusicBrainz records under neither form, or a
    value the library pairs with two ids, is held in `name_id_disagreement`.
  - Year: `years.py`. It blank-fills `originaldate` with the full first-release date of the
    album's MusicBrainz release group.
  - Song: `songs.py` and `release_match.py`. Every file of a folder holding a `pending` file
    votes by AcoustID fingerprint. A folder whose files all carry `musicbrainz_albumid` is
    checked against those releases. A folder without ids converges on the one Official release
    most of its files share and fills blank `title`, `tracknumber` and `discnumber` as `auto`.
    A folder holding a pending file whose audio is off its tagged release is reported in
    `rebind_folders` with ranked candidates. The first five candidates are placed as the manual
    release path would place the folder. A settled file (`done`, `manual` or staged) off its
    release never sends its folder to `rebind_folders`.
    `resolve_songs(release_mbid=..., assignments=...)` stages the entire release stamp as one
    `manual` batch, `artists` and the release-group fields included. Every file in scope must
    land on exactly one track, or nothing is staged.
- Mismatch gate: `detect_mismatches`, `set_mismatch_status`, `reset_mismatch_status`.
  `mismatch.py` compares the tags with every path level and records one path decision per file
  in `file_mismatch_status`. `legit_ignore` keeps the folder. `misfiled_deferred` renders every
  level from the tags. No decision keeps a filename. The path tools read `gate_state`,
  `check_files` and `planner_keep`. `path_text.py` holds what a tag value or a path part may
  contain.
- Detectors: `detect_album_gaps` (`album_gaps.py`, `parsing.py`), `detect_track_conflicts`
  (`track_conflicts.py`), `detect_album_conflicts` (`album_conflicts.py`),
  `detect_release_disagreements` (`release_disagreements.py`) and `detect_year_disagreements`
  (`year_disagreements.py`). They share `detector_core.py`. They write nothing but lookup cache
  rows.
- Lookup clients: `lastfm.py` fetches top tags and `artist.getCorrection`. `musicbrainz.py`
  looks up a release group's first-release date, a recording, an artist by MBID and a release
  by MBID. `acoustid.py` holds the fpcalc `Fingerprinter` and the `AcoustidClient`, which sends
  a gzip POST lookup. `lookup_clients.py` holds the injected-or-owned client seam and the one
  `PacedHttp` transport every client sends through. `PacedHttp` paces, retries and decodes JSON
  objects. Each client caches its answers in its own `*_cache` tables.
- Result serialization: `serialize.py`. `FieldDict` gives a result dataclass a `to_dict` that
  serializes its fields in field order. A class whose payload renames, omits, rounds or adds a
  key keeps its own `to_dict`.

## Invariants

- `tag_revisions`, `path_revisions` and `sidecar_moves` are append-only. Triggers abort every
  `UPDATE` or `DELETE` on them, the cascade from `files` included.
- `schema.apply_schema` upgrades an older ledger in place and refuses a newer one.
- Every forward change to a music file or its path is staged, reviewed with `diff_tags` or
  `diff_paths`, and written by `commit_tags` or `commit_paths`. A resolver or `stage_*` tool only
  stages. `revert_tags`, `revert_paths` and `revert_commit` write their restore directly, each as
  its own `revert` commit.
- Every commit is revertible. A resolver's real run and `revert_commit` are refused while
  anything is staged.
- `commit_tags` refuses a file whose size or mtime changed since staging (`changed_since_stage`),
  unless disk already holds the target. A commit's origin is `auto` only when every row it sweeps
  is `auto`.
- The four tag axes share one outcome-row model. A `file_<axis>_status` row records `done`,
  `no_match` or `manual` with a snapshot of the axis identity and of the field value it settled.
- `store.derived_status` returns the first status that holds of `staged`, `manual`,
  `no_identity` and the row's own status, else `pending`. `staged` means a staged change alters
  the axis fields. The row's own status holds only while both snapshots match the tags. A
  revert, an outside edit, an unstage or an identity change therefore re-opens a `done` or
  `no_match` file with no writer.
- Every resolver writes `done`. Every resolver but `resolve_songs` also writes `no_match`. The
  commit writer records `manual` for a committed human change to an axis field, keyed on
  `tag_revisions_staged.changed_fields`, so that a commit re-applied after a crash still records
  it. `manual` is sticky until `reset_<axis>_status`.
- `tags.py` owns the canonical tag namespace, since mutagen's easy layer is incomplete. A read
  accepts every known spelling and prefers the container-native one. A write emits the native
  one and drops every alternate, so a file never carries two values for one concept.
- mutagen's easy layer gets three containers wrong, so `tags.py` registers its own names.
  `EasyID3` maps `albumartistsort` to a `TXXX` frame Picard never writes, so `tags.py` maps it to
  `TSO2`. `EasyMP4` freeform atom names are case-sensitive. Its `releasecountry` atom is a word
  off from Picard's `MusicBrainz Album Release Country`. Vorbis has no easy layer, so FLAC and
  Ogg names pass through raw. `_VORBIS_SPELLINGS` maps the names that differ (`releasetype`,
  `releasestatus`).
- Two tag names are collapsed only after measuring that they never disagree in the wild.
  `organization` and `label` stay unmapped and unmanaged, since 245 real FLACs hold a different
  label in each.
- `MANAGED_TAGS` holds 26 fields (managed set 4). `MANAGED_SETS` keeps every older set frozen,
  since stored revisions point at them. A widening adds a new entry and bumps
  `MANAGED_SET_VERSION`. Staging and revert first append a `scan` re-baseline
  (`versioning.observe_widened_fields`) to a file whose latest revision predates the current set.
  A snapshot covers only its own set's fields. The commit refuses a row staged before the
  re-baseline. A drift-free re-baseline never blocks `revert_commit`. A revert takes a field its
  target never governed from the first later revision that governs it when that revision is a
  re-baseline. Otherwise it keeps the current value.
- `artists` is the Picard ARTISTS list Navidrome links artists from. It is `TXXX:ARTISTS` on
  ID3, the `ARTISTS` freeform atom on MP4 and `ARTISTS` on Vorbis.
- Any change to what `read_tags` produces bumps `TAG_READER_VERSION` in the same commit, so that
  the next incremental scan re-reads each stale row once.
- Every write verifies its temp copy before the atomic swap and raises `TagWriteError` on any
  difference. The check covers the audio payload hash (except on Ogg), every unmanaged entry,
  ID3v1 and APEv2 presence, and a read-back of every managed key.
- The writer refuses a container the verifier has no layout for and a non-Ogg file whose audio
  payload it cannot locate. It also refuses an ID3 file whose v2.4 save would drop an unmanaged
  frame, such as `RVAD` or `NCON`. `tags.ensure_writable` makes staging refuse the same files.
- A move never overwrites a target. TagMend never deletes a library file. A commit prunes only
  the folders its moves empty.
- A sidecar waits until every audio file under its album folder has moved. Of two folders whose
  sidecars claim one target, the folder whose audio holds the lower file id wins.
  `sidecars_held` reports the other sidecar.
- `path_keys.py` decides when two paths name the same file. Every folder argument resolves
  through `path_keys.resolve_folder_arg`.
- A real `stage_paths` or `stage_paths_batch` call is refused until `detect_mismatches` reads
  `gate_open: true`. The gate opens when nothing is flagged and no exception is undecided. A
  batch that only confirms landed moves skips the gate. `commit_paths` checks each staged file
  again at its current path.
- A rendered path never flags in `detect_mismatches`. A round-trip test checks it.
- Never log an API key. `log.py` holds the `httpx` and `httpcore` loggers at WARNING, since their
  INFO lines carry the Last.fm key in the request URL.
- Unit tests never touch `music/` and use generated files only. Snapshot tests use `tmp_path` or
  the `temp_library` fixture. Real-audio tests use `make_track`, which copies a silent `.mp3`,
  `.flac`, `.m4a` or `.ogg` template and writes tags.

## Python

- **Target Python 3.12** (`requires-python = ">=3.12"`). Write 3.12 code: built-in
  generics (`list[str]`, `dict[str, object]`), `X | None` unions, `pathlib`, modern
  typing. The `py` launcher default and the venv are both 3.12.

## Golden rules

- **Use the logger, never `print`.** `from tagmend.log import get_logger` →
  `logger = get_logger(__name__)`. Use lazy `%`-style args (`logger.info("x=%s", x)`),
  not f-strings. User-facing CLI text uses `typer.echo` (that's output, not logging).
  In the MCP server, logs go to **stderr only**. Stdout is the JSON-RPC channel.
- **Engine holds the logic. CLI/MCP stay thin.** New behavior goes in
  `tagmend/engine/*`, then gets a thin CLI subcommand and/or MCP tool.
- **Settings live on disk, not in env.** The MCP server can't see the CLI's shell.
  Read config via `tagmend.config.load_settings()`. Never read env/JSON directly.
- **`music/` is the live-testing sandbox.** It is a full **copy** of Blake's real 135 GB,
  11,233-file library. The original is `E:\Music`, which stays untouched and can be re-copied
  anytime. Live scans, resolver runs, fix runs and problem discovery run against this copy, so
  that everything is proven before the mended copy replaces the real library (ROADMAP B3). The
  folder is gitignored and copyrighted.

## Tool naming

Every MCP tool is `verb_object[_qualifier]`, lowercase snake_case, **verb always first, no
exceptions**. The verb names the operation. The object names the domain, axis, entity, or finding
(compound nouns allowed: `album_gaps`, `library_stats`). MCP is the primary surface. The CLI
carries only the four commands worth running by hand (`check-health`, `scan-library`,
`get-library-stats`, `detect-mismatches`), and each mirrors its MCP name with `-` for `_`. A new
tool needs no CLI command. Adding one means mirroring the MCP name exactly, with no aliases.
CLI-only program commands (`mcp`, `version`, `config*`) sit outside the grammar.

**The verb set is closed.** A tool is **mutating** if it changes any persisted state other than the
snapshot mirror (`files`/`file_tags`): staged rows, commits, status rows, or the music
files themselves.

- Observing: `check, scan, list, get, detect, diff, history` (`diff`/`history` are git-style nouns
  in the verb slot. `scan` refreshes only the snapshot mirror)
- Mutating: `stage, unstage, commit, revert, resolve, set, reset`

A verb may span two call/return shapes when the object disambiguates (`get_file` vs
`get_library_stats`, and `revert_tags` vs `revert_commit`: the commit ledger is domain-neutral).
A new verb requires an operation no existing verb covers.

| Shape | Template |
|---|---|
| readiness / ingest | `check_health` · `scan_library` |
| enumerate / fetch | `list_<plural>` · `get_<singular>` · `get_library_stats` |
| read-only findings | `detect_[<field>_]<plural finding noun>` |
| stage→commit cycle | `stage_/unstage_/diff_/commit_/history_/revert_<domain>` (domains `tags`, `paths`) |
| atomic multi-target | `<stage-verb>_<domain>_batch` (`_batch` reserved, reusable) |
| commit ledger | `list_commits` · `get_commit` · `revert_commit` (bare: one `commits` table, no domain column) |
| lookup → stage | `resolve_<axis>s` |
| axis status | `set_<axis>_status` / `reset_<axis>_status` |
| setting write | `set_<setting>` (`set_naming_pattern`) |

Rules, in order:

1. All read-only findings reports are ONE `detect_*` family, defined by call shape, never split by
   problem class or by whether a disposition table exists.
2. Reports name the **finding**, state ops name the **domain** (`stage_paths`, never `stage_moves`).
   One distinct plural finding noun per comparison (glossary below). Qualify with the field when the
   finding lives in exactly one (`album_gaps`, `year_disagreements`, `path_deviations`). Qualify with
   the thing compared when the finding spans several fields AND a sibling tool shares the finding
   noun (`track_conflicts` compares track slots, `album_conflicts` compares album identity). Leave it
   bare when it spans fields and nothing shares the noun (`mismatches`). Reuse the repo's word
   for that concept if one exists. Otherwise coin exactly one noun and add it to the glossary
   in the same commit. A token that already means something else in the repo is not a reuse.
3. Prefer an existing verb over a new one.
4. **Tool identity:** a report is one tool per (left source, right source, conformance criterion)
   triple. More fields on the same triple = body change, same name. A new comparison or a third
   input (e.g. the naming pattern) = a new tool.
5. Number is fixed by slot: axis singular in `set_/reset_<axis>_status`, plural in
   `resolve_<axis>s`. Domain and finding nouns always plural.
6. The `<axis>` token equals `Axis.name` in `engine/axis.py`, exactly. Renaming an axis is a code
   change first (`Axis.name` + `file_<name>_status` via `ALTER TABLE … RENAME TO` + the engine
   module), tool rename second, never one without the other.
7. One concept = one term, both directions, across tool names, CLI commands, engine
   module/function/class names, `Axis.name` values, status tables, and prose.

Glossary (the comparison behind each finding noun): `mismatch` = tags ↔ path (folders and
filename) · `gap` = tag ↔ absent · `disagreement` = tag ↔ external source (MusicBrainz) ·
`conflict` = tag ↔ sibling tags in the same folder (coined) · `deviation` = current path ↔
canonical path generated from tags by the naming pattern (coined).

## Quality gates: all four must pass before anything is "done"

Run from the repo root (venv at `.venv`):

```powershell
.\.venv\Scripts\ruff.exe check .          # lint (near-all rules; see pyproject)
.\.venv\Scripts\ruff.exe format .         # autoformat (use --check in CI)
.\.venv\Scripts\mypy.exe                  # strict static typing
.\.venv\Scripts\pytest.exe                # tests
```

Lint, format, types, and tests are **required** every change. Ruff is configured with
`select = ["ALL"]` minus a few formatter-conflicting rules. Fix issues, don't
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
(`music_path` is set to `E:\music-tag-mender\music`, the full-library working copy. See
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

## End-user install

- CLI: `uv tool install tagmend` → `tagmend …`
- MCP (in a client config): run `uvx tagmend mcp` (or the installed `tagmend mcp`)
- Fallback: `pipx install tagmend`

## Layout

```
src/tagmend/
  log.py            shared logger (use everywhere)
  config.py         settings.json (platformdirs) + typed Settings
  cli.py            Typer CLI (thin)
  configui.py       loopback config web UI that edits settings.json
  mcp_server.py     FastMCP server (thin), 46 tools
  data/             genre_vocabulary.yml, genre_overlay.yml, web/ (the config UI page)
  engine/
    db.py           SQLite connection (WAL)
    schema.py       all DDL + PRAGMA user_version (v27)
    path_keys.py    path identity keys, subtree key ranges, the folder-argument normalizer
    text_keys.py    the shared text fold keys (alnum, display, artist name, loose, title)
    clock.py        the engine's one source of the current time
    validation.py   argument checks shared by the engine entry points
    serialize.py
    scan.py         filesystem discovery + signatures
    health.py       check_health / readiness + interrupted-commit report
    store.py        pure data access: files/file_tags + tag_revisions[_staged] + tag-axis derived status + mismatch status
    library.py      scan orchestration (3 modes) + stats + list_files/get_file
    tags.py         mutagen read/write of the managed tag set
    versioning.py   tag-revision baseline/append + revert + history
    commits.py
    staging.py
    lastfm.py
    acoustid.py
    release_match.py  pure release-matching helpers (track text keys, positions, disc expectation)
    musicbrainz.py
    lookup_clients.py
    axis.py
    axis_status.py
    axis_resolver.py
    classify.py
    genres.py
    artists.py
    songs.py
    years.py
    mismatch.py
    path_text.py
    detector_core.py
    track_conflicts.py
    album_conflicts.py
    release_disagreements.py
    year_disagreements.py
    album_gaps.py
    parsing.py
    naming.py
    path_deviations.py  detect_path_deviations: current path vs the rendered path, plus the discovery header
    paths.py
tests/              pytest; conftest isolates config + builds temp libraries (make_track)
```

## Roadmap pointer

`ROADMAP.md` lists the remaining work in order. `PLAN.md` holds the design. Its §14 lists the
milestones M0 to M6. §18 to §21 cover moves and renames, settings, logging and quality gates.
