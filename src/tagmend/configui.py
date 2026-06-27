"""Local config web UI — edit ``settings.json`` from a friendly browser form (stdlib-only).

Two launch paths share this module (see ``cli.py`` and ``mcp_server.py``):

* ``tagmend config`` runs :func:`run_blocking` — a loopback web server that serves until
  Ctrl-C.
* ``tagmend mcp`` calls :func:`launch_background` when settings are incomplete, serving the
  same UI from a daemon thread without blocking the JSON-RPC channel.

Everything is engine-first and dependency-free: pure helpers (:func:`decide_launch`,
:func:`build_seed`, :func:`validate_and_normalize`, :func:`run_test_ping`,
:func:`is_loopback`/:func:`host_is_loopback`) carry the logic and are unit-tested directly,
while a thin :class:`http.server` handler wires them to HTTP. The handler is locked down for
a single local user: loopback-only source IP, a loopback ``Host`` header, a per-launch CSRF
token on every POST, an exact-filename static allowlist (no path traversal), and a JSON body
cap. The real Last.fm key is never sent to the browser — the seed masks it.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import sqlite3
import threading
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from tagmend.config import _KNOWN_KEYS, _NONE_TOKENS, Settings, load_settings, set_settings
from tagmend.engine.lastfm import LastfmClient, LastfmError
from tagmend.engine.schema import apply_schema
from tagmend.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import httpx

logger = get_logger(__name__)

# What the browser shows in place of the real Last.fm key; an unchanged field means
# "leave the stored key alone" (never the literal placeholder, never the real key).
MASK_PLACEHOLDER: Final = "********"

# The per-launch CSRF token travels in this request header on every POST.
_CSRF_HEADER: Final = "X-TagMend-CSRF"
# index.html ships this placeholder; the handler substitutes the live token when serving it.
_CSRF_PLACEHOLDER: Final = "__CSRF_TOKEN__"

# Numeric field families (light save-time validation only; ``load_settings`` still coerces).
_INT_KEYS: Final[frozenset[str]] = frozenset(
    {"genre_min_weight", "genre_stage_limit", "album_stage_limit"},
)
_FLOAT_KEYS: Final[frozenset[str]] = frozenset(
    {"lastfm_rate_per_sec", "musicbrainz_rate_per_sec"},
)

# Exact-filename static allowlist — no directory listing, no path traversal.
_STATIC_ROUTES: Final[dict[str, str]] = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
}
_CONTENT_TYPES: Final[dict[str, str]] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

# Reject oversized request bodies up front (the form is tiny).
_MAX_BODY_BYTES: Final = 64 * 1024


# --- pure helpers --------------------------------------------------------------------


class ValidationError(Exception):
    """A posted settings payload was rejected; carries the HTTP status to return."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        """Store the *status* (4xx) and human-readable *message* for the response."""
        super().__init__(message)
        self.status = status
        self.message = message


def decide_launch(settings: Settings) -> bool:
    """Return whether the config UI should auto-launch (a core setting is missing)."""
    return settings.music_path is None or settings.lastfm_api_key is None


def build_seed(settings: Settings) -> dict[str, object]:
    """Build the ``/api/seed`` payload: every key as a string, the Last.fm key masked.

    The real key is never included — ``lastfm_api_key`` is the mask placeholder when a key
    is set (empty otherwise), alongside a ``has_lastfm_api_key`` boolean for the UI.
    """
    values: dict[str, str] = {
        "music_path": str(settings.music_path) if settings.music_path else "",
        "db_path": str(settings.db_path),
        "genre_min_weight": str(settings.genre_min_weight),
        "genre_max_count": (
            "" if settings.genre_max_count is None else str(settings.genre_max_count)
        ),
        "genre_use_album_tags": "true" if settings.genre_use_album_tags else "false",
        "lastfm_rate_per_sec": str(settings.lastfm_rate_per_sec),
        "genre_stage_limit": str(settings.genre_stage_limit),
        "musicbrainz_rate_per_sec": str(settings.musicbrainz_rate_per_sec),
        "musicbrainz_user_agent": settings.musicbrainz_user_agent,
        "album_stage_limit": str(settings.album_stage_limit),
    }
    has_key = settings.lastfm_api_key is not None
    values["lastfm_api_key"] = MASK_PLACEHOLDER if has_key else ""
    return {"values": values, "has_lastfm_api_key": has_key}


def validate_and_normalize(payload: Mapping[str, object]) -> dict[str, str]:
    """Validate a posted form payload and return the subset of keys to persist.

    Unknown keys raise a 400 ``ValidationError``; numeric/none-token fields that don't parse
    raise a 422. An unchanged ``lastfm_api_key`` (the mask placeholder or an empty field) is
    dropped so the stored key is preserved — clearing a key stays a ``config-set`` action.
    """
    result: dict[str, str] = {}
    for key, raw_value in payload.items():
        if key not in _KNOWN_KEYS:
            message = f"unknown setting {key!r}"
            raise ValidationError(HTTPStatus.BAD_REQUEST, message)
        value = "" if raw_value is None else str(raw_value)
        if key == "lastfm_api_key":
            if value and value != MASK_PLACEHOLDER:
                result[key] = value
            continue
        _validate_typed(key, value)
        result[key] = value
    return result


def _validate_typed(key: str, value: str) -> None:
    """Raise a 422 ``ValidationError`` when a numeric/none-token field cannot be parsed."""
    if key in _INT_KEYS:
        _require_int(key, value)
    elif key in _FLOAT_KEYS:
        _require_float(key, value)
    elif key == "genre_max_count" and value.strip().lower() not in _NONE_TOKENS:
        _require_int(key, value)
    # genre_use_album_tags + free-text keys accept any value (load_settings coerces).


def _require_int(key: str, value: str) -> None:
    """Raise a 422 ``ValidationError`` unless *value* parses as an integer."""
    try:
        int(value)
    except ValueError as exc:
        message = f"{key} must be a whole number (got {value!r})"
        raise ValidationError(HTTPStatus.UNPROCESSABLE_ENTITY, message) from exc


def _require_float(key: str, value: str) -> None:
    """Raise a 422 ``ValidationError`` unless *value* parses as a number."""
    try:
        float(value)
    except ValueError as exc:
        message = f"{key} must be a number (got {value!r})"
        raise ValidationError(HTTPStatus.UNPROCESSABLE_ENTITY, message) from exc


def apply_save(payload: Mapping[str, object]) -> Path:
    """Validate *payload* then merge the accepted keys into ``settings.json``."""
    accepted = validate_and_normalize(payload)
    return set_settings(accepted)


def run_test_ping(
    api_key: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, object]:
    """Validate a Last.fm key by pinging ``artist.gettoptags`` via a throwaway in-memory DB.

    Returns ``{"ok": True}`` when the key works and ``{"ok": False, "error": ...}`` on a
    :class:`LastfmError`. The cache lives in an in-memory SQLite connection, so the real
    ledger is never touched; *transport* is injectable so tests fake the network.
    """
    conn = sqlite3.connect(":memory:")
    try:
        apply_schema(conn)
        with LastfmClient(api_key, conn, transport=transport) as client:
            client.artist_top_tags(name="The Beatles")
    except LastfmError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()
    return {"ok": True}


def is_loopback(addr: str) -> bool:
    """Return whether *addr* is an IPv4/IPv6 loopback address (``::ffff:`` mapped too)."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return ip.is_loopback


def host_is_loopback(host: str, port: int) -> bool:
    """Return whether a ``Host`` header names this loopback server (host + matching port)."""
    hostname, has_port, port_part = _split_host_port(host)
    if has_port and port_part != str(port):
        return False
    if hostname == "localhost":
        return True
    return is_loopback(hostname)


def _split_host_port(host: str) -> tuple[str, bool, str]:
    """Split a ``Host`` header into ``(hostname, has_port, port)``, unwrapping ``[..]`` IPv6."""
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return host, False, ""
        hostname = host[1:end]
        rest = host[end + 1 :]
        if rest.startswith(":"):
            return hostname, True, rest[1:]
        return hostname, False, ""
    if host.count(":") == 1:
        hostname, _, port_part = host.partition(":")
        return hostname, True, port_part
    return host, False, ""


def _new_csrf_token() -> str:
    """Return a fresh, unguessable per-launch CSRF token."""
    return secrets.token_urlsafe(32)


def _read_web_asset(filename: str) -> str:
    """Read a bundled static asset from ``tagmend/data/web`` (caller guarantees the name)."""
    resource = resources.files("tagmend") / "data" / "web" / filename
    return resource.read_text(encoding="utf-8")


# --- HTTP server ---------------------------------------------------------------------


class _ConfigServer(ThreadingHTTPServer):
    """Loopback config server holding the per-launch CSRF token for its handlers."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        csrf_token: str,
    ) -> None:
        """Bind *server_address* and remember the launch's *csrf_token*."""
        self.csrf_token = csrf_token
        super().__init__(server_address, handler_class)


class _ConfigHandler(BaseHTTPRequestHandler):
    """Dispatch loopback requests to the config helpers, guarded for a single local user."""

    @property
    def _cfg(self) -> _ConfigServer:
        """Return the owning server narrowed to :class:`_ConfigServer`."""
        return cast("_ConfigServer", self.server)

    def log_message(self, fmt: str, *args: object) -> None:
        """Route the stdlib access log to our logger (stderr), keeping stdout clean."""
        logger.debug("configui %s", fmt % args if args else fmt)

    def do_GET(self) -> None:
        """Serve a static asset or the JSON seed; everything else is 404."""
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in _STATIC_ROUTES:
            self._serve_static(_STATIC_ROUTES[path])
            return
        if path == "/api/seed":
            self._serve_seed()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        """Handle the save / test endpoints after the source/Host/CSRF guards pass."""
        if not self._guard() or not self._check_csrf():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/save":
            self._handle_save()
            return
        if path == "/api/test":
            self._handle_test()
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    # --- guards ----------------------------------------------------------------------

    def _guard(self) -> bool:
        """Reject non-loopback source IPs and non-loopback ``Host`` headers (403)."""
        if not is_loopback(self.client_address[0]):
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "forbidden"})
            return False
        host = self.headers.get("Host", "")
        if not host_is_loopback(host, self._cfg.server_port):
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "forbidden host"})
            return False
        return True

    def _check_csrf(self) -> bool:
        """Reject a POST whose CSRF header does not match the per-launch token (403)."""
        token = self.headers.get(_CSRF_HEADER, "")
        if not secrets.compare_digest(token, self._cfg.csrf_token):
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "bad csrf token"})
            return False
        return True

    # --- handlers --------------------------------------------------------------------

    def _serve_static(self, filename: str) -> None:
        """Serve a bundled asset, injecting the live CSRF token into ``index.html``."""
        text = _read_web_asset(filename)
        if filename == "index.html":
            text = text.replace(_CSRF_PLACEHOLDER, self._cfg.csrf_token)
        content_type = _CONTENT_TYPES.get(Path(filename).suffix, "application/octet-stream")
        self._send_bytes(HTTPStatus.OK, content_type, text.encode("utf-8"))

    def _serve_seed(self) -> None:
        """Serve the masked current settings with ``Cache-Control: no-store``."""
        body = json.dumps(build_seed(load_settings())).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_save(self) -> None:
        """Validate the posted form and merge it into ``settings.json``."""
        payload = self._read_json_body()
        if payload is None:
            return
        try:
            apply_save(payload)
        except ValidationError as exc:
            self._send_json(exc.status, {"ok": False, "error": exc.message})
            return
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _handle_test(self) -> None:
        """Ping Last.fm with the posted key (or the stored one if unchanged/masked)."""
        payload = self._read_json_body()
        if payload is None:
            return
        posted = payload.get("lastfm_api_key")
        api_key = posted if isinstance(posted, str) else ""
        if not api_key or api_key == MASK_PLACEHOLDER:
            api_key = load_settings().lastfm_api_key or ""
        if not api_key:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error": "no Last.fm key to test"},
            )
            return
        self._send_json(HTTPStatus.OK, run_test_ping(api_key))

    # --- io ----------------------------------------------------------------------

    def _read_json_body(self) -> dict[str, object] | None:
        """Read and parse a capped JSON object body, sending a 4xx + ``None`` on any fault."""
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self._send_json(HTTPStatus.LENGTH_REQUIRED, {"ok": False, "error": "missing length"})
            return None
        try:
            length = int(length_header)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "bad length"})
            return None
        if length > _MAX_BODY_BYTES:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"ok": False, "error": "body too large"},
            )
            return None
        raw = self.rfile.read(length)
        try:
            parsed: object = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid JSON"})
            return None
        if not isinstance(parsed, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "expected an object"})
            return None
        return parsed

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        """Send a JSON response with the given *status*."""
        self._send_bytes(status, "application/json", json.dumps(payload).encode("utf-8"))

    def _send_bytes(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        """Send raw *body* bytes with *content_type* and a ``Content-Length``."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# --- launch entry points -------------------------------------------------------------


def _make_server(csrf_token: str) -> _ConfigServer:
    """Build a config server bound to an ephemeral loopback port."""
    return _ConfigServer(("127.0.0.1", 0), _ConfigHandler, csrf_token=csrf_token)


def _maybe_open_browser(
    url: str,
    *,
    open_browser: bool,
    opener: Callable[[str], bool],
) -> None:
    """Open *url* in a browser unless disabled by flag or ``TAGMEND_NO_BROWSER``."""
    if not open_browser or os.environ.get("TAGMEND_NO_BROWSER"):
        return
    try:
        opener(url)
    except Exception:  # noqa: BLE001 - opening a browser must never break the server
        logger.debug("could not open a browser for %s", url)


def run_blocking(
    *,
    open_browser: bool = True,
    opener: Callable[[str], bool] = webbrowser.open,
    on_start: Callable[[str], None] | None = None,
) -> None:
    """Serve the config UI on a loopback port until Ctrl-C, then close the socket cleanly.

    *on_start* (when given) receives the URL before serving begins — the CLI uses it to echo
    the address. The browser open is injectable and honors ``TAGMEND_NO_BROWSER``.
    """
    server = _make_server(_new_csrf_token())
    url = f"http://127.0.0.1:{server.server_port}/"
    logger.info("config UI listening at %s", url)
    if on_start is not None:
        on_start(url)
    _maybe_open_browser(url, open_browser=open_browser, opener=opener)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def launch_background(
    *,
    open_browser: bool = True,
    opener: Callable[[str], bool] = webbrowser.open,
) -> str:
    """Start the config UI in a daemon thread and return its URL (non-blocking).

    Used by ``tagmend mcp`` so an incomplete-config first run can be fixed without leaving
    the MCP stdio loop. The browser open is injectable and honors ``TAGMEND_NO_BROWSER``.
    """
    server = _make_server(_new_csrf_token())
    url = f"http://127.0.0.1:{server.server_port}/"
    thread = threading.Thread(target=server.serve_forever, name="tagmend-configui", daemon=True)
    thread.start()
    _maybe_open_browser(url, open_browser=open_browser, opener=opener)
    return url
