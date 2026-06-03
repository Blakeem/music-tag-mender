# CLAUDE.md — TagMend working notes

TagMend (`tagmend`) is a CLI + MCP tool that cleans up **genre** and **artist-name**
tags in a music library using **Last.fm**, with an **append-only, fully revertible**
history per file, plus an opt-in **file/folder reorganization** feature (also
tracked & revertible). Engine-first: all logic lives in `tagmend.engine`; the CLI and
MCP server are thin wrappers. **Read `PLAN.md` for the full design** — this file is
just how to work in the repo.

Current status: **M0 skeleton complete** (config, logger, SQLite connection, CLI +
MCP server wired, `doctor`/`health_check` working). No tag writing yet.

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
- **Tests never touch the real `music/` folder** (it's gitignored, copyrighted).
  Use `tmp_path` / the `temp_library` fixture. Real-artist synthesized fixtures for
  format coverage come later (PLAN.md §21).

## Quality gates — all three must pass before anything is "done"

Run from the repo root (venv at `.venv`):

```powershell
.\.venv\Scripts\ruff.exe check .          # lint (near-all rules; see pyproject)
.\.venv\Scripts\ruff.exe format .         # autoformat (use --check in CI)
.\.venv\Scripts\mypy.exe                  # strict static typing
.\.venv\Scripts\pytest.exe                # tests
```

Lint, types, and tests are **required** every change. Ruff is configured with
`select = ["ALL"]` minus a few formatter-conflicting rules — fix issues, don't
broaden the ignore list without reason. `cli.py` intentionally omits
`from __future__ import annotations` because Typer evaluates annotations at runtime.

## Running the tool

```powershell
.\.venv\Scripts\tagmend.exe doctor                       # readiness check
.\.venv\Scripts\tagmend.exe config-set music_path "E:\path\to\music"
.\.venv\Scripts\tagmend.exe config-path                  # where settings.json lives
.\.venv\Scripts\tagmend.exe mcp                          # run MCP server (stdio)
```

Settings file (this machine): `C:\Users\Blake\AppData\Local\tagmend\settings.json`.
SQLite ledger: `C:\Users\Blake\AppData\Local\tagmend\tagmend.sqlite3`.
(`music_path` is already set to `E:\music-tag-mender\music` for dev.)

## MCP Inspector (from the command line)

The MCP server is `tagmend mcp` (stdio). Test it non-interactively with the
Inspector's **CLI mode** (Node/npx required, both present):

```powershell
$tag = "E:\music-tag-mender\.venv\Scripts\tagmend.exe"

# List tools
npx -y @modelcontextprotocol/inspector --cli $tag mcp --method tools/list

# Call the readiness tool (expect "ok": true, isError: false)
npx -y @modelcontextprotocol/inspector --cli $tag mcp --method tools/call --tool-name health_check
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
  mcp_server.py     FastMCP server (thin)
  engine/
    db.py           SQLite connection (WAL); schema added per feature
    scan.py         library discovery (real)
    doctor.py       health_check / readiness (real)
    tags.py lastfm.py classify.py versioning.py moves.py   # documented stubs
tests/              pytest; conftest isolates config + builds temp libraries
```

## Roadmap pointer

Milestones M0–M6 are in `PLAN.md §14`. Move/rename (organize) design is **§18**;
settings **§19**; logging **§20**; quality gates **§21**.
