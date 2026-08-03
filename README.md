# TagMend

TagMend cleans up the **genre** and **artist-name** tags in your music library using **Last.fm**, and fills in album original-release years from **MusicBrainz**. Every change is staged first and committed as a revertible unit, so nothing touches your files until you say so, and any change can be rolled back. It ships as both a command-line tool and an MCP server, so you can drive it yourself or hand it to an AI assistant like Claude. Built to make [Navidrome MCP](https://github.com/Blakeem/Navidrome-MCP) more useful by giving it accurate names and genres.

## Table of Contents

- [Features](#features)
- [Installation](#installation)
- [Tools](#tools)
- [Development](#development)
- [License](#license)

## Features

### 🎵 Genre cleanup (Last.fm)

Pull community top-tags for each artist (optionally each album), fold them through a curated genre vocabulary, and stage clean, consistent genres. Per-file controls let you re-run, skip, or re-queue specific tracks.

### 🎤 Artist-name normalization (Last.fm)

Resolve name variants to a single canonical spelling via `artist.getCorrection`, including the MusicBrainz artist ID, applied across both `artist` and `albumartist`. Feat/sentinel/empty values and multi-value fields are guarded so nothing ambiguous gets rewritten.

### 📅 Album year fill (MusicBrainz)

Blank-fill each album's original release year (`originaldate`) from MusicBrainz without overwriting values you already have.

### 🔍 Mislabeled-file detection

Find files whose identity tags disagree with their folder path — the fingerprint of a tagger matching the wrong release (e.g. an Ozzy Osbourne album stamped as another artist's) — with `tagmend detect-mismatches` or the `detect_mismatches` MCP tool. A read-only, confidence-tiered (high/medium/low) report; nothing is changed on disk.

### 🕳️ Blank-album gap detection

Find files carrying no `album` tag at all — invisible to every album-scoped tool — with the `detect_album_gaps` MCP tool. Groups them by folder and proposes grounded fills (unanimous folder mates, the parsed folder name, or a review-only MusicBrainz recording lookup). A read-only report; proposals only ever fill blanks, never overwrite.

### ↩️ Fully revertible history

A git-like flow: stage → commit → revert. Files are only written on commit, each change is recorded in an append-only per-file log, and any single file or whole commit can be undone. Reverts are themselves tracked commits.

### 🗂️ Per-file status workflow

Mark files as `manual` to exclude them from an axis (genre, artist, or year), or re-queue them as `pending`. Status is sticky and respected on every run.

### 🎚️ Multi-format and engine-first

Reads and writes MP3, FLAC, M4A, and OGG through mutagen. All logic lives in one engine; the CLI and MCP server are thin wrappers over it. Settings live in a single on-disk `settings.json`, edited through a loopback-only browser form with a built-in Last.fm key test, so there are no env vars or hand-edited JSON to manage.

## Installation

### Prerequisites

- **Python 3.12+**
- **A free Last.fm API key** ([create one](https://www.last.fm/api/account/create))
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

Edits apply on the next tool call; every command and MCP tool re-reads `settings.json` fresh (there is no in-process settings cache).

## Tools

The MCP server exposes 31 tools. All tag edits are staged in memory and only written to disk on `commit_tags`, and everything is revertible.

### Core & Library

| Tool | Description |
|------|-------------|
| `check_health` | Verify TagMend is ready to use |
| `scan_library` | Scan a music folder into the snapshot database (reads files, never writes them) |
| `get_library_stats` | Report library-wide snapshot counts |
| `list_files` | List tracked files with their current managed tags (to discover file ids) |
| `get_file` | Return one tracked file with its managed tags, by stable `file_id` |
| `detect_mismatches` | Detect files whose identity tags disagree with their folder path (read-only report) |
| `detect_album_gaps` | Find files with a blank `album` tag, grouped by folder, with tiered fill proposals (read-only report) |

### Staging & Commits

| Tool | Description |
|------|-------------|
| `stage_tags` | Stage a managed-tag change for one file (the git "index"); writes nothing to disk |
| `stage_tags_batch` | Stage managed-tag changes for many files in one atomic, all-or-nothing call |
| `unstage_tags` | Remove a pending staged change for one file |
| `diff_tags` | Show staged-but-uncommitted changes, enriched with the current to target diff |
| `commit_tags` | Apply all staged tag changes to disk as one revertible commit |
| `list_commits` | List commits newest first (the revertible units that group tag changes) |
| `get_commit` | Return one commit by id |

### History & Revert

| Tool | Description |
|------|-------------|
| `history_tags` | Show the append-only tag-revision log for one file, oldest first |
| `revert_tags` | Restore a file's managed tags to a prior version (append-only, revertible) |
| `revert_commit` | Undo an entire commit as a unit: every file it changed goes back to its pre-commit tags |

### Genre (Last.fm)

| Tool | Description |
|------|-------------|
| `resolve_genres` | Look up Last.fm genres for in-scope files and stage the result (writes nothing to disk) |
| `set_genre_status` | Exclude files from genre tagging (`manual`) or re-queue them (`pending`) |
| `reset_genre_status` | Clear any genre status row for in-scope files, returning them to `pending` |

### Artist Names (Last.fm)

| Tool | Description |
|------|-------------|
| `list_artists` | List distinct `artist` values with file counts (to scope a run) |
| `resolve_artists` | Normalize artist names via Last.fm `getCorrection` and stage the result (no disk write) |
| `set_artist_status` | Exclude files from artist-name normalization (`manual`) or re-queue them (`pending`) |
| `reset_artist_status` | Clear any artist status row for in-scope files, returning them to `pending` |

### Album Year (MusicBrainz)

| Tool | Description |
|------|-------------|
| `list_albums` | List distinct album groups with file counts and status (to scope a run) |
| `resolve_years` | Blank-fill the original release year (`originaldate`) from MusicBrainz (no disk write) |
| `set_year_status` | Exclude files from album-year fill (`manual`) or re-queue them (`pending`) |
| `reset_year_status` | Clear any year status row for in-scope files, returning them to `pending` |

### Mismatch fixing

Use `detect_mismatches` (above) to find files whose identity tags disagree with their folder path, then drive the fix through the staging engine (`stage_tags_batch` → `commit_tags` → `reopen_axes`) and disposition the false positives.

| Tool | Description |
|------|-------------|
| `set_mismatch_status` | Silence a mismatch false positive (`legit_ignore`) or defer a misfiled file (`misfiled_deferred`), or clear with `pending` |
| `reset_mismatch_status` | Clear any mismatch disposition for in-scope files, returning them to `pending` |
| `reopen_axes` | After committing a manual identity fix, re-open the file's derived genre/year axes and clear its stale artist status |

## Development

Install the dev extras, then run the four quality gates from the repo root. Lint, format, types, and tests must all pass for any change.

```bash
pip install -e ".[dev]"
ruff check .            # lint
ruff format --check .   # format
mypy                    # strict static typing
pytest                  # tests
```

All logic lives in the engine (`src/tagmend/engine/`); the CLI (`cli.py`) and MCP server (`mcp_server.py`) are thin wrappers. See `CLAUDE.md` for working notes and `PLAN.md` for the full design.

Test the MCP server non-interactively with the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx -y @modelcontextprotocol/inspector --cli tagmend mcp --method tools/list
npx -y @modelcontextprotocol/inspector --cli tagmend mcp --method tools/call --tool-name check_health
```

## License

MIT. See [LICENSE](LICENSE).
