"""Tests for the config web UI (``tagmend.configui``).

Pure helpers are unit-tested directly; ``run_test_ping`` fakes the network with
:class:`httpx.MockTransport`; one integration test boots the real stdlib server on an
ephemeral loopback port in a thread and drives it with ``httpx``. The autouse
``_isolate_config`` fixture keeps every write inside ``tmp_path``.
"""

from __future__ import annotations

import threading
from http import HTTPStatus
from typing import TYPE_CHECKING

import httpx
import pytest

from tagmend import config, configui
from tagmend.config import Settings

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _settings(**overrides: object) -> Settings:
    """Build a Settings with sensible defaults, overriding only what a test cares about."""
    base: dict[str, object] = {
        "music_path": None,
        "lastfm_api_key": None,
        "db_path": config.default_db_path(),
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- decide_launch -------------------------------------------------------------------


def test_decide_launch_both_present_is_false(tmp_path: Path) -> None:
    settings = _settings(music_path=tmp_path, lastfm_api_key="k")
    assert configui.decide_launch(settings) is False


def test_decide_launch_missing_music_is_true() -> None:
    assert configui.decide_launch(_settings(lastfm_api_key="k")) is True


def test_decide_launch_missing_key_is_true(tmp_path: Path) -> None:
    assert configui.decide_launch(_settings(music_path=tmp_path)) is True


# --- build_seed ----------------------------------------------------------------------


def test_build_seed_masks_key_and_flags_present(tmp_path: Path) -> None:
    settings = _settings(music_path=tmp_path, lastfm_api_key="super-secret")
    seed = configui.build_seed(settings)

    values = seed["values"]
    assert isinstance(values, dict)
    assert values["lastfm_api_key"] == configui.MASK_PLACEHOLDER
    assert seed["has_lastfm_api_key"] is True
    # The real key must never reach the browser.
    assert "super-secret" not in str(seed)


def test_build_seed_unset_key_has_no_placeholder() -> None:
    seed = configui.build_seed(_settings())
    values = seed["values"]
    assert isinstance(values, dict)
    assert values["lastfm_api_key"] == ""
    assert seed["has_lastfm_api_key"] is False


def test_build_seed_exposes_musicbrainz_contact() -> None:
    seed = configui.build_seed(_settings(musicbrainz_contact="me@example.com"))
    values = seed["values"]
    assert isinstance(values, dict)
    # The form edits the raw contact, not the composed (version-bearing) user agent.
    assert values["musicbrainz_contact"] == "me@example.com"
    assert "musicbrainz_user_agent" not in values


# --- validate_and_normalize ----------------------------------------------------------


def test_validate_drops_masked_key() -> None:
    result = configui.validate_and_normalize({"lastfm_api_key": configui.MASK_PLACEHOLDER})
    assert "lastfm_api_key" not in result


def test_validate_drops_empty_key() -> None:
    result = configui.validate_and_normalize({"lastfm_api_key": ""})
    assert "lastfm_api_key" not in result


def test_validate_keeps_new_key() -> None:
    result = configui.validate_and_normalize({"lastfm_api_key": "fresh"})
    assert result == {"lastfm_api_key": "fresh"}


def test_validate_rejects_unknown_key() -> None:
    with pytest.raises(configui.ValidationError) as excinfo:
        configui.validate_and_normalize({"bogus": "x"})
    assert excinfo.value.status == HTTPStatus.BAD_REQUEST


def test_validate_rejects_bad_int() -> None:
    with pytest.raises(configui.ValidationError) as excinfo:
        configui.validate_and_normalize({"genre_min_weight": "lots"})
    assert excinfo.value.status == HTTPStatus.UNPROCESSABLE_ENTITY


def test_validate_rejects_bad_float() -> None:
    with pytest.raises(configui.ValidationError) as excinfo:
        configui.validate_and_normalize({"lastfm_rate_per_sec": "fast"})
    assert excinfo.value.status == HTTPStatus.UNPROCESSABLE_ENTITY


@pytest.mark.parametrize("token", ["", "0", "none", "null"])
def test_validate_accepts_genre_max_count_none_tokens(token: str) -> None:
    result = configui.validate_and_normalize({"genre_max_count": token})
    assert result == {"genre_max_count": token}


@pytest.mark.parametrize("token", ["true", "false"])
def test_validate_accepts_bool_tokens(token: str) -> None:
    result = configui.validate_and_normalize({"genre_use_album_tags": token})
    assert result == {"genre_use_album_tags": token}


# --- is_loopback / host_is_loopback --------------------------------------------------


@pytest.mark.parametrize("addr", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_is_loopback_accepts_loopback(addr: str) -> None:
    assert configui.is_loopback(addr) is True


@pytest.mark.parametrize("addr", ["8.8.8.8", "192.168.1.4", "not-an-ip", ""])
def test_is_loopback_rejects_others(addr: str) -> None:
    assert configui.is_loopback(addr) is False


def test_host_is_loopback_accepts_matching_port() -> None:
    assert configui.host_is_loopback("127.0.0.1:8731", 8731) is True


def test_host_is_loopback_accepts_localhost_and_bracketed_v6() -> None:
    assert configui.host_is_loopback("localhost:8731", 8731) is True
    assert configui.host_is_loopback("[::1]:8731", 8731) is True
    assert configui.host_is_loopback("127.0.0.1", 8731) is True


def test_host_is_loopback_rejects_wrong_port_and_non_loopback() -> None:
    assert configui.host_is_loopback("127.0.0.1:8731", 9999) is False
    assert configui.host_is_loopback("evil.example.com:8731", 8731) is False
    assert configui.host_is_loopback("", 8731) is False


# --- run_test_ping -------------------------------------------------------------------


def _transport(response: httpx.Response) -> httpx.MockTransport:
    """A MockTransport always serving *response* (no real network)."""
    return httpx.MockTransport(lambda _request: response)


def test_run_test_ping_success_no_ledger_file() -> None:
    body = {"toptags": {"tag": [{"name": "rock", "count": 100}]}}
    result = configui.run_test_ping("key", transport=_transport(httpx.Response(200, json=body)))
    assert result == {"ok": True}
    # The throwaway in-memory DB must not have created the real ledger file.
    assert not config.default_db_path().exists()


def test_run_test_ping_invalid_key_reports_error() -> None:
    body = {"error": 10, "message": "Invalid API key"}
    result = configui.run_test_ping("bad", transport=_transport(httpx.Response(200, json=body)))
    assert result["ok"] is False
    assert "10" in str(result["error"])


def test_run_test_ping_http_error_reports_error() -> None:
    result = configui.run_test_ping("k", transport=_transport(httpx.Response(503, json={})))
    assert result["ok"] is False


# --- integration: boot the real server on an ephemeral loopback port -----------------


@pytest.fixture
def server() -> Iterator[tuple[str, str]]:
    """Boot the config server on 127.0.0.1:0 in a daemon thread; yield (base_url, token)."""
    token = "test-csrf-token"  # noqa: S105 - test CSRF token, not a credential
    handle = configui._make_server(token)
    thread = threading.Thread(target=handle.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{handle.server_port}"
    try:
        yield base, token
    finally:
        handle.shutdown()
        handle.server_close()
        thread.join(timeout=5)


def test_server_serves_index(server: tuple[str, str]) -> None:
    base, _token = server
    with httpx.Client() as client:
        response = client.get(base + "/")
    assert response.status_code == HTTPStatus.OK
    assert "text/html" in response.headers["content-type"]
    # The CSRF placeholder is substituted with the live token.
    assert configui._CSRF_PLACEHOLDER not in response.text


def test_server_seed_is_masked_and_no_store(server: tuple[str, str]) -> None:
    base, _token = server
    config.set_setting("lastfm_api_key", "real-key")
    with httpx.Client() as client:
        response = client.get(base + "/api/seed")
    assert response.status_code == HTTPStatus.OK
    assert response.headers["cache-control"] == "no-store"
    assert "real-key" not in response.text
    assert response.json()["has_lastfm_api_key"] is True


def test_server_save_persists(server: tuple[str, str]) -> None:
    base, token = server
    with httpx.Client() as client:
        response = client.post(
            base + "/api/save",
            json={"genre_min_weight": "9"},
            headers={"X-TagMend-CSRF": token},
        )
    assert response.status_code == HTTPStatus.OK
    assert config.load_settings().genre_min_weight == 9


def test_server_save_rejects_unknown_key(server: tuple[str, str]) -> None:
    base, token = server
    with httpx.Client() as client:
        response = client.post(
            base + "/api/save",
            json={"bogus": "x"},
            headers={"X-TagMend-CSRF": token},
        )
    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_server_post_without_csrf_is_forbidden(server: tuple[str, str]) -> None:
    base, _token = server
    with httpx.Client() as client:
        response = client.post(base + "/api/save", json={"genre_min_weight": "1"})
    assert response.status_code == HTTPStatus.FORBIDDEN


def test_server_static_allowlist_and_traversal(server: tuple[str, str]) -> None:
    base, _token = server
    with httpx.Client() as client:
        for name in ("/index.html", "/app.js", "/styles.css"):
            assert client.get(base + name).status_code == HTTPStatus.OK
        # A path outside the exact-filename allowlist is 404 — no traversal possible.
        traversal = client.get(base + "/%2e%2e/config.py")
        assert traversal.status_code == HTTPStatus.NOT_FOUND
