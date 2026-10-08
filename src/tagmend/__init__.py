"""TagMend mends the tags and paths of a music library.

It mends genres, artist names, original dates, songs, paths and covers, with revertible
history. This package is the importable core (``tagmend``). The CLI and MCP server are
thin frontends over :mod:`tagmend.engine`.
"""

from __future__ import annotations

__version__ = "0.0.1"

__all__ = ["__version__"]
