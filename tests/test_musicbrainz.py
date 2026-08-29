"""Unit tests for the MusicBrainz client (``engine/musicbrainz.py``).

All network traffic is faked with :class:`httpx.MockTransport`; the cache lives in the
in-memory ``db_conn`` fixture (real schema). The clock/sleep are injected so pacing is
asserted without any real waiting. These never hit the live MusicBrainz API.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from tagmend.engine import musicbrainz
from tagmend.engine.musicbrainz import (
    MusicBrainzClient,
    MusicBrainzError,
    _artist_request_key,
    _recording_request_key,
    _release_request_key,
    _request_key,
)
from tagmend.engine.store import get_cached_mb_album, get_cached_mb_recording

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping


def _group(**overrides: object) -> dict[str, object]:
    """Build one release-group entry as the MB JSON search response shapes it.

    Defaults describe a clean Album hit (*Paranoid*, 1970, score 100, one release);
    pass keyword overrides (e.g. ``primary_type="Single"``, ``secondary_types=["Live"]``,
    ``releases=None``) to vary it.
    """
    entry: dict[str, object] = {
        "id": "rg-1",
        "title": "Paranoid",
        "primary-type": "Album",
        "first-release-date": "1970-09-18",
        "score": 100,
        "releases": [{"id": "rel-1"}],
    }
    # Normalize Python-friendly override keys to the MB JSON spellings.
    rename = {
        "primary_type": "primary-type",
        "secondary_types": "secondary-types",
        "first_release_date": "first-release-date",
        "rgid": "id",
    }
    for key, value in overrides.items():
        mb_key = rename.get(key, key)
        if value is None:
            entry.pop(mb_key, None)
        else:
            entry[mb_key] = value
    return entry


def _body(*groups: dict[str, object]) -> dict[str, object]:
    """Wrap *groups* in the release-group search envelope."""
    return {"release-groups": list(groups)}


def _handler(
    responses: list[httpx.Response],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[int]]:
    calls: list[int] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        index = len(calls)
        calls.append(index)
        return responses[index]

    return handle, calls


def _json_response(body: Mapping[str, object], status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=dict(body))


def _client(
    db_conn: sqlite3.Connection,
    responses: list[httpx.Response],
    *,
    rate_per_sec: float = 0.0,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> tuple[MusicBrainzClient, list[int]]:
    handle, calls = _handler(responses)
    transport = httpx.MockTransport(handle)
    kwargs: dict[str, object] = {"rate_per_sec": rate_per_sec, "transport": transport}
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    if sleep is not None:
        kwargs["sleep"] = sleep
    client = MusicBrainzClient("TagMend/test ( test@example.com )", db_conn, **kwargs)  # type: ignore[arg-type]
    return client, calls


# --- found / selection ---------------------------------------------------------------


def test_returns_album_original_year(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_body(_group()))])
    with client:
        album = client.album_first_release("Black Sabbath", "Paranoid")

    assert album is not None
    assert album.original_date == "1970"  # normalized to the four-digit year
    assert album.album_title == "Paranoid"
    assert album.release_group_id == "rg-1"
    assert album.release_mbid == "rel-1"
    assert len(calls) == 1


def test_picks_highest_scoring_album(db_conn: sqlite3.Connection) -> None:
    body = _body(
        _group(first_release_date="1999", score=40, rgid="rg-lo"),
        _group(first_release_date="1970", score=100, rgid="rg-hi"),
    )
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        album = client.album_first_release("Artist", "Paranoid")
    assert album is not None
    assert album.original_date == "1970"
    assert album.release_group_id == "rg-hi"


def test_skips_non_album_primary_type(db_conn: sqlite3.Connection) -> None:
    body = _body(_group(primary_type="Single", first_release_date="1971"))
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        assert client.album_first_release("Artist", "Paranoid") is None


def test_skips_live_and_compilation_secondary_types(db_conn: sqlite3.Connection) -> None:
    body = _body(
        _group(secondary_types=["Live"], first_release_date="1975"),
        _group(secondary_types=["Compilation"], first_release_date="1980"),
    )
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        assert client.album_first_release("Artist", "Paranoid") is None


def test_skips_demo_secondary_type(db_conn: sqlite3.Connection) -> None:
    body = _body(_group(secondary_types=["Demo"], first_release_date="2019"))
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        assert client.album_first_release("Artist", "Paranoid") is None


def test_replicas_candidate_set_is_no_match(db_conn: sqlite3.Connection) -> None:
    # The live run's Gary Numan / Replicas candidate set: the Demo reissue outscored every
    # other candidate and filled 2019 over the album's real 1979.
    body = _body(
        _group(
            title="Replicas: The First Recordings",
            secondary_types=["Demo"],
            first_release_date="2019-10-04",
            score=100,
            rgid="rg-demo",
        ),
        _group(
            title="Replicas Live",
            secondary_types=["Live"],
            first_release_date="2008",
            score=95,
            rgid="rg-live-1",
        ),
        _group(
            title="Replicas (Live)",
            secondary_types=["Live"],
            first_release_date="2011",
            score=90,
            rgid="rg-live-2",
        ),
        _group(
            title="Replicas",
            secondary_types=["Compilation"],
            first_release_date="1998",
            score=88,
            rgid="rg-comp",
        ),
    )
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        assert client.album_first_release("Gary Numan", "Replicas") is None


def test_edition_suffix_in_request_still_matches_plain_release_group(
    db_conn: sqlite3.Connection,
) -> None:
    body = _body(_group(title="Fiction", first_release_date="2007-04-20", rgid="rg-fiction"))
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        album = client.album_first_release("Dark Tranquillity", "Fiction (Deluxe Edition)")
    assert album is not None
    assert album.original_date == "2007"
    assert album.release_group_id == "rg-fiction"


def test_rejects_candidate_carrying_content_the_request_lacks(
    db_conn: sqlite3.Connection,
) -> None:
    body = _body(_group(title="Fiction: The Demos", first_release_date="2019", rgid="rg-demos"))
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        assert client.album_first_release("Dark Tranquillity", "Fiction") is None


def test_non_ascii_title_matches_itself(db_conn: sqlite3.Connection) -> None:
    # ``fold`` keeps only [a-z0-9], so a wholly non-Latin title folds to "" and would never
    # match the album it names unless the loose key catches it.
    body = _body(_group(title="Спутник", first_release_date="1985", rgid="rg-nonascii"))
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        album = client.album_first_release("Artist", "Спутник")
    assert album is not None
    assert album.original_date == "1985"
    assert album.release_group_id == "rg-nonascii"


def test_different_non_ascii_titles_do_not_match(db_conn: sqlite3.Connection) -> None:
    body = _body(_group(title="東京事変", first_release_date="2004", rgid="rg-other"))
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        assert client.album_first_release("Artist", "Спутник") is None


@pytest.mark.parametrize(
    ("artist", "album", "year"),
    [
        ("36 Crazyfists", "In the Skin", "1997"),
        ("Autolux", "Future Perfect", "2004"),
        ("Dark Tranquillity", "Fiction", "2007"),
        ("Imperative Reaction", "Eulogy for the Sick Child", "1999"),
    ],
)
def test_live_run_album_years_stay_pinned(
    db_conn: sqlite3.Connection,
    artist: str,
    album: str,
    year: str,
) -> None:
    # The decoy scores as high as the real group and carries no excluded secondary type, so
    # only the title gate can keep the year each of these resolved to in the live run.
    body = _body(
        _group(title=f"{album}: The Demos", first_release_date="2019", rgid="rg-decoy"),
        _group(title=album, first_release_date=year, rgid="rg-real"),
    )
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        resolved = client.album_first_release(artist, album)
    assert resolved is not None
    assert resolved.original_date == year
    assert resolved.release_group_id == "rg-real"


def test_empty_results_is_no_match(db_conn: sqlite3.Connection) -> None:
    client, _ = _client(db_conn, [_json_response(_body())])
    with client:
        assert client.album_first_release("Nobody", "Nothing") is None


# --- caching -------------------------------------------------------------------------


def test_found_result_is_positive_cached(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_body(_group()))])
    with client:
        client.album_first_release("Black Sabbath", "Paranoid")
        # Second call is served from cache — no second network request.
        again = client.album_first_release("Black Sabbath", "Paranoid")
    assert again is not None
    assert again.original_date == "1970"
    assert len(calls) == 1

    cached = get_cached_mb_album(db_conn, _request_key("Black Sabbath", "Paranoid"))
    assert cached is not None
    assert cached[0] is True


def test_no_match_is_negative_cached(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_body())])
    with client:
        client.album_first_release("Nobody", "Nothing")
        client.album_first_release("Nobody", "Nothing")
    assert len(calls) == 1  # the negative result is cached, not re-fetched
    cached = get_cached_mb_album(db_conn, _request_key("Nobody", "Nothing"))
    assert cached is not None
    assert cached[0] is False


def test_selection_version_changes_request_key(monkeypatch: pytest.MonkeyPatch) -> None:
    before = _request_key("Gary Numan", "Replicas")
    monkeypatch.setattr(musicbrainz, "_SELECTION_VERSION", "rules-bumped")
    assert _request_key("Gary Numan", "Replicas") != before


def test_selection_version_changes_recording_request_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The recording path shares the excluded-secondary-type set, so one bump must reopen both.
    before = _recording_request_key("Black Sabbath", "War Pigs")
    monkeypatch.setattr(musicbrainz, "_SELECTION_VERSION", "rules-bumped")
    assert _recording_request_key("Black Sabbath", "War Pigs") != before


# --- transient errors ----------------------------------------------------------------


def test_http_error_raises_and_caches_nothing(db_conn: sqlite3.Connection) -> None:
    client, _ = _client(db_conn, [_json_response({}, status_code=503)])
    with client, pytest.raises(MusicBrainzError):
        client.album_first_release("Artist", "Album")
    # Nothing cached → a re-run would retry.
    assert get_cached_mb_album(db_conn, _request_key("Artist", "Album")) is None


# --- pacing --------------------------------------------------------------------------


def test_pacing_sleeps_between_network_requests(db_conn: sqlite3.Connection) -> None:
    clock = [0.0]
    slept: list[float] = []

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    client, _ = _client(
        db_conn,
        [_json_response(_body(_group())), _json_response(_body())],
        rate_per_sec=1.0,
        monotonic=monotonic,
        sleep=sleep,
    )
    with client:
        client.album_first_release("A", "Paranoid")
        client.album_first_release("B", "Two")

    # The second request was paced to honor 1 req/sec.
    assert slept == [1.0]


# --- recording search (the review-only album-gaps tier) ------------------------------


def _rec_group(**overrides: object) -> dict[str, object]:
    """Build one nested release-group as the recording response shapes it (Album by default)."""
    group: dict[str, object] = {"id": "rg-1", "title": "Paranoid", "primary-type": "Album"}
    rename = {"primary_type": "primary-type", "secondary_types": "secondary-types", "rgid": "id"}
    for key, value in overrides.items():
        mb_key = rename.get(key, key)
        if value is None:
            group.pop(mb_key, None)
        else:
            group[mb_key] = value
    return group


def _recording(group: dict[str, object] | None = None, **overrides: object) -> dict[str, object]:
    """Build one recording entry wrapping *group* in a single release (score 100 by default)."""
    entry: dict[str, object] = {
        "id": "rec-1",
        "title": "War Pigs",
        "score": 100,
        "releases": [{"id": "rel-1", "release-group": _rec_group() if group is None else group}],
    }
    rename = {"rec_id": "id"}
    for key, value in overrides.items():
        mb_key = rename.get(key, key)
        if value is None:
            entry.pop(mb_key, None)
        else:
            entry[mb_key] = value
    return entry


def _recording_body(*recordings: dict[str, object]) -> dict[str, object]:
    """Wrap *recordings* in the recording-search envelope."""
    return {"recordings": list(recordings)}


def test_recording_search_returns_album(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_recording_body(_recording()))])
    with client:
        recording = client.recording_search("Black Sabbath", "War Pigs")

    assert recording is not None
    assert recording.album_title == "Paranoid"
    assert recording.release_group_id == "rg-1"
    assert recording.recording_mbid == "rec-1"
    assert len(calls) == 1


def test_recording_search_picks_highest_scoring(db_conn: sqlite3.Connection) -> None:
    body = _recording_body(
        _recording(_rec_group(title="Low", rgid="rg-lo"), rec_id="rec-lo", score=40),
        _recording(_rec_group(title="High", rgid="rg-hi"), rec_id="rec-hi", score=100),
    )
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        recording = client.recording_search("Artist", "Title")
    assert recording is not None
    assert recording.album_title == "High"
    assert recording.release_group_id == "rg-hi"
    assert recording.recording_mbid == "rec-hi"


def test_recording_search_skips_non_album_release_group(db_conn: sqlite3.Connection) -> None:
    body = _recording_body(_recording(_rec_group(primary_type="Single")))
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        assert client.recording_search("Artist", "Title") is None


def test_recording_search_skips_live_and_compilation(db_conn: sqlite3.Connection) -> None:
    body = _recording_body(
        _recording(_rec_group(title="Live", secondary_types=["Live"]), rec_id="rec-live"),
        _recording(_rec_group(title="Comp", secondary_types=["Compilation"]), rec_id="rec-comp"),
    )
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        assert client.recording_search("Artist", "Title") is None


def test_recording_search_empty_is_no_match(db_conn: sqlite3.Connection) -> None:
    client, _ = _client(db_conn, [_json_response(_recording_body())])
    with client:
        assert client.recording_search("Nobody", "Nothing") is None


def test_recording_found_is_positive_cached(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_recording_body(_recording()))])
    with client:
        client.recording_search("Black Sabbath", "War Pigs")
        # Second call is served from cache — no second network request.
        again = client.recording_search("Black Sabbath", "War Pigs")
    assert again is not None
    assert again.album_title == "Paranoid"
    assert len(calls) == 1

    cached = get_cached_mb_recording(db_conn, _recording_request_key("Black Sabbath", "War Pigs"))
    assert cached is not None
    assert cached[0] is True


def test_recording_no_match_is_negative_cached(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_recording_body())])
    with client:
        client.recording_search("Nobody", "Nothing")
        client.recording_search("Nobody", "Nothing")
    assert len(calls) == 1  # the negative result is cached, not re-fetched
    cached = get_cached_mb_recording(db_conn, _recording_request_key("Nobody", "Nothing"))
    assert cached is not None
    assert cached[0] is False


def test_recording_http_error_raises_and_caches_nothing(db_conn: sqlite3.Connection) -> None:
    client, _ = _client(db_conn, [_json_response({}, status_code=503)])
    with client, pytest.raises(MusicBrainzError):
        client.recording_search("Artist", "Title")
    # Nothing cached → a re-run would retry.
    assert get_cached_mb_recording(db_conn, _recording_request_key("Artist", "Title")) is None


def test_recording_pacing_sleeps_between_network_requests(db_conn: sqlite3.Connection) -> None:
    clock = [0.0]
    slept: list[float] = []

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] += seconds

    client, _ = _client(
        db_conn,
        [_json_response(_recording_body(_recording())), _json_response(_recording_body())],
        rate_per_sec=1.0,
        monotonic=monotonic,
        sleep=sleep,
    )
    with client:
        client.recording_search("A", "One")
        client.recording_search("B", "Two")

    # The shared pacer honors 1 req/sec for the recording endpoint too.
    assert slept == [1.0]


# --- artist_by_mbid: the MBID -> canonical-name authority ----------------------------


def _artist_body(**overrides: object) -> dict[str, object]:
    """Build one artist lookup response as ``/ws/2/artist/<mbid>?inc=aliases`` shapes it."""
    body: dict[str, object] = {
        "id": "24ee4021-50ac-4285-b76e-860082d0d731",
        "name": "Lusine",
        "sort-name": "Lusine",
        "disambiguation": "",
        "aliases": [
            {"name": "Lusine ICL", "type": "Artist name"},
            {"name": "L\u2019Usine", "type": "Artist name"},
        ],
    }
    rename = {"sort_name": "sort-name"}
    for key, value in overrides.items():
        mb_key = rename.get(key, key)
        if value is None:
            body.pop(mb_key, None)
        else:
            body[mb_key] = value
    return body


def test_artist_by_mbid_returns_canonical_name_and_aliases(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_artist_body())])
    with client:
        artist = client.artist_by_mbid("24ee4021-50ac-4285-b76e-860082d0d731")

    assert artist is not None
    assert artist.name == "Lusine"
    assert artist.sort_name == "Lusine"
    assert artist.aliases == ("Lusine ICL", "L\u2019Usine")
    assert len(calls) == 1


def test_artist_by_mbid_queries_the_artist_endpoint_by_id(db_conn: sqlite3.Connection) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json_response(_artist_body())

    client = MusicBrainzClient(
        "TagMend/test ( test@example.com )",
        db_conn,
        rate_per_sec=0.0,
        transport=httpx.MockTransport(handle),
    )
    with client:
        client.artist_by_mbid("24ee4021-50ac-4285-b76e-860082d0d731")

    url = seen[0].url
    # A direct lookup by id, never a search: the file already supplies the identity.
    assert url.path.endswith("/ws/2/artist/24ee4021-50ac-4285-b76e-860082d0d731")
    assert url.params["inc"] == "aliases"
    assert url.params["fmt"] == "json"
    assert "query" not in url.params


def test_artist_by_mbid_second_call_is_served_from_cache(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_artist_body())])
    with client:
        first = client.artist_by_mbid("24ee4021-50ac-4285-b76e-860082d0d731")
        second = client.artist_by_mbid("24ee4021-50ac-4285-b76e-860082d0d731")

    assert first == second
    assert len(calls) == 1


def test_artist_by_mbid_caches_a_404_as_a_negative(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(
        db_conn,
        [httpx.Response(404, json={"error": "Not Found"})],
    )
    with client:
        assert client.artist_by_mbid("00000000-0000-0000-0000-000000000000") is None
        assert client.artist_by_mbid("00000000-0000-0000-0000-000000000000") is None

    assert len(calls) == 1  # the negative is cached, so no second request


def test_artist_by_mbid_raises_and_does_not_cache_a_transient_failure(
    db_conn: sqlite3.Connection,
) -> None:
    client, calls = _client(
        db_conn,
        [httpx.Response(503, text="busy"), _json_response(_artist_body())],
    )
    with client:
        with pytest.raises(MusicBrainzError):
            client.artist_by_mbid("24ee4021-50ac-4285-b76e-860082d0d731")
        # Not cached, so the retry goes back to the network and succeeds.
        artist = client.artist_by_mbid("24ee4021-50ac-4285-b76e-860082d0d731")

    assert artist is not None
    assert len(calls) == 2


def test_artist_by_mbid_tolerates_a_response_with_no_aliases(db_conn: sqlite3.Connection) -> None:
    client, _ = _client(db_conn, [_json_response(_artist_body(aliases=None))])
    with client:
        artist = client.artist_by_mbid("24ee4021-50ac-4285-b76e-860082d0d731")

    assert artist is not None
    assert artist.aliases == ()


def test_artist_by_mbid_returns_none_when_the_payload_has_no_name(
    db_conn: sqlite3.Connection,
) -> None:
    client, _ = _client(db_conn, [_json_response(_artist_body(name=None))])
    with client:
        assert client.artist_by_mbid("24ee4021-50ac-4285-b76e-860082d0d731") is None


def test_artist_by_mbid_carries_disambiguation(db_conn: sqlite3.Connection) -> None:
    client, _ = _client(
        db_conn,
        [_json_response(_artist_body(disambiguation="industrial metal band"))],
    )
    with client:
        artist = client.artist_by_mbid("24ee4021-50ac-4285-b76e-860082d0d731")

    assert artist is not None
    assert artist.disambiguation == "industrial metal band"


def test_artist_request_key_is_stable_and_id_scoped() -> None:
    assert _artist_request_key("abc") == _artist_request_key("abc")
    assert _artist_request_key("abc") != _artist_request_key("abd")


def test_bumping_the_artist_version_changes_the_request_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _artist_request_key("abc")
    monkeypatch.setattr(musicbrainz, "_ARTIST_VERSION", "99")
    assert _artist_request_key("abc") != before


def test_artist_by_mbid_paces_network_requests(db_conn: sqlite3.Connection) -> None:
    slept: list[float] = []
    now = [0.0]

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    client, _ = _client(
        db_conn,
        [_json_response(_artist_body()), _json_response(_artist_body(id="other"))],
        rate_per_sec=1.0,
        monotonic=lambda: now[0],
        sleep=fake_sleep,
    )
    with client:
        client.artist_by_mbid("mbid-one")
        client.artist_by_mbid("mbid-two")

    assert len(slept) == 1
    assert slept[0] == pytest.approx(1.0)


# --- release_by_mbid: the release's own tracklist -------------------------------------


def _track(position: int, number: str, title: str, **overrides: object) -> dict[str, object]:
    """Build one track entry as the release lookup shapes it."""
    entry: dict[str, object] = {
        "position": position,
        "number": number,
        "title": title,
        "id": f"rt-{position}",
        "length": 200000,
        "recording": {"id": f"rec-{position}", "title": title},
        "artist-credit": [
            {"name": "36 Crazyfists", "joinphrase": "", "artist": {"id": "artist-1"}}
        ],
    }
    entry.update(overrides)
    return entry


def _release_body(**overrides: object) -> dict[str, object]:
    """Build one release lookup response as ``/ws/2/release/<mbid>?inc=recordings`` shapes it."""
    body: dict[str, object] = {
        "id": "rel-1",
        "title": "In the Skin",
        "date": "1997",
        "country": "US",
        "status": "Official",
        "barcode": "12345",
        "artist-credit": [
            {"name": "36 Crazyfists", "joinphrase": "", "artist": {"id": "artist-1"}}
        ],
        "media": [
            {
                "position": 1,
                "title": "",
                "format": "CD",
                "track-count": 2,
                "tracks": [_track(1, "1", "Enemy Throttle"), _track(2, "2", "In the Skin")],
            },
        ],
    }
    body.update(overrides)
    return body


def test_release_by_mbid_returns_the_tracklist(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_release_body())])
    with client:
        release = client.release_by_mbid("rel-1")

    assert release is not None
    assert release.title == "In the Skin"
    assert release.date == "1997"
    assert release.country == "US"
    assert release.artist_credit == "36 Crazyfists"
    assert len(release.media) == 1
    medium = release.media[0]
    assert medium.position == 1
    assert medium.track_count == 2
    assert [t.title for t in medium.tracks] == ["Enemy Throttle", "In the Skin"]
    assert [t.number for t in medium.tracks] == ["1", "2"]
    assert medium.tracks[0].release_track_mbid == "rt-1"
    assert medium.tracks[0].recording_mbid == "rec-1"
    assert len(calls) == 1


def test_release_by_mbid_queries_the_release_endpoint_with_recordings(
    db_conn: sqlite3.Connection,
) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _json_response(_release_body())

    client = MusicBrainzClient(
        "TagMend/test ( test@example.com )",
        db_conn,
        rate_per_sec=0.0,
        transport=httpx.MockTransport(handle),
    )
    with client:
        client.release_by_mbid("rel-1")

    url = seen[0].url
    assert url.path.endswith("/ws/2/release/rel-1")
    assert url.params["inc"] == "recordings+artist-credits"
    assert url.params["fmt"] == "json"


def test_release_by_mbid_joins_a_multi_artist_credit_with_its_join_phrases(
    db_conn: sqlite3.Connection,
) -> None:
    credit = [
        {"name": "Kruder", "joinphrase": " & ", "artist": {"id": "a1"}},
        {"name": "Dorfmeister", "joinphrase": "", "artist": {"id": "a2"}},
    ]
    client, _ = _client(db_conn, [_json_response(_release_body(**{"artist-credit": credit}))])
    with client:
        release = client.release_by_mbid("rel-1")

    assert release is not None
    assert release.artist_credit == "Kruder & Dorfmeister"
    assert release.artist_mbids == ("a1", "a2")


def test_release_by_mbid_carries_a_per_track_credit_that_differs_from_the_release(
    db_conn: sqlite3.Connection,
) -> None:
    guest = [
        {"name": "36 Crazyfists", "joinphrase": " feat. ", "artist": {"id": "artist-1"}},
        {"name": "Guest", "joinphrase": "", "artist": {"id": "artist-2"}},
    ]
    body = _release_body()
    media = body["media"]
    assert isinstance(media, list)
    media[0]["tracks"][1]["artist-credit"] = guest
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        release = client.release_by_mbid("rel-1")

    assert release is not None
    assert release.media[0].tracks[1].artist_credit == "36 Crazyfists feat. Guest"
    assert release.media[0].tracks[0].artist_credit == "36 Crazyfists"


def test_release_by_mbid_indexes_tracks_by_the_ids_a_file_carries(
    db_conn: sqlite3.Connection,
) -> None:
    client, _ = _client(db_conn, [_json_response(_release_body())])
    with client:
        release = client.release_by_mbid("rel-1")

    assert release is not None
    # A tagged file carries both ids, so either one finds its track without guessing.
    by_release_track = release.track_by_release_track_mbid("rt-2")
    by_recording = release.track_by_recording_mbid("rec-1")
    assert by_release_track is not None
    assert by_release_track.title == "In the Skin"
    assert by_recording is not None
    assert by_recording.title == "Enemy Throttle"
    assert release.track_by_release_track_mbid("nope") is None


def test_release_by_mbid_second_call_is_served_from_cache(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [_json_response(_release_body())])
    with client:
        first = client.release_by_mbid("rel-1")
        second = client.release_by_mbid("rel-1")

    assert first == second
    assert len(calls) == 1


def test_release_by_mbid_caches_a_404_as_a_negative(db_conn: sqlite3.Connection) -> None:
    client, calls = _client(db_conn, [httpx.Response(404, json={"error": "Not Found"})])
    with client:
        assert client.release_by_mbid("gone") is None
        assert client.release_by_mbid("gone") is None

    assert len(calls) == 1


def test_release_by_mbid_raises_and_does_not_cache_a_transient_failure(
    db_conn: sqlite3.Connection,
) -> None:
    client, calls = _client(
        db_conn,
        [httpx.Response(503, text="busy"), _json_response(_release_body())],
    )
    with client:
        with pytest.raises(MusicBrainzError):
            client.release_by_mbid("rel-1")
        assert client.release_by_mbid("rel-1") is not None

    assert len(calls) == 2


def test_release_by_mbid_returns_none_when_the_payload_has_no_title(
    db_conn: sqlite3.Connection,
) -> None:
    body = _release_body()
    del body["title"]
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        assert client.release_by_mbid("rel-1") is None


def test_release_by_mbid_tolerates_a_release_with_no_media(db_conn: sqlite3.Connection) -> None:
    client, _ = _client(db_conn, [_json_response(_release_body(media=[]))])
    with client:
        release = client.release_by_mbid("rel-1")

    assert release is not None
    assert release.media == ()
    assert release.total_tracks == 0


def test_release_total_tracks_sums_every_medium(db_conn: sqlite3.Connection) -> None:
    body = _release_body()
    media = body["media"]
    assert isinstance(media, list)
    media.append(
        {
            "position": 2,
            "title": "Bonus",
            "format": "CD",
            "track-count": 1,
            "tracks": [_track(1, "1", "Extra")],
        },
    )
    client, _ = _client(db_conn, [_json_response(body)])
    with client:
        release = client.release_by_mbid("rel-1")

    assert release is not None
    assert release.total_tracks == 3
    assert release.media[1].title == "Bonus"


def test_release_request_key_is_stable_and_id_scoped() -> None:
    assert _release_request_key("abc") == _release_request_key("abc")
    assert _release_request_key("abc") != _release_request_key("abd")


def test_bumping_the_release_version_changes_the_request_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _release_request_key("abc")
    monkeypatch.setattr(musicbrainz, "_RELEASE_VERSION", "99")
    assert _release_request_key("abc") != before
