"""Integration tests for the song resolver (:mod:`tagmend.engine.songs`).

Every test runs the real engine on generated tracks with a real temp ledger. fpcalc never runs:
a fake runner returns one fingerprint per filename. AcoustID never answers from the network: a
real :class:`AcoustidClient` talks to an :class:`httpx.MockTransport` serving canned compressed
responses per fingerprint, built by :func:`_recording` and :func:`_body`. MusicBrainz is a fake
:class:`tagmend.engine.musicbrainz.MBReleaseSource` holding :class:`MBRelease` fixtures.
"""

from __future__ import annotations

import gzip
import json
import urllib.parse
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from conftest import make_track
from tagmend.engine import axis, library, songs, staging, store, versioning
from tagmend.engine.acoustid import AcoustidClient, Fingerprinter
from tagmend.engine.db import connect
from tagmend.engine.musicbrainz import MBMedium, MBRelease, MBTrack, MusicBrainzError
from tagmend.engine.schema import apply_schema
from tagmend.engine.tags import read_tags
from test_axis_status import _edit_on_disk
from test_health import _run_ok

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from tagmend.config import Settings

_DURATION = 200
_TITLES = ("Song One", "Song Two", "Song Three", "Song Four")
_LP = "rel-lp"


# --- fakes ---------------------------------------------------------------------------


class FakeFpcalc:
    """A fake fpcalc runner: one fingerprint per filename, or a failing exit."""

    def __init__(self, failures: Mapping[str, int] | None = None) -> None:
        self._failures = dict(failures or {})
        self.calls: list[str] = []

    def __call__(self, argv: Sequence[str], _timeout: float) -> tuple[int, str, str]:
        name = Path(argv[-1]).name
        self.calls.append(name)
        if name in self._failures:
            return self._failures[name], "", "ERROR: could not decode the file"
        fingerprint = {"fingerprint": f"fp-{name}", "duration": _DURATION + 0.4}
        return 0, json.dumps(fingerprint), ""


class FakeAcoustid:
    """A fake AcoustID server: a canned body per fingerprint, or a failing HTTP status."""

    def __init__(self, bodies: Mapping[str, dict[str, object]]) -> None:
        self.bodies = dict(bodies)
        self.statuses: dict[str, int] = {}
        self.requests: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        form = urllib.parse.parse_qs(gzip.decompress(request.content).decode("ascii"))
        fingerprint = form["fingerprint"][0]
        self.requests.append(fingerprint)
        if fingerprint in self.statuses:
            return httpx.Response(self.statuses[fingerprint])
        return httpx.Response(200, json=self.bodies.get(fingerprint, _body()))


class FakeReleases:
    """An in-memory :class:`tagmend.engine.musicbrainz.MBReleaseSource` that records lookups.

    The releases it holds act as the cache. A fresh lookup answers from ``upstream`` when it
    holds the release, as MusicBrainz would now, and replaces the cached one.
    """

    def __init__(self, *releases: MBRelease) -> None:
        self._releases = {release.mbid: release for release in releases}
        self.upstream: dict[str, MBRelease] = {}
        self.lookups: list[str] = []

    def release_by_mbid(self, mbid: str, *, fresh: bool = False) -> MBRelease | None:
        self.lookups.append(mbid)
        if fresh and mbid in self.upstream:
            self._releases[mbid] = self.upstream[mbid]
        return self._releases.get(mbid)


@dataclass
class Kit:
    """The three injected fakes one test drives the resolver with."""

    acoustid: FakeAcoustid
    releases: FakeReleases
    fpcalc: FakeFpcalc = field(default_factory=FakeFpcalc)


def _resolve(settings: Settings, kit: Kit, **kwargs: object) -> songs.ResolveSongsResult:
    transport = httpx.MockTransport(kit.acoustid.handle)
    with AcoustidClient("test-key", transport=transport, sleep=lambda _s: None) as client:
        return songs.resolve_songs(
            settings,
            fingerprinter=Fingerprinter("fpcalc", runner=kit.fpcalc, is_windows=False),
            acoustid_client=client,
            releases=kit.releases,
            **kwargs,  # type: ignore[arg-type]
        )


# --- canned AcoustID responses -------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """One place a recording sits: a release, its medium, the track position and the counts."""

    release: str
    position: int
    track_count: int = 4
    date: int = 2000


def _track_id(release: str, position: int) -> str:
    return f"{release}-t{position}"


def _recording(
    recording_id: str,
    title: str,
    *slots: Slot,
    sources: int = 20,
    duration: int = _DURATION,
    artists: Sequence[tuple[str, str]] = (),
) -> dict[str, object]:
    """Return one recording entry of a compressed AcoustID response.

    *artists* is the credit as ``(id, name)`` pairs. Without it the entry has no ``artists`` key.
    """
    credit = {"artists": [{"id": aid, "name": name} for aid, name in artists]} if artists else {}
    return credit | {
        "id": recording_id,
        "title": title,
        "duration": duration,
        "sources": sources,
        "releases": [
            {
                "id": slot.release,
                "title": f"{slot.release} title",
                "country": "US",
                "date": {"year": slot.date},
                "medium_count": 1,
                "track_count": slot.track_count,
                "mediums": [
                    {
                        "position": 1,
                        "format": "CD",
                        "track_count": slot.track_count,
                        "tracks": [
                            {
                                "id": _track_id(slot.release, slot.position),
                                "position": slot.position,
                            }
                        ],
                    },
                ],
            }
            for slot in slots
        ],
    }


def _body(*recordings: dict[str, object], score: float = 0.98) -> dict[str, object]:
    """Return a compressed AcoustID lookup body holding *recordings* in one match."""
    if not recordings:
        return {"status": "ok", "results": []}
    return {
        "status": "ok",
        "results": [{"id": "aid", "score": score, "recordings": list(recordings)}],
    }


def _release(
    mbid: str,
    titles: Sequence[str] = _TITLES,
    *,
    status: str = "Official",
    recordings: Sequence[str] | None = None,
) -> MBRelease:
    """Return a one-medium release whose track N plays recording ``rec-N``."""
    recording_ids = recordings or [f"rec-{n}" for n in range(1, len(titles) + 1)]
    tracks = tuple(
        MBTrack(
            position=n,
            number=str(n),
            title=title,
            release_track_mbid=_track_id(mbid, n),
            recording_mbid=recording_ids[n - 1],
            artist_credit="The Band",
            artist_sort="Band, The",
            artist_mbids=("art-band",),
        )
        for n, title in enumerate(titles, 1)
    )
    return MBRelease(
        mbid=mbid,
        title="LP",
        artist_credit="The Band",
        artist_sort="Band, The",
        artist_mbids=("art-band",),
        date="2000",
        country="US",
        status=status,
        barcode="0123",
        media=(
            MBMedium(position=1, title="", format="CD", track_count=len(tracks), tracks=tracks),
        ),
    )


def _lp_bodies(names: Sequence[str], *extra: Slot) -> dict[str, dict[str, object]]:
    """Return a body per file: file N is recording ``rec-N`` on the LP (and on *extra*)."""
    return {
        f"fp-{name}": _body(_recording(f"rec-{n}", _TITLES[n - 1], Slot(_LP, n), *extra))
        for n, name in enumerate(names, 1)
    }


# --- library helpers -----------------------------------------------------------------

_NAMES = tuple(f"0{n} {title}.flac" for n, title in enumerate(_TITLES, 1))
_BASE_TAGS: Mapping[str, Sequence[str]] = {"artist": ["The Band"], "album": ["LP"]}


def _make_folder(
    folder: Path,
    tags: Sequence[Mapping[str, Sequence[str]]],
    names: Sequence[str] = _NAMES,
    base: Mapping[str, Sequence[str]] = _BASE_TAGS,
) -> None:
    for name, file_tags in zip(names, tags, strict=False):
        make_track(folder / name, {**base, **file_tags})


def _ids(settings: Settings) -> dict[str, int]:
    return {view.filename: view.file_id for view in library.list_files(settings)}


def _status(settings: Settings, file_id: int) -> str:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return store.derived_status(conn, axis.SONG_AXIS, file_id)
    finally:
        conn.close()


def _scalar(settings: Settings, sql: str, *params: object) -> object:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def _diffs(settings: Settings) -> dict[str, staging.TagDiffView]:
    return {view.filename: view for view in staging.diff_tags(settings)}


def _blank_tracknumbers() -> list[dict[str, list[str]]]:
    """Two blank titles, blank track numbers, and one fully agreeing file."""
    return [
        {"title": ["Song One"]},
        {},
        {"title": ["Track 03"]},
        {"title": ["Song Four"], "tracknumber": ["4/4"]},
    ]


def _converging_kit() -> Kit:
    return Kit(acoustid=FakeAcoustid(_lp_bodies(_NAMES)), releases=FakeReleases(_release(_LP)))


# --- convergence route ---------------------------------------------------------------


def test_convergence_route_fills_blanks_and_records_done(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(music_dir / "LP", _blank_tracknumbers())
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)

    result = _resolve(engine_settings, _converging_kit())

    assert result.staged_files == 3
    assert result.verified_files == 1
    assert result.settled == 4
    diffs = _diffs(engine_settings)
    assert {view.origin for view in diffs.values()} == {"auto"}
    # A non-blank field that agrees is never restaged.
    assert diffs[_NAMES[0]].diff == {"tracknumber": {"from": [], "to": ["1/4"]}}
    assert diffs[_NAMES[1]].diff == {
        "title": {"from": [], "to": ["Song Two"]},
        "tracknumber": {"from": [], "to": ["2/4"]},
    }
    # A placeholder title counts as blank.
    assert diffs[_NAMES[2]].diff["title"] == {"from": ["Track 03"], "to": ["Song Three"]}
    assert _NAMES[3] not in diffs
    assert _status(engine_settings, ids[_NAMES[3]]) == "done"

    staging.commit_tags(engine_settings)

    assert [_status(engine_settings, ids[name]) for name in _NAMES] == ["done"] * 4
    assert read_tags(music_dir / "LP" / _NAMES[1]).tags["title"] == ["Song Two"]


def test_convergence_floor_holds_a_folder_too_few_voters_share(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    names = [f"0{n} Song {n}.flac" for n in range(1, 6)]
    _make_folder(music_dir / "Loose", [{}] * 5, names)
    library.scan_library(engine_settings)
    # Two voters share one release, the other three each sit on a release of their own.
    shared = {
        f"fp-{names[n - 1]}": _body(_recording(f"rec-{n}", f"Song {n}", Slot("rel-x", n, 5)))
        for n in (1, 2)
    }
    single = {
        f"fp-{names[n - 1]}": _body(_recording(f"rec-{n}", f"Song {n}", Slot(f"rel-{n}", 1, 1)))
        for n in (3, 4, 5)
    }
    # Every voter passes the gate and the shared release is Official, so only the family floor
    # stops the two sharing voters from filling.
    shared_release = _release("rel-x", [f"Song {n}" for n in range(1, 6)])
    kit = Kit(acoustid=FakeAcoustid(shared | single), releases=FakeReleases(shared_release))

    result = _resolve(engine_settings, kit)

    assert result.held_unconverged == 5
    assert result.staged_files == 0
    assert result.settled == 0
    assert staging.diff_tags(engine_settings) == []


# --- anchored route ------------------------------------------------------------------


def test_anchored_route_verifies_and_holds_a_disagreement(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    numbers = ["1/4", "2/4", "3/4", "9/4"]
    _make_folder(
        music_dir / "LP",
        [
            {
                "title": [_TITLES[n]],
                "tracknumber": [numbers[n]],
                "discnumber": ["1/1"],
                "musicbrainz_albumid": [_LP],
                "musicbrainz_releasetrackid": [_track_id(_LP, n + 1)],
            }
            for n in range(4)
        ],
    )
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)

    result = _resolve(engine_settings, _converging_kit())

    assert result.verified_files == 3
    assert result.staged_files == 0
    assert result.held_disagreement == 1
    held = result.held_values[0]
    assert held["file_id"] == ids[_NAMES[3]]
    assert held["have"] == {"tracknumber": "9/4"}
    assert held["want"] == {"tracknumber": "4/4"}
    assert held["release_mbid"] == _LP
    assert [_status(engine_settings, ids[name]) for name in _NAMES] == [
        "done",
        "done",
        "done",
        "pending",
    ]


def _anchored_lp_folder(music_dir: Path) -> None:
    """Make a folder whose four files carry their LP track's ids."""
    _make_folder(
        music_dir / "LP",
        [
            {
                "title": [_TITLES[n]],
                "tracknumber": [f"{n + 1}/4"],
                "discnumber": ["1/1"],
                "musicbrainz_albumid": [_LP],
                "musicbrainz_releasetrackid": [_track_id(_LP, n + 1)],
            }
            for n in range(4)
        ],
    )


def _off_release_kit(*indexes: int) -> Kit:
    """Return the LP kit with the files at *indexes* heard only on another release."""
    kit = _converging_kit()
    for n in indexes:
        kit.acoustid.bodies[f"fp-{_NAMES[n]}"] = _body(
            _recording(f"rec-off-{n}", _TITLES[n], Slot("rel-single", 1, 1)),
        )
    return kit


def test_a_manual_file_off_its_release_leaves_its_siblings_on_the_anchored_route(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _anchored_lp_folder(music_dir)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    songs.set_song_status(engine_settings, file_ids=[ids[_NAMES[0]]], status="manual")

    result = _resolve(engine_settings, _off_release_kit(0))

    assert result.rebind_folders == []
    assert result.skipped_manual == 1
    assert result.verified_files == 3
    assert [_status(engine_settings, ids[name]) for name in _NAMES] == [
        "manual",
        "done",
        "done",
        "done",
    ]


def test_a_pending_file_off_its_release_routes_rebind_and_alone_is_flagged(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _anchored_lp_folder(music_dir)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    songs.set_song_status(engine_settings, file_ids=[ids[_NAMES[0]]], status="manual")

    result = _resolve(engine_settings, _off_release_kit(0, 1))

    [rebind] = result.rebind_folders
    assert rebind["flagged_file_ids"] == [ids[_NAMES[1]]]
    assert result.verified_files == 0


def test_an_out_of_scope_pending_file_off_its_release_still_routes_rebind(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _anchored_lp_folder(music_dir)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)

    result = _resolve(
        engine_settings,
        _off_release_kit(3),
        file_ids=[ids[name] for name in _NAMES[:3]],
    )

    [rebind] = result.rebind_folders
    assert rebind["flagged_file_ids"] == [ids[_NAMES[3]]]
    assert result.verified_files == 0


# --- stamp check and the manual release path -----------------------------------------

_WRONG_STAMP = {
    "albumartist": ["Linkin Park"],
    "artistsort": ["Linkin Park & Adema"],
    "artists": ["Linkin Park"],
    "date": ["2008"],
    "discnumber": ["1/2"],
    "originaldate": ["2008"],
    "musicbrainz_albumid": ["rel-wrong"],
    "musicbrainz_releasegroupid": ["rg-wrong"],
}


def _wrong_stamp_kit() -> Kit:
    bodies = {
        f"fp-{name}": _body(
            _recording(f"rec-{n}", _TITLES[n - 1], Slot(_LP, n), Slot("rel-boot", n, date=1999)),
        )
        for n, name in enumerate(_NAMES, 1)
    }
    releases = FakeReleases(
        _release(_LP),
        _release("rel-boot", status="Bootleg"),
        _release(
            "rel-wrong",
            ["Numb", "Crawling", "Faint", "Papercut"],
            recordings=["x1", "x2", "x3", "x4"],
        ),
        _release("rel-three", _TITLES[:3]),
    )
    return Kit(acoustid=FakeAcoustid(bodies), releases=releases)


def _wrong_stamp_folder(music_dir: Path) -> Path:
    folder = music_dir / "Greatest Hits"
    _make_folder(
        folder,
        [
            {"title": [title], "tracknumber": [f"{n}/16"], **_WRONG_STAMP}
            for n, title in enumerate(_TITLES, 1)
        ],
    )
    return folder


def test_stamp_check_reports_a_rebind_with_ranked_candidates(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _wrong_stamp_folder(music_dir)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)

    result = _resolve(engine_settings, _wrong_stamp_kit())

    assert result.staged_files == 0
    assert result.settled == 0
    [rebind] = result.rebind_folders
    assert rebind["folder"] == str(folder)
    assert rebind["flagged_file_ids"] == [ids[name] for name in _NAMES]
    assert rebind["tagged_releases"] == [
        {"release_mbid": "rel-wrong", "title": "LP", "status": "official"},
    ]
    candidates = rebind["candidates"]
    assert isinstance(candidates, list)
    # The bootleg is earlier, so it is fetched first, but the Official release ranks first.
    assert [(c["release_mbid"], c["status"]) for c in candidates] == [
        (_LP, "official"),
        ("rel-boot", "bootleg"),
    ]
    assert staging.diff_tags(engine_settings) == []


def test_a_rebind_folder_reports_an_ungated_targets_fpcalc_error(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _wrong_stamp_folder(music_dir)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    kit = replace(_wrong_stamp_kit(), fpcalc=FakeFpcalc(failures={_NAMES[3]: 2}))

    result = _resolve(engine_settings, kit)

    [rebind] = result.rebind_folders
    assert rebind["flagged_file_ids"] == [ids[name] for name in _NAMES[:3]]
    [item] = result.error_items
    assert item["key"] == str(ids[_NAMES[3]])
    assert "exited 2" in item["message"]
    assert result.held_values == []


def test_a_gate_failure_is_held_as_no_contribution_with_its_reason(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(music_dir / "LP", _blank_tracknumbers())
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    kit = _converging_kit()
    # The only recording heard is a live take the filename does not name.
    kit.acoustid.bodies[f"fp-{_NAMES[3]}"] = _body(
        _recording("rec-4", "Song Four (Live)", Slot(_LP, 4)),
    )

    result = _resolve(engine_settings, kit)

    assert result.staged_files == 3
    assert result.held_no_contribution == 1
    assert result.to_dict()["held_no_contribution"] == 1
    [held] = result.held_values
    assert held["file_id"] == ids[_NAMES[3]]
    assert held["reason"] == "qualifier_mismatch"
    assert _status(engine_settings, ids[_NAMES[3]]) == "pending"


def test_manual_release_path_stages_the_whole_stamp_in_one_batch(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _wrong_stamp_folder(music_dir)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)

    result = _resolve(engine_settings, _wrong_stamp_kit(), folder=str(folder), release_mbid=_LP)

    assert result.staged_files == 4
    assert result.unassigned == []
    assert result.release is not None
    assert result.release["release_mbid"] == _LP
    diffs = _diffs(engine_settings)
    assert {view.origin for view in diffs.values()} == {"manual"}
    target = diffs[_NAMES[1]].target
    assert target["title"] == ["Song Two"]
    assert target["tracknumber"] == ["2/4"]
    assert target["discnumber"] == ["1/1"]
    assert target["artist"] == ["The Band"]
    assert target["artistsort"] == ["Band, The"]
    assert target["albumartist"] == ["The Band"]
    assert target["albumartistsort"] == ["Band, The"]
    assert target["musicbrainz_artistid"] == ["art-band"]
    assert target["musicbrainz_albumid"] == [_LP]
    assert target["musicbrainz_releasetrackid"] == [_track_id(_LP, 2)]
    assert target["musicbrainz_trackid"] == ["rec-2"]
    assert target["date"] == ["2000"]
    assert target["musicbrainz_albumstatus"] == ["official"]
    for cleared in ("artists", "originaldate", "musicbrainz_releasegroupid"):
        assert not target.get(cleared)
    assert diffs[_NAMES[1]].stale_identity == []

    commit = staging.commit_tags(engine_settings)

    assert commit.committed == 4
    on_disk = read_tags(folder / _NAMES[1]).tags
    assert on_disk["albumartist"] == ["The Band"]
    assert "originaldate" not in on_disk
    # The commit writer records the human decision on the song fields it changed.
    assert _status(engine_settings, ids[_NAMES[1]]) == "manual"


@pytest.mark.parametrize(
    ("album_id", "date"),
    [
        pytest.param(_LP, ["2007-05-22"], id="own-release-keeps"),
        pytest.param("rel-wrong", [], id="rebind-clears"),
    ],
)
def test_manual_release_path_keeps_a_date_only_on_the_files_own_dateless_release(
    engine_settings: Settings,
    music_dir: Path,
    album_id: str,
    date: list[str],
) -> None:
    folder = music_dir / "LP"
    stamp = {"date": ["2007-05-22"], "musicbrainz_albumid": [album_id]}
    _make_folder(
        folder,
        [
            {"title": [title], "tracknumber": [str(n)], **stamp}
            for n, title in enumerate(_TITLES, 1)
        ],
    )
    library.scan_library(engine_settings)
    dateless = replace(_release(_LP), date="")
    kit = Kit(acoustid=FakeAcoustid(_lp_bodies(_NAMES)), releases=FakeReleases(dateless))

    result = _resolve(engine_settings, kit, folder=str(folder), release_mbid=_LP)

    assert result.staged_files == 4
    assert _diffs(engine_settings)[_NAMES[1]].target.get("date", []) == date


_GROUP_FIELDS = (
    "musicbrainz_releasegroupid",
    "musicbrainz_albumtype",
    "catalognumber",
    "asin",
    "isrc",
    "originaldate",
)


def _group_release() -> MBRelease:
    """Return the LP with its release group, two catalog numbers, an ASIN and ISRCs."""
    release = _release(_LP)
    medium = release.media[0]
    tracks = tuple(
        replace(track, isrcs=(f"USAAA03000{track.position}",)) for track in medium.tracks
    )
    return replace(
        release,
        media=(replace(medium, tracks=tracks),),
        release_group_mbid="rg-lp",
        release_types=("album", "compilation"),
        first_release_date="2003-11-07",
        catalog_numbers=("CAT-1", "CAT-2"),
        asin="B00001",
    )


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        pytest.param(
            _group_release(),
            {
                "musicbrainz_releasegroupid": ["rg-lp"],
                "musicbrainz_albumtype": ["album", "compilation"],
                "catalognumber": ["CAT-1", "CAT-2"],
                "asin": ["B00001"],
                "isrc": ["USAAA030002"],
                "originaldate": ["2003-11-07"],
            },
            id="writes-the-group-block",
        ),
        pytest.param(_release(_LP), {name: [] for name in _GROUP_FIELDS}, id="clears-each-field"),
    ],
)
def test_a_rebind_stamp_writes_or_clears_the_release_group_label_and_isrc_fields(
    engine_settings: Settings,
    music_dir: Path,
    release: MBRelease,
    expected: dict[str, list[str]],
) -> None:
    folder = music_dir / "Greatest Hits"
    held = {
        "musicbrainz_albumtype": ["single"],
        "catalognumber": ["OLD-1"],
        "asin": ["B0OLD"],
        "isrc": ["GBOLD0000001"],
    }
    _make_folder(
        folder,
        [
            {"title": [title], "tracknumber": [f"{n}/16"], **_WRONG_STAMP, **held}
            for n, title in enumerate(_TITLES, 1)
        ],
    )
    library.scan_library(engine_settings)
    kit = _wrong_stamp_kit()
    kit.releases = FakeReleases(release)

    result = _resolve(engine_settings, kit, folder=str(folder), release_mbid=_LP)

    assert result.staged_files == 4
    target = _diffs(engine_settings)[_NAMES[1]].target
    assert {name: target.get(name, []) for name in _GROUP_FIELDS} == expected


@pytest.mark.parametrize(
    ("held_date", "group_date", "originaldate"),
    [
        pytest.param("1997-10", "1998-01-01", ["1997-10"], id="keeps-a-different-date"),
        pytest.param("2003", "2003-11-07", ["2003-11-07"], id="refines-a-bare-year"),
        pytest.param("2003-11-07", "2003", ["2003-11-07"], id="never-loses-precision"),
    ],
)
def test_a_same_release_stamp_keeps_the_files_own_fields_and_only_refines_its_date(
    engine_settings: Settings,
    music_dir: Path,
    held_date: str,
    group_date: str,
    originaldate: list[str],
) -> None:
    folder = music_dir / "LP"
    held = {
        "musicbrainz_albumid": [_LP],
        "asin": ["B0OWN"],
        "catalognumber": ["OWN-1"],
        "isrc": ["GBOWN0000001"],
        "originaldate": [held_date],
    }
    _make_folder(
        folder,
        [
            {"title": [title], "tracknumber": [str(n)], "musicbrainz_trackid": [f"rec-{n}"], **held}
            for n, title in enumerate(_TITLES, 1)
        ],
    )
    library.scan_library(engine_settings)
    release = replace(_release(_LP), first_release_date=group_date)
    kit = Kit(acoustid=FakeAcoustid(_lp_bodies(_NAMES)), releases=FakeReleases(release))

    result = _resolve(engine_settings, kit, folder=str(folder), release_mbid=_LP)

    assert result.staged_files == 4
    target = _diffs(engine_settings)[_NAMES[1]].target
    assert target["asin"] == ["B0OWN"]
    assert target["catalognumber"] == ["OWN-1"]
    # The file already names this recording, which the release lists no ISRC for.
    assert target["isrc"] == ["GBOWN0000001"]
    assert target["originaldate"] == originaldate


@pytest.mark.parametrize(
    ("credit", "mbids", "names"),
    [
        pytest.param("A vs. B", ("a-1", "a-2"), ("A", "B"), id="two-artists"),
        pytest.param("A", ("a-1",), ("A",), id="one-artist"),
    ],
)
def test_a_stamp_writes_artists_aligned_with_the_artist_ids(
    engine_settings: Settings,
    music_dir: Path,
    credit: str,
    mbids: tuple[str, ...],
    names: tuple[str, ...],
) -> None:
    folder = _wrong_stamp_folder(music_dir)
    library.scan_library(engine_settings)
    kit = _wrong_stamp_kit()
    release = _release(_LP)
    medium = release.media[0]
    tracks = tuple(
        replace(track, artist_credit=credit, artist_mbids=mbids, artist_names=names)
        for track in medium.tracks
    )
    kit.releases = FakeReleases(replace(release, media=(replace(medium, tracks=tracks),)))

    result = _resolve(engine_settings, kit, folder=str(folder), release_mbid=_LP)

    assert result.staged_files == 4
    target = _diffs(engine_settings)[_NAMES[1]].target
    assert target["artists"] == list(names)
    assert target["musicbrainz_artistid"] == list(mbids)


def test_manual_release_path_stamps_the_track_ids_musicbrainz_lists_now(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _wrong_stamp_folder(music_dir)
    library.scan_library(engine_settings)
    kit = _wrong_stamp_kit()
    current = _release(_LP)
    medium = current.media[0]
    retired = tuple(
        replace(track, release_track_mbid=f"{track.release_track_mbid}-retired")
        for track in medium.tracks
    )
    kit.releases = FakeReleases(replace(current, media=(replace(medium, tracks=retired),)))
    kit.releases.upstream[_LP] = current

    preview = _resolve(engine_settings, kit, folder=str(folder), release_mbid=_LP, dry_run=True)
    result = _resolve(engine_settings, kit, folder=str(folder), release_mbid=_LP)

    # The dry run reads the cached tracklist, whose retired ids AcoustID no longer names.
    assert preview.unassigned is not None
    assert {row["reason"] for row in preview.unassigned} == {"not_on_release"}
    assert result.staged_files == 4
    target = _diffs(engine_settings)[_NAMES[1]].target
    assert target["musicbrainz_releasetrackid"] == [_track_id(_LP, 2)]


def test_manual_release_path_stages_nothing_when_a_file_is_not_on_the_release(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _wrong_stamp_folder(music_dir)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    kit = _wrong_stamp_kit()
    # The first three recordings also sit on a three-track release, the fourth does not.
    kit.acoustid.bodies = {
        f"fp-{name}": _body(
            _recording(
                f"rec-{n}",
                _TITLES[n - 1],
                Slot(_LP, n),
                *([Slot("rel-three", n, 3)] if name != _NAMES[3] else []),
            ),
        )
        for n, name in enumerate(_NAMES, 1)
    }

    result = _resolve(engine_settings, kit, folder=str(folder), release_mbid="rel-three")

    assert result.staged_files == 0
    assert result.unassigned == [
        {"file_id": ids[_NAMES[3]], "filename": _NAMES[3], "reason": "not_on_release"},
    ]
    assert staging.diff_tags(engine_settings) == []


def test_manual_release_path_assigns_a_gate_failure_corroborated_by_its_stem(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(music_dir / "Pair", [{}, {}], _NAMES[:2])
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    # The second file's audio is split 60/40, so the auto gate fails it on share.
    bodies = _lp_bodies(_NAMES[:1])
    bodies[f"fp-{_NAMES[1]}"] = _body(
        _recording("rec-other", "Other Song", sources=60),
        _recording("rec-2", "Song Two", Slot(_LP, 2), sources=40),
    )
    kit = Kit(acoustid=FakeAcoustid(bodies), releases=FakeReleases(_release(_LP)))

    auto = _resolve(engine_settings, kit, dry_run=True)
    manual = _resolve(engine_settings, kit, file_ids=list(ids.values()), release_mbid=_LP)

    # Only the first file passes the gate, so the auto tier holds it and stages nothing.
    assert auto.held_unconverged == 1
    assert auto.staged_files == 0
    assert manual.unassigned == []
    assert manual.staged_files == 2
    assert _diffs(engine_settings)[_NAMES[1]].target["title"] == ["Song Two"]


def test_manual_release_path_reports_a_lookup_error_in_error_items(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _wrong_stamp_folder(music_dir)
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)[_NAMES[3]]
    kit = _wrong_stamp_kit()
    kit.acoustid.statuses[f"fp-{_NAMES[3]}"] = 503

    result = _resolve(engine_settings, kit, folder=str(folder), release_mbid=_LP)

    assert result.unassigned == [{"file_id": file_id, "filename": _NAMES[3], "reason": "error"}]
    assert result.errors == 1
    [item] = result.error_items
    assert item["key"] == str(file_id)
    assert "503" in item["message"]
    assert result.staged_files == 0


def test_release_mbid_without_a_scope_is_rejected(engine_settings: Settings) -> None:
    with pytest.raises(ValueError, match="release_mbid needs folder or file_ids"):
        songs.resolve_songs(engine_settings, release_mbid=_LP)


# --- operator track assignments on the manual release path ---------------------------


def _reprise_kit(fourth_length: int | None = None) -> Kit:
    """Return a kit whose fourth file's recording sits on two tracks of a five-track LP.

    Track 5 reprises track 4's recording, so the audio alone leaves the fourth file
    ``ambiguous_slot``. *fourth_length* is track 4's MusicBrainz length in seconds.
    """
    release = _release(
        _LP,
        (*_TITLES, "Song Four (Reprise)"),
        recordings=["rec-1", "rec-2", "rec-3", "rec-4", "rec-4"],
    )
    medium = release.media[0]
    tracks = tuple(
        replace(track, length_seconds=fourth_length) if track.position == 4 else track
        for track in medium.tracks
    )
    bodies = _lp_bodies(_NAMES[:3])
    bodies[f"fp-{_NAMES[3]}"] = _body(
        _recording("rec-4", _TITLES[3], Slot(_LP, 4, 5), Slot(_LP, 5, 5)),
    )
    return Kit(
        acoustid=FakeAcoustid(bodies),
        releases=FakeReleases(replace(release, media=(replace(medium, tracks=tracks),))),
    )


def _reprise_folder(music_dir: Path) -> Path:
    folder = music_dir / "LP"
    _make_folder(folder, [{} for _ in _NAMES])
    return folder


def _voter(settings: Settings, file_id: int) -> songs._Voter:
    conn = connect(settings.db_path)
    try:
        apply_schema(conn)
        row = store.get_file_by_id(conn, file_id)
        assert row is not None
        return songs._Voter(
            row=row, tags=store.get_tags(conn, file_id), target=True, unsettled=True
        )
    finally:
        conn.close()


def test_an_assigned_track_places_an_ambiguous_file_and_stages_the_whole_stamp(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _reprise_folder(music_dir)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    fourth = ids[_NAMES[3]]
    kit = _reprise_kit()
    assignments = [(fourth, _track_id(_LP, 4))]
    scope = {"folder": str(folder), "release_mbid": _LP}

    unaided = _resolve(engine_settings, kit, **scope, dry_run=True)
    preview = _resolve(engine_settings, kit, **scope, assignments=assignments, dry_run=True)
    result = _resolve(engine_settings, kit, **scope, assignments=assignments)

    assert unaided.unassigned == [
        {"file_id": fourth, "filename": _NAMES[3], "reason": "ambiguous_slot"},
    ]
    assert {row["file_id"]: row["placed_by"] for row in preview.mappings} == {
        **{ids[name]: "audio" for name in _NAMES[:3]},
        fourth: "operator",
    }
    release = kit.releases.release_by_mbid(_LP)
    assert release is not None
    track = release.track_by_release_track_mbid(_track_id(_LP, 4))
    assert track is not None
    expected = songs._stamp(release, track, _voter(engine_settings, fourth))
    [mapping] = [row for row in preview.mappings if row["file_id"] == fourth]
    assert mapping["tags"] == expected
    assert result.unassigned == []
    assert result.staged_files == 4
    target = _diffs(engine_settings)[_NAMES[3]].target
    assert {name: target.get(name, []) for name in expected} == expected
    assert target["tracknumber"] == ["4/5"]


@pytest.mark.parametrize(
    ("assigned", "scope", "reason"),
    [
        pytest.param([(_NAMES[3], "no-such-track")], None, "does not list", id="track-off-release"),
        pytest.param(
            [(_NAMES[3], _track_id(_LP, 4))],
            _NAMES[:3],
            "not in the call's scope",
            id="file-out-of-scope",
        ),
        pytest.param(
            [(_NAMES[3], _track_id(_LP, 4)), (_NAMES[2], _track_id(_LP, 4))],
            None,
            "also claim",
            id="track-assigned-twice",
        ),
        pytest.param(
            [(_NAMES[3], _track_id(_LP, 3))],
            None,
            "also claim",
            id="track-the-audio-places-a-sibling-on",
        ),
    ],
)
def test_a_refused_assignment_names_the_file_and_stages_nothing(
    engine_settings: Settings,
    music_dir: Path,
    assigned: list[tuple[str, str]],
    scope: tuple[str, ...] | None,
    reason: str,
) -> None:
    folder = _reprise_folder(music_dir)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    assignments = [(ids[name], track) for name, track in assigned]
    where: dict[str, object] = (
        {"folder": str(folder)} if scope is None else {"file_ids": [ids[name] for name in scope]}
    )

    with pytest.raises(ValueError, match=reason) as refused:
        _resolve(
            engine_settings, _reprise_kit(), release_mbid=_LP, assignments=assignments, **where
        )

    assert f"file_id={ids[_NAMES[3]]} " in str(refused.value)
    assert staging.diff_tags(engine_settings) == []


def test_an_assigned_track_far_from_the_files_length_is_refused(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _reprise_folder(music_dir)
    library.scan_library(engine_settings)
    fourth = _ids(engine_settings)[_NAMES[3]]
    kit = _reprise_kit(fourth_length=_DURATION + 30)

    with pytest.raises(ValueError, match=f"file_id={fourth} runs {_DURATION}s"):
        _resolve(
            engine_settings,
            kit,
            folder=str(folder),
            release_mbid=_LP,
            assignments=[(fourth, _track_id(_LP, 4))],
        )

    assert staging.diff_tags(engine_settings) == []


def test_an_assigned_file_with_no_fingerprint_skips_only_the_length_check(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = _reprise_folder(music_dir)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    fourth = ids[_NAMES[3]]
    kit = replace(
        _reprise_kit(fourth_length=_DURATION + 30),
        fpcalc=FakeFpcalc(failures={_NAMES[3]: 2}),
    )
    scope = {"folder": str(folder), "release_mbid": _LP}

    with pytest.raises(ValueError, match=f"file_id={fourth} .* also claim"):
        _resolve(engine_settings, kit, **scope, assignments=[(fourth, _track_id(_LP, 3))])
    result = _resolve(engine_settings, kit, **scope, assignments=[(fourth, _track_id(_LP, 4))])

    assert result.unassigned == []
    assert result.error_items == []
    assert result.staged_files == 4
    target = _diffs(engine_settings)[_NAMES[3]].target
    assert target["musicbrainz_releasetrackid"] == [_track_id(_LP, 4)]


@pytest.mark.parametrize(
    ("release_mbid", "assignments", "error"),
    [
        pytest.param(None, [(1, "t-1")], "assignments needs release_mbid", id="no-release"),
        pytest.param(_LP, [{"file_id": 1}], r"expected a \(file_id", id="not-a-pair"),
        pytest.param(_LP, [("1", "t-1")], "file_id must be an integer", id="file-id-text"),
        pytest.param(_LP, [(1, " ")], "release_track_mbid must be an id", id="blank-track"),
        pytest.param(_LP, [(1, "t-1"), (1, "t-2")], "assigned more than once", id="file-twice"),
    ],
)
def test_malformed_assignments_are_rejected_before_any_lookup(
    engine_settings: Settings,
    release_mbid: str | None,
    assignments: list[object],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        songs.resolve_songs(
            engine_settings,
            folder="LP",
            release_mbid=release_mbid,
            assignments=assignments,
        )


# --- transient errors and the caches -------------------------------------------------


def test_acoustid_503_leaves_the_file_pending_and_caches_nothing(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(music_dir / "LP", [{}] * 4)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    kit = _converging_kit()
    kit.acoustid.statuses[f"fp-{_NAMES[3]}"] = 503

    result = _resolve(engine_settings, kit)

    assert result.staged_files == 3
    assert result.errors == 1
    assert result.error_items[0]["key"] == str(ids[_NAMES[3]])
    assert "503" in result.error_items[0]["message"]
    assert _status(engine_settings, ids[_NAMES[3]]) == "pending"
    assert _scalar(engine_settings, "SELECT COUNT(*) FROM acoustid_cache") == 3


def test_empty_answer_is_cached_stores_no_status_and_is_asked_again_after_a_week(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(music_dir / "LP", [{}], _NAMES[:1])
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)[_NAMES[0]]
    kit = Kit(acoustid=FakeAcoustid({}), releases=FakeReleases())

    first = _resolve(engine_settings, kit)
    warm = _resolve(engine_settings, kit)

    assert first.lookup_empty == 1
    assert warm.lookup_empty == 1
    assert _scalar(engine_settings, "SELECT found FROM acoustid_cache") == 0
    assert _scalar(engine_settings, "SELECT COUNT(*) FROM file_song_status") == 0
    assert _status(engine_settings, file_id) == "pending"
    assert len(kit.acoustid.requests) == 1
    assert kit.fpcalc.calls == [_NAMES[0]]

    week_ago = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    conn = connect(engine_settings.db_path)
    try:
        conn.execute("UPDATE acoustid_cache SET fetched_at = ?", (week_ago,))
        conn.commit()
    finally:
        conn.close()
    _resolve(engine_settings, kit)

    assert len(kit.acoustid.requests) == 2
    assert kit.fpcalc.calls == [_NAMES[0]]


def test_stored_fpcalc_failure_makes_its_folder_warm(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(music_dir / "LP", [{}], _NAMES[:1])
    library.scan_library(engine_settings)
    kit = Kit(
        acoustid=FakeAcoustid({}),
        releases=FakeReleases(),
        fpcalc=FakeFpcalc(failures={_NAMES[0]: 2}),
    )

    cold = _resolve(engine_settings, kit)
    warm = _resolve(engine_settings, kit, limit=0)

    assert "exited 2" in cold.error_items[0]["message"]
    assert "exited 2" in warm.error_items[0]["message"]
    assert warm.cold_folders_remaining == 0
    assert kit.fpcalc.calls == [_NAMES[0]]
    assert kit.acoustid.requests == []


def test_an_fpcalc_failure_on_a_file_gone_since_the_scan_is_never_stored(
    engine_settings: Settings,
    music_dir: Path,
    tmp_path: Path,
) -> None:
    # A drive that drops offline mid-run would otherwise mark every remaining file as bad audio.
    _make_folder(music_dir / "LP", [{}], _NAMES[:1])
    library.scan_library(engine_settings)
    track = music_dir / "LP" / _NAMES[0]
    away = tmp_path / _NAMES[0]
    kit = Kit(
        acoustid=FakeAcoustid({}),
        releases=FakeReleases(),
        fpcalc=FakeFpcalc(failures={_NAMES[0]: 2}),
    )

    track.rename(away)
    offline = _resolve(engine_settings, kit)
    stored_while_offline = _scalar(engine_settings, "SELECT COUNT(*) FROM fingerprint_cache")
    away.rename(track)
    back_kit = Kit(acoustid=FakeAcoustid({}), releases=FakeReleases())
    back = _resolve(engine_settings, back_kit)

    assert "could not be opened" in offline.error_items[0]["message"]
    assert kit.fpcalc.calls == [_NAMES[0]]
    assert stored_while_offline == 0
    assert back_kit.fpcalc.calls == [_NAMES[0]]
    assert back.error_items == []
    assert back.lookup_empty == 1
    assert _scalar(engine_settings, "SELECT fpcalc_exit FROM fingerprint_cache") == 0


def test_limit_counts_cold_folders_and_warm_folders_run_free(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    names = ["a.flac", "b.flac", "c.flac"]
    for folder, name in zip(("A", "B", "C"), names, strict=True):
        make_track(music_dir / folder / name, {"artist": ["The Band"]})
    library.scan_library(engine_settings)
    bodies = {
        f"fp-{name}": _body(_recording(f"rec-{n}", f"Song {n}", Slot(f"rel-{n}", 1, 1)))
        for n, name in enumerate(names, 1)
    }
    kit = Kit(acoustid=FakeAcoustid(bodies), releases=FakeReleases())

    runs = [_resolve(engine_settings, kit, limit=1, dry_run=True) for _ in range(3)]

    assert kit.fpcalc.calls == names
    assert [run.cold_folders_remaining for run in runs] == [2, 1, 0]
    assert [run.more for run in runs] == [True, True, False]

    fpcalc_before, requests_before = len(kit.fpcalc.calls), len(kit.acoustid.requests)
    warm = _resolve(engine_settings, kit, limit=0, dry_run=True)

    assert warm.held_unconverged == 3  # every folder ran, each holding a release lookup failure
    assert len(kit.fpcalc.calls) == fpcalc_before
    assert len(kit.acoustid.requests) == requests_before


# --- voters --------------------------------------------------------------------------


def test_a_settled_sibling_still_votes_when_a_lookup_recovers(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(music_dir / "LP", [{}] * 4)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    # The fourth recording also sits on an earlier ten-track compilation. Alone it names two
    # slots, so only the settled siblings' votes narrow it to the LP.
    bodies = _lp_bodies(_NAMES)
    bodies[f"fp-{_NAMES[3]}"] = _body(
        _recording("rec-4", "Song Four", Slot(_LP, 4), Slot("rel-comp", 7, 10, date=1990)),
    )
    kit = Kit(
        acoustid=FakeAcoustid(bodies),
        releases=FakeReleases(_release(_LP), _release("rel-comp", ["x"] * 10)),
    )
    kit.acoustid.statuses[f"fp-{_NAMES[3]}"] = 503

    first = _resolve(engine_settings, kit)
    staging.commit_tags(engine_settings)
    del kit.acoustid.statuses[f"fp-{_NAMES[3]}"]
    second = _resolve(engine_settings, kit)

    assert first.staged_files == 3
    assert second.held_unconverged == 0
    assert second.staged_files == 1
    assert _diffs(engine_settings)[_NAMES[3]].diff["tracknumber"] == {"from": [], "to": ["4/4"]}
    assert _status(engine_settings, ids[_NAMES[0]]) == "done"


# --- the artist review row -----------------------------------------------------------

_ALBUM_ONLY: Mapping[str, Sequence[str]] = {"album": ["LP"]}
_MOBY = ("art-moby", "Moby")


class FailingReleases(FakeReleases):
    """A release source whose every lookup fails transiently."""

    def release_by_mbid(self, mbid: str, *, fresh: bool = False) -> MBRelease | None:
        self.lookups.append(mbid)
        message = f"MusicBrainz answered HTTP 503 for {mbid}"
        raise MusicBrainzError(message)


def _credited(n: int, *artists: tuple[str, str], sources: int = 20) -> dict[str, object]:
    """Return recording ``rec-N`` on the LP crediting *artists*."""
    return _recording(f"rec-{n}", _TITLES[n - 1], Slot(_LP, n), sources=sources, artists=artists)


def _credited_bodies(first: Sequence[dict[str, object]]) -> dict[str, dict[str, object]]:
    """Return a body per file: *first* for the first file, one Moby credit for every other."""
    bodies = {f"fp-{name}": _body(_credited(n, _MOBY)) for n, name in enumerate(_NAMES, 1)}
    bodies[f"fp-{_NAMES[0]}"] = _body(*first)
    return bodies


def _artist_rows(result: songs.ResolveSongsResult) -> list[dict[str, object]]:
    return [row for row in result.review_values if row["field"] == "artist"]


def test_an_artist_less_folder_gets_one_artist_review_row_per_file_and_stages_none(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    folder = music_dir / "LP"
    _make_folder(folder, _blank_tracknumbers(), base=_ALBUM_ONLY)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    # The first file's audio names two recordings crediting one artist, one name padded.
    first = [
        _credited(1, ("art-moby", " Moby ")),
        _recording("rec-1-b", _TITLES[0], artists=[_MOBY]),
    ]
    kit = Kit(
        acoustid=FakeAcoustid(_credited_bodies(first)),
        releases=FakeReleases(_release(_LP)),
    )

    result = _resolve(engine_settings, kit)

    # Three fills and one verified file, so the row does not depend on the song outcome.
    assert (result.staged_files, result.verified_files) == (3, 1)
    assert _artist_rows(result) == [
        {
            "file_id": ids[name],
            "folder": str(folder),
            "filename": name,
            "field": "artist",
            "proposal": "Moby",
            "musicbrainz_artistid": "art-moby",
        }
        for name in _NAMES
    ]
    assert result.review_files == 4
    for view in _diffs(engine_settings).values():
        assert not {"artist", "albumartist", "musicbrainz_artistid"} & set(view.diff)


@pytest.mark.parametrize(
    ("tags", "first"),
    [
        pytest.param({"artist": ["The Band"]}, [_credited(1, _MOBY)], id="artist_tag"),
        pytest.param({"albumartist": ["The Band"]}, [_credited(1, _MOBY)], id="albumartist_only"),
        pytest.param({}, [_credited(1, _MOBY, ("art-other", "Other"))], id="two_artist_credit"),
        pytest.param(
            {},
            [
                _credited(1, _MOBY),
                _recording("rec-1-b", _TITLES[0], artists=[("art-other", "Moby")]),
            ],
            id="differing_ids",
        ),
        pytest.param({}, [_credited(1)], id="no_credit"),
        pytest.param(
            {},
            [
                _recording("rec-x", "Other Song", sources=60, artists=[_MOBY]),
                _credited(1, _MOBY, sources=40),
            ],
            id="ungated",
        ),
    ],
)
def test_a_blocked_file_gets_no_artist_review_row(
    engine_settings: Settings,
    music_dir: Path,
    tags: Mapping[str, Sequence[str]],
    first: Sequence[dict[str, object]],
) -> None:
    _make_folder(music_dir / "LP", [tags, {}, {}, {}], base=_ALBUM_ONLY)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    kit = Kit(
        acoustid=FakeAcoustid(_credited_bodies(first)),
        releases=FakeReleases(_release(_LP)),
    )

    result = _resolve(engine_settings, kit)

    assert [row["file_id"] for row in _artist_rows(result)] == [ids[n] for n in _NAMES[1:]]


def test_a_rebind_folder_gets_no_artist_review_row(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(
        music_dir / "Greatest Hits",
        [
            {"title": [title], "tracknumber": [f"{n}/16"], "musicbrainz_albumid": ["rel-wrong"]}
            for n, title in enumerate(_TITLES, 1)
        ],
        base=_ALBUM_ONLY,
    )
    library.scan_library(engine_settings)
    wrong = _release("rel-wrong", ["Numb", "Crawling", "Faint", "Papercut"], recordings=["x1"] * 4)
    kit = Kit(
        acoustid=FakeAcoustid(_credited_bodies([_credited(1, _MOBY)])),
        releases=FakeReleases(_release(_LP), wrong),
    )

    result = _resolve(engine_settings, kit)

    assert len(result.rebind_folders) == 1
    assert _artist_rows(result) == []


def test_a_musicbrainz_error_folder_gets_no_artist_review_row(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(music_dir / "LP", [{}] * 4, base=_ALBUM_ONLY)
    library.scan_library(engine_settings)
    kit = Kit(
        acoustid=FakeAcoustid(_credited_bodies([_credited(1, _MOBY)])),
        releases=FailingReleases(),
    )

    result = _resolve(engine_settings, kit)

    assert result.errors == 4
    assert "503" in result.error_items[0]["message"]
    assert _artist_rows(result) == []


def test_a_held_file_with_a_title_and_an_artist_review_row_counts_once(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(music_dir / "Pair", [{}, {}], _NAMES[:2], base=_ALBUM_ONLY)
    library.scan_library(engine_settings)
    # Each file sits on a release of its own, so the folder misses the floor and holds both.
    bodies = {
        f"fp-{name}": _body(
            _recording(f"rec-{n}", _TITLES[n - 1], Slot(f"rel-{n}", 1, 1), artists=[_MOBY]),
        )
        for n, name in enumerate(_NAMES[:2], 1)
    }
    kit = Kit(acoustid=FakeAcoustid(bodies), releases=FakeReleases())

    result = _resolve(engine_settings, kit)

    assert result.held_unconverged == 2
    assert sorted(str(row["field"]) for row in result.review_values) == [
        "artist",
        "artist",
        "title",
        "title",
    ]
    assert result.review_files == 2


# --- the status model ----------------------------------------------------------------


def test_revert_of_a_committed_fill_reads_pending(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(music_dir / "LP", _blank_tracknumbers())
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)[_NAMES[1]]
    _resolve(engine_settings, _converging_kit())
    commit = staging.commit_tags(engine_settings)
    assert commit.commit_id is not None
    assert _status(engine_settings, file_id) == "done"

    versioning.revert_commit(engine_settings, commit.commit_id)

    assert _status(engine_settings, file_id) == "pending"


def test_a_fill_over_a_title_written_on_disk_since_the_scan_reopens(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(music_dir / "LP", _blank_tracknumbers())
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)[_NAMES[1]]
    track = music_dir / "LP" / _NAMES[1]
    _edit_on_disk(track, title=["Edited In Picard"])

    _resolve(engine_settings, _converging_kit())
    assert _diffs(engine_settings)[_NAMES[1]].diff == {
        "tracknumber": {"from": [], "to": ["2/4"]},
    }
    staging.commit_tags(engine_settings)
    library.scan_library(engine_settings)

    assert read_tags(track).tags["title"] == ["Edited In Picard"]
    assert _status(engine_settings, file_id) == "pending"


def test_a_release_id_change_reopens_a_done_row_only_once_committed(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(music_dir / "LP", _blank_tracknumbers())
    library.scan_library(engine_settings)
    file_id = _ids(engine_settings)[_NAMES[1]]
    _resolve(engine_settings, _converging_kit())
    staging.commit_tags(engine_settings)

    staging.stage_tags_batch(engine_settings, entries=[(file_id, {"musicbrainz_albumid": [_LP]})])

    # Staging leaves the snapshot the classifier reads unchanged.
    assert _status(engine_settings, file_id) == "done"
    staging.commit_tags(engine_settings)
    assert _status(engine_settings, file_id) == "pending"


def test_a_manual_file_is_skipped_but_still_votes(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(music_dir / "LP", [{}] * 4)
    library.scan_library(engine_settings)
    ids = _ids(engine_settings)
    songs.set_song_status(engine_settings, file_ids=[ids[_NAMES[0]]], status="manual")
    # The fourth file finds nothing, so without the manual file's vote 2 of 3 voters fall
    # below the floor.
    kit = _converging_kit()
    del kit.acoustid.bodies[f"fp-{_NAMES[3]}"]

    result = _resolve(engine_settings, kit)

    assert result.skipped_manual == 1
    assert result.lookup_empty == 1
    assert result.staged_files == 2
    assert _NAMES[0] not in _diffs(engine_settings)
    assert _status(engine_settings, ids[_NAMES[0]]) == "manual"


def test_stats_list_files_and_health_carry_the_song_axis(
    engine_settings: Settings,
    music_dir: Path,
) -> None:
    _make_folder(music_dir / "LP", _blank_tracknumbers())
    library.scan_library(engine_settings)
    pending = library.list_files(engine_settings, song_status="pending")
    _resolve(engine_settings, _converging_kit())

    stats = library.get_library_stats(engine_settings)

    assert [view.filename for view in pending] == list(_NAMES)
    gauge = stats["song"]
    assert gauge == {"pending": 0, "manual": 0, "staged": 3, "done": 1}
    assert sum(gauge.values()) == stats["present"]
    [done] = library.list_files(engine_settings, song_status="done")
    assert done.song_status == "done"
    names = {check.name for check in _run_ok(engine_settings).checks}
    assert {"fpcalc", "acoustid"} <= names
