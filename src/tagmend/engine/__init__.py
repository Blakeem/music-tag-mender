"""TagMend core engine.

All the valuable, risky work lives here and is exercised identically by the CLI and
the MCP server. Frontends must contain no business logic — only argument marshalling
and presentation.
"""

from __future__ import annotations
