"""TagMend command-line interface (``tagmend …``).

A thin Typer frontend over :mod:`tagmend.engine`. Subcommands:

* ``tagmend doctor``       — readiness check (settings, music folder, ledger).
* ``tagmend scan``         — scan the library into the snapshot (read-only).
* ``tagmend stats``        — show library-wide snapshot counts.
* ``tagmend config``       — launch the local config web UI.
* ``tagmend config-set``   — write a value into ``settings.json``.
* ``tagmend config-path``  — print the settings file location.
* ``tagmend mcp``          — run the MCP server over stdio.
* ``tagmend version``      — print the version.
"""

# NOTE: deliberately no `from __future__ import annotations` here — Typer evaluates
# parameter annotations at runtime, so types like `Path` must be real runtime imports.
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from tagmend import __version__, config, configui, mcp_server
from tagmend.engine import library
from tagmend.engine.doctor import run_health_check
from tagmend.engine.library import ScanMode
from tagmend.log import get_logger, set_level

logger = get_logger(__name__)

app = typer.Typer(
    name="tagmend",
    help="TagMend — mend your music tags.",
    no_args_is_help=True,
    add_completion=True,
)


@app.callback()
def _main(
    *,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Enable debug logging."),
    ] = False,
) -> None:
    """TagMend — genre & artist-name cleanup with full revertible history."""
    if verbose:
        set_level("DEBUG")


@app.command()
def doctor(
    music_path: Annotated[
        Path | None,
        typer.Option(help="Override the configured music folder for this check."),
    ] = None,
) -> None:
    """Check that settings load and the music folder + ledger are reachable."""
    settings = config.load_settings()
    if music_path is not None:
        settings = replace(settings, music_path=music_path)

    report = run_health_check(settings)
    for check in report.checks:
        mark = "OK  " if check.ok else "FAIL"
        typer.echo(f"[{mark}] {check.name}: {check.detail}")

    if not report.ok:
        typer.echo("Not ready — fix the failures above.")
        raise typer.Exit(code=1)
    typer.echo("All checks passed — ready to go.")


@app.command()
def scan(
    path: Annotated[
        Path | None,
        typer.Argument(help="Folder to scan; defaults to the configured music_path."),
    ] = None,
    mode: Annotated[
        ScanMode,
        typer.Option(help="incremental | full | presence"),
    ] = ScanMode.INCREMENTAL,
) -> None:
    """Scan the library into the snapshot (reads files only; never writes tags)."""
    settings = config.load_settings()
    try:
        result = library.scan_library(settings, path=path, mode=mode)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(f"Scan complete (mode: {mode.value}).")
    typer.echo(f"  files seen:      {result.total_seen}")
    typer.echo(f"  added:           {result.added}")
    typer.echo(f"  updated:         {result.updated}")
    typer.echo(f"  unchanged:       {result.unchanged}")
    typer.echo(f"  tags read:       {result.tags_read}")
    typer.echo(f"  restored:        {result.restored}")
    typer.echo(f"  missing flagged: {result.missing_flagged}")
    typer.echo(f"  errors:          {result.errors}")


@app.command()
def stats() -> None:
    """Show library-wide snapshot counts (present/missing/unprocessed, by extension)."""
    settings = config.load_settings()
    summary = library.library_stats(settings)

    typer.echo(f"total files:  {summary['total_files']}")
    typer.echo(f"present:      {summary['present']}")
    typer.echo(f"missing:      {summary['missing']}")
    typer.echo(f"unprocessed:  {summary['unprocessed']}")

    by_ext = summary["by_ext"]
    if isinstance(by_ext, dict):
        typer.echo("by extension:")
        for ext, count in by_ext.items():
            typer.echo(f"  {ext or '(none)'}: {count}")

    typer.echo(f"total tag values: {summary['total_tag_values']}")


@app.command(name="config")
def config_ui() -> None:
    """Launch the local config web UI (serves until Ctrl-C)."""
    configui.run_blocking(on_start=lambda url: typer.echo(f"TagMend config UI: {url}"))


@app.command(name="config-set")
def config_set(
    key: Annotated[str, typer.Argument(help="Setting name (e.g. music_path).")],
    value: Annotated[str, typer.Argument(help="New value.")],
) -> None:
    """Set a value in settings.json (music_path, lastfm_api_key, db_path)."""
    try:
        path = config.set_setting(key, value)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(f"Saved {key} -> {path}")


@app.command(name="config-path")
def config_path() -> None:
    """Print the location of settings.json."""
    typer.echo(str(config.settings_path()))


@app.command()
def mcp() -> None:
    """Run the MCP server over stdio (for MCP clients and the MCP Inspector)."""
    mcp_server.run()


@app.command()
def version() -> None:
    """Print the TagMend version."""
    typer.echo(__version__)
