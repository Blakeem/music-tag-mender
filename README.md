# TagMend

TagMend cleans up the tags of your music library and organizes its files. It fills genres from **Last.fm**, normalizes artist names against **MusicBrainz** and Last.fm, fills original release dates from MusicBrainz, and identifies songs by their audio with **AcoustID**. Every change is staged first and committed as a revertible unit. Nothing touches your files until you say so. Any change can be rolled back. It ships as both a command-line tool and an MCP server, so you can drive it yourself or hand it to an AI assistant like Claude. Built to make [Navidrome MCP](https://github.com/Blakeem/Navidrome-MCP) more useful by giving it accurate names and genres.

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Tools](#tools)
- [Development](#development)
- [License](#license)

## Features

### 🎵 Genre cleanup (Last.fm)

Pull community top-tags for each artist (optionally each album), fold them through a curated genre vocabulary, and stage clean, consistent genres. A deny rule in the genre overlay keeps a genre off the files of the artists it names, or off every file. Per-file controls let you re-run, skip, or re-queue specific tracks.

### 🎤 Artist-name normalization (MusicBrainz + Last.fm)

Resolve name variants to a single canonical spelling across `artist`, `albumartist` and each element of the multi-value `artists` tag. A file that already carries a MusicBrainz artist ID is settled by a direct lookup of that ID, against the artist's canonical name and registered aliases. A rewritten name carries MusicBrainz's sort name to its own sort field, `artistsort` for `artist` and `albumartistsort` for `albumartist`. Values with no ID fall through to Last.fm `artist.getCorrection`. That tier leaves the sort field alone, because Last.fm publishes no sort name. Feat/sentinel/empty values and a multi-value `artist` or `albumartist` field are guarded so nothing ambiguous gets rewritten. A name that disagrees with the ID its own file carries is reported, never rewritten.

### 📅 Year fill (MusicBrainz)

Blank-fill each album's original release date (`originaldate`) from MusicBrainz without overwriting values you already have.

### 🎧 Song identification (AcoustID + MusicBrainz)

Fingerprint each file and look it up on AcoustID to find the release its audio is on. A folder whose files agree on one release gets its blank `title`, `tracknumber` and `discnumber` filled. A folder whose audio is not on the release its tags name is reported with ranked candidate releases. Stamping a folder onto the release you choose writes that release's names, ids, dates and numbers onto every file. A file the audio cannot place takes the track you name for it.

### 🧭 Consistency reports

Read-only reports find files that share a track slot in one folder, folders whose files describe different albums, tags that contradict the MusicBrainz release a file names, and years that contradict the album's first release.

### 🔍 Mislabeled-file detection

Find files whose path disagrees with their own tags with `tagmend detect-mismatches` or the `detect_mismatches` MCP tool. The report compares the top folder, the release folder and its year, a disc subfolder, and the filename's track number and title with the tags. Formatting alone never flags. The report is read-only and tiered high, medium or low.

### 🕳️ Blank-album gap detection

Find files carrying no `album` tag at all (invisible to every album-scoped tool) with the `detect_album_gaps` MCP tool. Groups them by folder and proposes grounded fills (unanimous folder mates, the parsed folder name, or a review-only MusicBrainz recording lookup). A read-only report. Proposals only ever fill blanks, never overwrite.

### ↩️ Fully revertible history

A git-like flow: stage → commit → revert. Files are only written on commit, each change is recorded in an append-only per-file log, and any single file or whole commit can be undone. Reverts are themselves tracked commits.

### 📁 File organization

File moves are opt-in. Each file moves to the path a naming pattern builds from its tags. Moves are staged and committed like tag edits. A move never overwrites a file. Cover art, cue sheets and other non-audio files move with their album.

### 🗂️ Per-file status workflow

Mark files as `manual` to exclude them from an axis (genre, artist, year, or song), or reset them to `pending`. A `manual` mark stays until you reset it. A file a resolver settled returns to `pending` on its own when a tag it was settled on changes.

### 🎚️ Multi-format and engine-first

Reads and writes MP3, FLAC, M4A, and OGG through mutagen. All logic lives in one engine. The CLI and MCP server are thin wrappers over it. Settings live in a single on-disk `settings.json`, edited through a loopback-only browser form with a built-in Last.fm key test, so there are no env vars or hand-edited JSON to manage.

## Installation

### Prerequisites

- **Python 3.12+**
- **A free Last.fm API key** ([create one](https://www.last.fm/api/account/create))
- **Optional, for the song axis (`resolve_songs`) only:**
  - **A free AcoustID application API key** ([register an application](https://acoustid.org/new-application))
  - **fpcalc** from [Chromaprint](https://acoustid.org/chromaprint), on `PATH` or set in `fpcalc_path`
- **Optional: an MCP client** (Claude Desktop, Claude Code, Cursor, or another client with local stdio support) to use the MCP server

### Install

From source:

```bash
git clone https://github.com/Blakeem/music-tag-mender.git
cd music-tag-mender
pip install -e .
```

Once published, install it as a standalone tool:

```bash
uv tool install tagmend
# or
pipx install tagmend
```

### Configure

Open the settings page, enter your music folder and Last.fm key, test it, then save:

```bash
tagmend config
```

This starts a loopback-only web server on `127.0.0.1:<random-port>`, prints the URL, and opens your browser. Settings are written to an on-disk `settings.json` (see [`settings.example.json`](settings.example.json) for every supported key). You can also set keys directly:

```bash
tagmend config-set music_path "E:\path\to\music"
tagmend config-path     # show where settings.json lives
tagmend check-health    # readiness check
```

CLI commands: `check-health`, `scan-library`, `get-library-stats`, `detect-mismatches`, `config`, `config-set`, `config-path`, `mcp`, `version`.

### Use as an MCP server

Run the server over stdio:

```bash
tagmend mcp
```

In an MCP client config:

```json
{
  "mcpServers": {
    "tagmend": {
      "command": "tagmend",
      "args": ["mcp"]
    }
  }
}
```

If `music_path` or your Last.fm key is missing on launch, TagMend auto-opens the settings page in the background and keeps serving normally. Two environment switches control this behavior:

- `TAGMEND_NO_BROWSER` starts the server but does not open a browser window.
- `TAGMEND_NO_CONFIG_UI` never auto-launches the settings page from `tagmend mcp`.

Edits apply on the next tool call. Every command and MCP tool re-reads `settings.json` fresh (there is no in-process settings cache).

## Tools

The MCP server exposes 46 tools. Tag edits and file moves are staged first and written to disk only by `commit_tags`, `commit_paths` or a revert tool. Every change is revertible.

### Core & Library

| Tool | Description |
|------|-------------|
| `check_health` | Verify TagMend is ready to use |
| `scan_library` | Scan a music folder into the snapshot database (reads files, never writes them) |
| `get_library_stats` | Report library-wide snapshot counts |
| `list_files` | List tracked files with their current managed tags (to discover file ids) |
| `get_file` | Return one tracked file with its managed tags, by stable `file_id` |
| `detect_mismatches` | Detect files whose folders or filename disagree with their own tags (read-only report) |
| `detect_album_gaps` | Find files with a blank `album` tag, grouped by folder, with tiered fill proposals (read-only report) |
| `detect_track_conflicts` | Find files sharing a `(disc, track)` slot with a folder sibling, tiered by how the titles and containers compare (read-only report) |
| `detect_album_conflicts` | Find files whose album identity differs from their folder siblings', tiered by whether a release ID, a name or year, or a disc suffix splits the folder (read-only report) |
| `detect_release_disagreements` | Find files whose tags contradict the MusicBrainz release their own `musicbrainz_albumid` names, reporting the value the release says each tag should hold (read-only report) |
| `detect_year_disagreements` | Find files whose `originaldate` names a different year than the first release of their MusicBrainz release group, or whose `date` is earlier than that first release (read-only report) |

### Staging & Commits

| Tool | Description |
|------|-------------|
| `stage_tags` | Stage a managed-tag change for one file (the git "index"). Writes nothing to disk |
| `stage_tags_batch` | Stage managed-tag changes for many files in one atomic, all-or-nothing call |
| `unstage_tags` | Remove a pending staged change for one file |
| `diff_tags` | Show staged-but-uncommitted changes, enriched with the current to target diff |
| `commit_tags` | Apply all staged tag changes to disk as one revertible commit |
| `list_commits` | List commits newest first (the revertible units that group tag changes) |
| `get_commit` | Return one commit by id and the revision logs that hold its changes |

### History & Revert

| Tool | Description |
|------|-------------|
| `history_tags` | Show the append-only tag-revision log for one file, oldest first |
| `revert_tags` | Restore a file's managed tags to a prior version (append-only, revertible) |
| `revert_commit` | Undo an entire commit as a unit. Every file it changed goes back to its pre-commit tags or path |

### Genre (Last.fm)

| Tool | Description |
|------|-------------|
| `resolve_genres` | Look up Last.fm genres for in-scope files and stage the result (writes nothing to disk) |
| `set_genre_status` | Exclude files from genre tagging (`manual`). The exclusion is sticky until `reset_genre_status` |
| `reset_genre_status` | Clear any genre status row for in-scope files, returning them to `pending` |

### Artist Names (MusicBrainz + Last.fm)

| Tool | Description |
|------|-------------|
| `list_artists` | List distinct `artist` values with file counts (to scope a run) |
| `resolve_artists` | Normalize artist names against MusicBrainz (by the ID the file carries), then Last.fm `getCorrection`, and stage the result (no disk write) |
| `set_artist_status` | Exclude files from artist-name normalization (`manual`). The exclusion is sticky until `reset_artist_status` |
| `reset_artist_status` | Clear any artist status row for in-scope files, returning them to `pending` |

### Year (MusicBrainz)

| Tool | Description |
|------|-------------|
| `list_albums` | List distinct album groups with file counts and status (to scope a run) |
| `resolve_years` | Blank-fill the original release date (`originaldate`) from MusicBrainz (no disk write) |
| `set_year_status` | Exclude files from the year fill (`manual`). The exclusion is sticky until `reset_year_status` |
| `reset_year_status` | Clear any year status row for in-scope files, returning them to `pending` |

### Songs (AcoustID + MusicBrainz)

| Tool | Description |
|------|-------------|
| `resolve_songs` | Blank-fill `title`, `tracknumber` and `discnumber` from the release each file's audio is on. Report each folder whose audio is on another release, with ranked candidates placed on its tracks. `release_mbid` stamps the folder onto that release. `assignments` name the track for each file the audio cannot place. No disk write. |
| `set_song_status` | Exclude files from the song fill (`manual`). The exclusion is sticky until `reset_song_status` |
| `reset_song_status` | Clear any song status row for in-scope files, returning them to `pending` |

### Mismatch fixing

Use `detect_mismatches` (above) to find files whose path disagrees with their own tags. Fix a wrong tag through the staging engine (`stage_tags_batch` → `commit_tags`), or record a path decision for the group. `legit_ignore` keeps the folder. `misfiled_deferred` lets the tags render every level of the path. No decision keeps a filename, so write a wording you want to keep into the tag. A decision covers the names its group flags. The file flags again when a covered tag changes or a committed move changes its path. The report reads `gate_open: true` once every group is fixed or decided. Set the container folders with `set_naming_pattern` before the first decision, since `detect_mismatches` reads that list.

| Tool | Description |
|------|-------------|
| `set_mismatch_status` | Record a path decision for one group. `legit_ignore` keeps its folder, and `misfiled_deferred` renders every level from the tags |
| `reset_mismatch_status` | Delete the path decision of in-scope files |

### File Moves

A naming pattern renders each file's path from its tags. A move is staged from that render or with an explicit destination, and is committed as one revertible commit. Staging needs `detect_mismatches` to read `gate_open: true`. Each file keeps its id across moves.

| Tool | Description |
|------|-------------|
| `set_naming_pattern` | Save the naming pattern and the container folder list |
| `detect_path_deviations` | Report files whose path differs from the path their tags render. A candidate pattern is shown without being saved |
| `stage_paths` | Stage every file under a folder to the path its tags render. A folder moves as a unit or not at all |
| `stage_paths_batch` | Stage explicit destinations for many files in one atomic, all-or-nothing call |
| `unstage_paths` | Drop the staged moves of one file or of every file under a folder |
| `diff_paths` | Show the staged moves and where each file sits on disk now |
| `commit_paths` | Move every staged file as one revertible commit and remove the folders it empties |
| `history_paths` | Show every location one file has had, oldest first |
| `revert_paths` | Move one file back to the location of a prior path version |

## Development

Install the dev extras, then run the four quality gates from the repo root. Lint, format, types, and tests must all pass for any change.

```bash
pip install -e ".[dev]"
ruff check .            # lint
ruff format --check .   # format
mypy                    # strict static typing
pytest                  # tests
```

All logic lives in the engine (`src/tagmend/engine/`). The CLI (`cli.py`) and MCP server (`mcp_server.py`) are thin wrappers. See `CLAUDE.md` for working notes and `PLAN.md` for the full design.

Test the MCP server non-interactively with the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx -y @modelcontextprotocol/inspector --cli tagmend mcp --method tools/list
npx -y @modelcontextprotocol/inspector --cli tagmend mcp --method tools/call --tool-name check_health
```

## License

MIT. See [LICENSE](LICENSE).
