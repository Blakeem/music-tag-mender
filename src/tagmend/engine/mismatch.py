"""Path coherence: every level of a file's path, compared with the file's own tags.

The comparator reads the ``files``/``file_tags`` snapshot, writes nothing, stages nothing and
never reaches the network. It names the levels of each present file's path (:class:`Layout`),
then runs six comparisons over them through one tolerance ladder, so a difference in formatting
alone never flags. Curated and nested folders are exceptions: listed for a decision and never
counted as flags. :func:`set_mismatch_status` and :func:`reset_mismatch_status` are the module's
only writers, and they write only ``file_mismatch_status`` rows. A fresh row silences its file.
"""

from __future__ import annotations

import functools
import itertools
import re
import unicodedata
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tagmend.engine import axis, axis_status, db, path_keys, schema, store
from tagmend.engine.detector_core import (
    NON_ALBUM_FOLDERS,
    TIER_RANK,
    Tier,
    group_by_folder,
    parse_position,
    validate_tier,
)
from tagmend.engine.parsing import parse_filename_track
from tagmend.engine.path_text import clean_value
from tagmend.engine.text_keys import alnum_ascii_key, loose_key
from tagmend.engine.validation import check_limit, require_choice
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Mapping

    from tagmend.config import Settings

logger = get_logger(__name__)

TOP_FOLDER_ARTIST: Final = "top_folder_artist"
RELEASE_FOLDER_ALBUM: Final = "release_folder_album"
RELEASE_FOLDER_YEAR: Final = "release_folder_year"
DISC_FOLDER_NUMBER: Final = "disc_folder_number"
FILENAME_TRACK: Final = "filename_track"
FILENAME_TITLE: Final = "filename_title"
COMPARISONS: Final = (
    TOP_FOLDER_ARTIST,
    RELEASE_FOLDER_ALBUM,
    RELEASE_FOLDER_YEAR,
    DISC_FOLDER_NUMBER,
    FILENAME_TRACK,
    FILENAME_TITLE,
)

CURATED: Final = "curated"
NESTED: Final = "nested"

# Above this library-wide rate of top-folder differences the top folder likely does not name the
# album artist, so those differences are tiered low. A path-encoding library measures about 1.4%.
RELIABILITY_FLOOR: Final = 0.30

# A name pair at least this similar on the ladder differs by spelling, not by identity.
_NEAR_SPELLING: Final = 0.8

_DETECT_FIELDS: Final = (
    "albumartist",
    "artist",
    "album",
    "date",
    "originaldate",
    "tracknumber",
    "discnumber",
    "title",
    "musicbrainz_albumid",
)

# The dispositions :func:`set_mismatch_status` may write. ``pending`` deletes the row
# (re-queue). This axis has no ``staged`` or ``done``, since an accepted fix needs no row.
_USER_MISMATCH_STATUSES: Final = frozenset({"legit_ignore", "misfiled_deferred", "pending"})

# Folders that collect releases rather than hold one, matched punctuation-insensitively like the
# shared non-album names. The three additions were measured on the live library.
CURATED_FOLDERS: Final = NON_ALBUM_FOLDERS | frozenset({"covers", "unreleased", "other"})
_CURATED_KEYS: Final = frozenset(alnum_ascii_key(name) for name in CURATED_FOLDERS)

# The disc-subfolder pattern measured on the live library. Track-range folders and ``Bonus CD``
# are disc subfolders that name no disc number.
DISC_LEAF: Final = re.compile(
    r"^(?:\[?(?:disc|disk|cd)[\s._-]*\d+[^/]*\]?|\d{1,2}|bonus\s*(?:cd|disc)|cd\s*\d+"
    r"|\[disc\.\d+\]|\d{2}-\d{2}\s.*)$",
    re.IGNORECASE,
)
_DISC_NUMBER: Final = re.compile(r"^\[?(?:disc|disk|cd)[\s._-]*(\d+)|^(\d{1,2})$", re.IGNORECASE)
# A disc subfolder needs a top folder and a release folder above it.
_DISC_DEPTH: Final = 3

_WRAPPER: Final = re.compile(
    r"\s*(?:\[discography\]|-\s*discography\b.*|\((?:19|20)\d\d\s*-\s*(?:19|20)\d\d\)\s*"
    r"(?:flac|mp3)?)\s*$",
    re.IGNORECASE,
)
_YEAR: Final = re.compile(r"(?<!\d)(?:19|20)\d\d(?!\d)")
_TAG_YEAR: Final = re.compile(r"^\s*((?:19|20)\d\d)")
_SCENE_TAG: Final = re.compile(r"\[[^\]]*\]|\{[^}]*\}")
_DISC_EDITION_MARKER: Final = re.compile(
    r"[(\[]?\s*(?:bonus\s+)?\b(?:cd|disc|disk)\s*\d+\s*(?::[^)\]]*)?[)\]]?"
    r"|[(\[]?\s*bonus\s+(?:cd|disc|disk|tracks?)[^)\]]*[)\]]?"
    r"|(?<!\d)\d\s*cd\b"
    r"|\bltd\.?\s*ed(?:ition)?"
    r"|\(?\s*limited\s+edition[^)]*\)?"
    r"|\(?\s*(?:deluxe|special|japanese|tour)\s+edition\)?",
    re.IGNORECASE,
)
_PARENTHETICAL: Final = re.compile(r"\s*[(\[][^)\]]*[)\]]\s*")
_PRIMARY_ARTIST_SPLIT: Final = re.compile(r"\b(?:feat|ft|featuring)\b\.?|[&,]", re.IGNORECASE)

_ARTIST_NUMBER_TITLE: Final = re.compile(r"^.+? - (\d{1,3}) - (.+)$")
_LEADING_NUMBER: Final = re.compile(
    r"^\s*(?:(\d{1,2})[-.](\d{2,3})|(\d{3})|(\d{1,3}))(?=[\s._-]|$)",
)
_LEADING_NUMBER_TEXT: Final = re.compile(r"^\s*(?:\d{1,2}[-.])?\d{1,3}(?:\s*[-._]\s*|\s+)")
_TRAILING_BRACKET: Final = re.compile(r"\[[^\]]*\]$")
# A three-digit leading number from 101 up codes the disc in its hundreds (101 = disc 1, track 1).
_DISC_CODED_MIN: Final = 101
_DISC_CODE_BASE: Final = 100

_WORD: Final = re.compile(r"[^\W_]+")
_INNER_APOSTROPHE: Final = re.compile(r"(?<=[^\W_])['`\u2018\u2019](?=[^\W_])")
_ARTICLES: Final = frozenset({"the", "a", "an"})
_TRAILING_ARTICLE: Final = re.compile(r",\s*(?:the|a|an)\s*$", re.IGNORECASE)
_DIGRAPHS: Final = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue"})
_TOKEN_MAP: Final = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "i": "1",
    "ii": "2",
    "iii": "3",
    "iv": "4",
    "v": "5",
    "vi": "6",
    "vii": "7",
    "viii": "8",
    "ix": "9",
    "x": "10",
    "volume": "vol",
    "and": "",
}
_LADDER_CACHE: Final = 65536


# --- the tolerance ladder ------------------------------------------------------------


@functools.lru_cache(maxsize=_LADDER_CACHE)
def _rungs(text: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return *text*'s word tokens with diacritics kept, stripped, and written as digraphs.

    ``Röyksopp`` is spelled ``Royksopp`` and ``Roeyksopp`` in real folders, so each comparison
    tries all three rungs and agrees when any one agrees.
    """
    prepared = _TRAILING_ARTICLE.sub("", text).replace("&", " and ")
    normalized = unicodedata.normalize("NFKC", prepared).casefold()
    # A split contraction or initialism leaves an orphan ``i`` the roman-numeral map reads as 1.
    words = _join_initials(_WORD.findall(_INNER_APOSTROPHE.sub("", normalized)))
    return (
        _ladder_tokens(words),
        _ladder_tokens([alnum_ascii_key(word) for word in words]),
        _ladder_tokens([alnum_ascii_key(word.translate(_DIGRAPHS)) for word in words]),
    )


def _join_initials(words: list[str]) -> list[str]:
    """Join each run of single letters into one word, so ``I.V.``, ``I V`` and ``IV`` agree."""
    joined: list[str] = []
    for is_initial, run in itertools.groupby(words, key=_is_initial):
        run_words = list(run)
        joined.extend(["".join(run_words)] if is_initial else run_words)
    return joined


def _is_initial(word: str) -> bool:
    """Whether *word* is one letter."""
    return len(word) == 1 and word.isalpha()


def _ladder_tokens(words: list[str]) -> tuple[str, ...]:
    """Map number words, roman numerals and ``Volume``, then drop a leading article."""
    tokens = [token for token in map(_map_token, words) if token]
    if len(tokens) > 1 and tokens[0] in _ARTICLES:
        return tuple(tokens[1:])
    return tuple(tokens)


def _map_token(word: str) -> str:
    """Return *word* as an integer string when numeric, else its ladder alias."""
    # isdecimal, not isdigit: int() rejects the superscripts isdigit accepts.
    if word.isdecimal():
        return str(int(word))
    return _TOKEN_MAP.get(word, word)


@functools.lru_cache(maxsize=_LADDER_CACHE)
def _keys(text: str) -> tuple[str, str, str]:
    """Return one key per rung, or the loose key on every rung for a name with no word in it."""
    kept, stripped, digraph = ("".join(tokens) for tokens in _rungs(text))
    if kept or stripped or digraph:
        return kept, stripped, digraph
    # A name such as ``!!!`` has no word, and must still equal itself.
    fallback = loose_key(text)
    return fallback, fallback, fallback


def _same_name(left: str, right: str) -> bool:
    """Whether *left* and *right* share a non-empty key on one rung."""
    return any(a and a == b for a, b in zip(_keys(left), _keys(right), strict=True))


def _contains(outer: str, inner: str) -> bool:
    """Whether *inner*'s non-empty key sits inside *outer*'s key on one rung."""
    return any(i and i in o for o, i in zip(_keys(outer), _keys(inner), strict=True))


def _overlaps(left: str, right: str) -> bool:
    """Bidirectional containment on the ladder."""
    return _contains(left, right) or _contains(right, left)


def _similarity(left: str, right: str) -> float:
    """Return the best ratio between *left* and *right* over the rungs both fold to something."""
    pairs = zip(_keys(left), _keys(right), strict=True)
    return max((SequenceMatcher(None, a, b).ratio() for a, b in pairs if a and b), default=0.0)


def _shares_word(left: str, right: str) -> bool:
    """Whether *left* and *right* hold one ladder token in common."""
    return any(set(a) & set(b) for a, b in zip(_rungs(left), _rungs(right), strict=True))


def _has_name_text(text: str) -> bool:
    """Whether *text* holds a word that is not a number."""
    return any(not token.isdecimal() for rung in _rungs(text) for token in rung)


def _name_tier(tag_text: str, path_text: str) -> Tier:
    """Tier a name difference: a near spelling is low, no shared word is high."""
    if _similarity(tag_text, path_text) >= _NEAR_SPELLING:
        return Tier.LOW
    if not _shares_word(tag_text, path_text):
        return Tier.HIGH
    return Tier.MEDIUM


def _without(text: str, values: Iterable[str | None]) -> str:
    """Return *text* with every occurrence of each non-empty value removed, ignoring case."""
    result = text
    for value in values:
        if value:
            result = re.sub(re.escape(value), " ", result, flags=re.IGNORECASE)
    return result


def _without_markers(text: str) -> str:
    """Return *text* without disc and edition markers, or whole when no word survives them."""
    stripped = _DISC_EDITION_MARKER.sub(" ", text)
    # An album titled ``Limited Edition`` is its own name, so markers alone stay whole.
    return stripped if any(_rungs(stripped)) else text


def _primary_artist(value: str) -> str:
    """Return the first artist of a ``feat.``/``&``/``,`` credit."""
    for segment in _PRIMARY_ARTIST_SPLIT.split(value):
        candidate = segment.strip()
        if candidate:
            return candidate
    return value.strip()


# --- the layout classifier -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Layout:
    """The named levels of one file's path under ``music_path``.

    ``top_folder`` is ``None`` for a root album, whose one folder is its release folder, and for
    a file at the library root. ``disc_number`` is ``None`` for a disc subfolder that names no
    number (``Bonus CD``, a track range). ``exception`` is ``curated`` or ``nested``.
    """

    top_folder: str | None
    container: bool
    release_folder: str | None
    disc_folder: str | None
    disc_number: int | None
    exception: str | None
    release_years: tuple[str, ...]
    filename: str

    @property
    def disc_numbered(self) -> bool:
        """Whether the file sits in a disc subfolder that names its disc number."""
        return self.disc_number is not None


def layout_of(settings: Settings, folder: str, filename: str) -> Layout:
    """Return the :class:`Layout` of *filename* in *folder*.

    Raises :class:`ValueError` when no music path is configured.
    """
    music_path = _require_music_path(settings)
    return _layout(_path_parts(folder, music_path), filename, _container_keys(settings))


def _require_music_path(settings: Settings) -> Path:
    """Return ``music_path``, or raise :class:`ValueError` when it is not configured."""
    if settings.music_path is None:
        message = "music_path not configured. Run `tagmend config-set music_path <dir>`"
        raise ValueError(message)
    return settings.music_path


def _container_keys(settings: Settings) -> frozenset[str]:
    """Return the fold keys of the configured container folders."""
    return frozenset(alnum_ascii_key(name) for name in settings.container_folders)


def _path_parts(folder: str, music_path: Path) -> tuple[str, ...]:
    """Return *folder*'s parts under *music_path*, empty at the root or outside it."""
    try:
        return Path(folder).relative_to(music_path).parts
    except ValueError:
        return ()


def _layout(parts: tuple[str, ...], filename: str, container_keys: frozenset[str]) -> Layout:
    """Classify *parts* into the top folder, release folder, disc subfolder and exception."""
    if not parts:
        return Layout(
            top_folder=None,
            container=False,
            release_folder=None,
            disc_folder=None,
            disc_number=None,
            exception=None,
            release_years=(),
            filename=filename,
        )
    top = _strip_wrapper(parts[0])
    container = alnum_ascii_key(top) in container_keys
    if len(parts) == 1:
        root_release = None if container else parts[0]
        return Layout(
            top_folder=None,
            container=container,
            release_folder=root_release,
            disc_folder=None,
            disc_number=None,
            exception=None,
            release_years=_years_in(root_release),
            filename=filename,
        )

    below = parts[1:]
    curated = any(_is_curated(part) for part in below)
    disc = None
    if len(parts) >= _DISC_DEPTH and not curated and DISC_LEAF.match(below[-1]):
        disc = below[-1]
    releases = below[:-1] if disc is not None else below
    release = next((part for part in reversed(releases) if not _is_curated(part)), None)
    exception = None
    if curated:
        exception = CURATED
    elif len(releases) > 1:
        exception = NESTED
    return Layout(
        top_folder=top,
        container=container,
        release_folder=release,
        disc_folder=disc,
        disc_number=_disc_number(disc),
        exception=exception,
        release_years=_years_in(release),
        filename=filename,
    )


def _strip_wrapper(name: str) -> str:
    """Return a top folder name without ``[Discography]``-style wrapper decoration."""
    stripped = _WRAPPER.sub("", name).strip()
    return stripped or name


def _is_curated(name: str) -> bool:
    """Whether a folder below the top collects releases (``Singles``, ``Remixes``, ...)."""
    return alnum_ascii_key(name) in _CURATED_KEYS


def _years_in(name: str | None) -> tuple[str, ...]:
    """Return the year tokens in *name*, in order."""
    return () if name is None else tuple(_YEAR.findall(name))


def _disc_number(name: str | None) -> int | None:
    """Return the disc number a disc subfolder names, or ``None``."""
    if name is None:
        return None
    match = _DISC_NUMBER.match(name)
    if match is None:
        return None
    return int(match[1] or match[2])


# --- inputs --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FileInput:
    """One present file: its location and the ordinal-0 value of each tag the comparator reads."""

    file_id: int
    folder: str
    filename: str
    tags: Mapping[str, str]

    def value(self, name: str) -> str | None:
        """Return the tag under the path value rule, ``None`` when blank after it."""
        raw = self.tags.get(name)
        if raw is None:
            return None
        return clean_value(raw) or None

    def shown(self, name: str) -> str:
        """Return the tag as stored, stripped, for a report row."""
        return self.tags.get(name, "").strip()


@dataclass(frozen=True, slots=True)
class _Numbering:
    """A file's position in its folder's (disc, track) order, and whether the folder spans discs."""

    index: int
    multi_disc: bool


@dataclass(frozen=True, slots=True)
class _FilenameNumber:
    """The track number a filename carries, with the disc a disc-coded form adds."""

    disc: int | None
    track: int
    text: str


@dataclass(frozen=True, slots=True)
class _ParsedName:
    """A filename's track number and title text."""

    number: _FilenameNumber | None
    title: str


# --- public result types -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Difference:
    """One comparison that disagrees: the tag's value and the path text it was compared with."""

    comparison: str
    tag_value: str
    path_value: str
    tier: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "comparison": self.comparison,
            "tag_value": self.tag_value,
            "path_value": self.path_value,
        }


@dataclass(frozen=True, slots=True)
class MismatchRow:
    """One file: every difference it carries, its tier, and its exception class.

    ``tier`` is ``None`` only on an exception row with no difference. ``group_folder`` is the
    release folder for a file under a disc subfolder, else the file's folder.
    """

    file_id: int
    folder: str
    filename: str
    tier: str | None
    differences: tuple[Difference, ...]
    exception: str | None
    group_folder: str

    def carries(self, comparison: str) -> bool:
        """Whether one of this file's differences is *comparison*."""
        return any(d.comparison == comparison for d in self.differences)

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "file_id": self.file_id,
            "folder": self.folder,
            "filename": self.filename,
            "tier": self.tier,
            "differences": [d.to_dict() for d in self.differences],
            "exception": self.exception,
        }


@dataclass(frozen=True, slots=True)
class ComparisonSummary:
    """One comparison inside a group: how many files carry it, with one example pair."""

    files: int
    tag: str
    path: str

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {"files": self.files, "tag": self.tag, "path": self.path}


@dataclass(frozen=True, slots=True)
class MismatchGroup:
    """One release folder's flagged and exception files (the ``group=True`` view).

    The keys of ``comparisons`` plus ``exception`` are the names a decision covers. ``file_ids``
    holds the flagged and exception files, ``unflagged_ids`` every other present file.
    """

    folder: str
    file_count: int
    flagged: int
    tier: str | None
    comparisons: dict[str, ComparisonSummary]
    mb_stamped: bool
    exception: str | None
    suppressed: dict[str, int]
    file_ids: list[int]
    unflagged_ids: list[int]

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "folder": self.folder,
            "file_count": self.file_count,
            "flagged": self.flagged,
            "tier": self.tier,
            "comparisons": {name: s.to_dict() for name, s in self.comparisons.items()},
            "mb_stamped": self.mb_stamped,
            "exception": self.exception,
            "suppressed": self.suppressed,
            "file_ids": self.file_ids,
            "unflagged_ids": self.unflagged_ids,
        }


@dataclass(frozen=True, slots=True)
class _GroupMembers:
    """Every present file of one group, under the group folder's display string."""

    folder: str
    file_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class MismatchesReport:
    """Immutable summary of one :func:`detect_mismatches` run, JSON-ready for the MCP tool.

    The counts describe the whole library minus files a fresh disposition silenced, whatever
    the view. ``flagged`` counts files with a difference, and the tier counts sum to it.
    ``exception_rows`` lists every undecided curated or nested file, outside ``flagged``.
    ``group_count`` counts the groups holding a flagged or exception file. ``suppressed`` maps a
    disposition status to the files it silenced. ``container_suppressed`` maps a container top
    folder to the files whose top-folder comparison it skipped. ``groups`` is filled only in the
    grouped view. ``members``, ``mb_stamped_ids`` and ``suppressed_by_group`` build that view and
    are not serialized.
    """

    rows: list[MismatchRow]
    exception_rows: list[MismatchRow]
    total_files: int
    flagged: int
    group_count: int
    by_comparison: dict[str, int]
    high: int
    medium: int
    low: int
    exceptions_undecided: int
    disagreement_rate: float
    path_signal_unreliable: bool
    summary: str
    suppressed: dict[str, int] = field(default_factory=dict)
    container_suppressed: dict[str, int] = field(default_factory=dict)
    groups: list[MismatchGroup] = field(default_factory=list)
    members: dict[str, _GroupMembers] = field(default_factory=dict)
    mb_stamped_ids: frozenset[int] = frozenset()
    suppressed_by_group: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "rows": [row.to_dict() for row in self.rows],
            "exception_rows": [row.to_dict() for row in self.exception_rows],
            "groups": [group.to_dict() for group in self.groups],
            "total_files": self.total_files,
            "flagged": self.flagged,
            "group_count": self.group_count,
            "by_comparison": self.by_comparison,
            "high": self.high,
            "medium": self.medium,
            "low": self.low,
            "exceptions_undecided": self.exceptions_undecided,
            # Round at the serialization edge only. The engine float keeps full precision for
            # the RELIABILITY_FLOOR comparison.
            "disagreement_rate": round(self.disagreement_rate, 4),
            "path_signal_unreliable": self.path_signal_unreliable,
            "suppressed": self.suppressed,
            "container_suppressed": self.container_suppressed,
            "summary": self.summary,
        }


# --- the comparisons -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TopCheck:
    """A file's top-folder comparison before its tier is known."""

    tag_value: str
    path_value: str
    differs: bool
    fallback: bool


@dataclass(frozen=True, slots=True)
class _Judged:
    """One file with its layout, its group and every comparison it went through."""

    file: _FileInput
    layout: Layout
    group_folder: str
    group_key: str
    top: _TopCheck | None
    others: tuple[Difference, ...]


def _check_top(file: _FileInput, layout: Layout) -> _TopCheck | None:
    """Compare the top folder with ``albumartist``, else ``artist``. ``None`` gives no signal."""
    top = layout.top_folder
    if top is None or layout.container:
        return None
    albumartist = file.value("albumartist")
    if albumartist is not None:
        return _TopCheck(
            tag_value=file.shown("albumartist"),
            path_value=top,
            differs=not _same_name(top, albumartist),
            fallback=False,
        )
    artist = file.value("artist")
    if artist is None:
        return None
    agrees = _same_name(top, artist) or _same_name(top, _primary_artist(artist))
    return _TopCheck(
        tag_value=file.shown("artist"),
        path_value=top,
        differs=not agrees,
        fallback=True,
    )


def _compare_album(file: _FileInput, layout: Layout, album_artist: str | None) -> Difference | None:
    """Compare the release folder's album text with ``album`` by bidirectional containment."""
    album = file.value("album")
    release = layout.release_folder
    if album is None or release is None or layout.exception == CURATED:
        return None
    core = _SCENE_TAG.sub(" ", _without(_YEAR.sub(" ", release), (album_artist, layout.top_folder)))
    names_artist = album_artist is not None and _same_name(core, album_artist)
    if layout.top_folder is None and (names_artist or not _has_name_text(core)):
        # A root folder named only after the artist carries no album text.
        return None
    if _contains(release, album):
        return None
    folder_text = release if layout.disc_folder is None else f"{release} {layout.disc_folder}"
    album_text = _without_markers(album)
    core_text = _without_markers(core)
    if _contains(_without_markers(folder_text), album_text):
        return None
    if _overlaps(core_text, album_text):
        return None
    path_text = core_text if _has_name_text(core_text) else release
    return Difference(
        comparison=RELEASE_FOLDER_ALBUM,
        tag_value=file.shown("album"),
        path_value=release,
        tier=_name_tier(album_text, path_text).value,
    )


def _compare_year(file: _FileInput, layout: Layout) -> Difference | None:
    """Require the release folder's year tokens, outside the album text, to hold a tag year."""
    release = layout.release_folder
    if release is None or layout.exception == CURATED:
        return None
    years = {
        match[1]
        for match in (_TAG_YEAR.match(file.shown(name)) for name in ("date", "originaldate"))
        if match is not None
    }
    tokens = set(_YEAR.findall(_without(release, (file.shown("album"), file.value("album")))))
    if not years or not tokens or tokens & years:
        return None
    return Difference(
        comparison=RELEASE_FOLDER_YEAR,
        tag_value=", ".join(sorted(years)),
        path_value=", ".join(sorted(tokens)),
        tier=Tier.HIGH.value,
    )


def _compare_disc(file: _FileInput, layout: Layout) -> Difference | None:
    """Compare a numbered disc subfolder with ``discnumber``."""
    disc = parse_position(file.tags.get("discnumber"))
    if layout.disc_number is None or layout.disc_folder is None or disc is None:
        return None
    if disc == layout.disc_number:
        return None
    return Difference(
        comparison=DISC_FOLDER_NUMBER,
        tag_value=file.shown("discnumber"),
        path_value=layout.disc_folder,
        tier=Tier.HIGH.value,
    )


def _compare_track(
    file: _FileInput,
    parsed: _ParsedName,
    numbering: _Numbering,
) -> Difference | None:
    """Compare the filename's track number with ``tracknumber`` (and a disc-coded disc)."""
    track = parse_position(file.tags.get("tracknumber"))
    number = parsed.number
    if track is None or number is None:
        return None
    disc = parse_position(file.tags.get("discnumber"))
    same_disc = number.disc is None or disc is None or number.disc == disc
    if number.track == track and same_disc:
        return None
    # A folder holding several discs may number its files straight through.
    if numbering.multi_disc and number.track == numbering.index:
        return None
    return Difference(
        comparison=FILENAME_TRACK,
        tag_value=file.shown("tracknumber"),
        path_value=number.text,
        tier=Tier.HIGH.value,
    )


def _compare_title(file: _FileInput, parsed: _ParsedName) -> Difference | None:
    """Compare the filename's title text with ``title`` by bidirectional containment."""
    title = file.value("title")
    core = parsed.title
    if title is None or not _has_name_text(core):
        return None
    if _overlaps(core, title) or _contains(core, _PARENTHETICAL.sub(" ", title)):
        return None
    return Difference(
        comparison=FILENAME_TITLE,
        tag_value=file.shown("title"),
        path_value=core,
        tier=_name_tier(title, core).value,
    )


def _parse_filename(
    filename: str,
    *,
    album_artist: str | None,
    album: str | None,
    prefixes: tuple[str | None, ...],
) -> _ParsedName:
    """Read the track number and title text out of *filename*, the most specific shape first.

    A leading number is read last, because ``311`` and ``36 Crazy Fists`` are artist names.
    """
    name = unicodedata.normalize("NFC", filename)
    stem = Path(name).stem
    anchored = _anchored_template(stem, album_artist, album)
    if anchored is not None:
        return anchored
    template = parse_filename_track(name)
    if template is not None:
        return _ParsedName(number=_track_number(template.track), title=template.title)
    match = _ARTIST_NUMBER_TITLE.match(stem)
    if match is not None:
        return _ParsedName(number=_track_number(match[1]), title=match[2])
    return _ParsedName(number=_leading_number(stem), title=_title_core(stem, prefixes))


def _anchored_template(
    stem: str, album_artist: str | None, album: str | None
) -> _ParsedName | None:
    """Parse ``AlbumArtist - Album - NN - Title`` with the file's own tags as the prefix.

    The generic template splits at the first `` - NN - ``, which an album or artist may hold.
    """
    if album_artist is None or album is None:
        return None
    prefix = re.escape(f"{album_artist} - {album} - ")
    match = re.match(prefix + r"(\d+) - (.+)$", stem, re.IGNORECASE)
    if match is None:
        return None
    return _ParsedName(number=_track_number(match[1]), title=match[2])


def _track_number(text: str) -> _FilenameNumber | None:
    """Return a plain track number, ``None`` for the ``00`` placeholder."""
    track = int(text)
    if track == 0:
        return None
    return _FilenameNumber(disc=None, track=track, text=text)


def _leading_number(stem: str) -> _FilenameNumber | None:
    """Read ``D-NN``, ``DNN`` or ``NN`` at the start of *stem*."""
    disc: int | None = None
    track = 0
    match = _LEADING_NUMBER.match(stem)
    if match is None:
        return None
    if match[1] is not None:
        disc, track = int(match[1]), int(match[2])
    elif match[3] is not None:
        coded = int(match[3])
        if coded >= _DISC_CODED_MIN and coded % _DISC_CODE_BASE:
            disc, track = divmod(coded, _DISC_CODE_BASE)
        else:
            track = coded
    else:
        track = int(match[4])
    if track == 0:
        return None
    return _FilenameNumber(disc=disc, track=track, text=match[0].strip())


def _title_core(stem: str, prefixes: tuple[str | None, ...]) -> str:
    """Return *stem* without its leading number, an artist prefix and a trailing ``[...]``."""
    core = _LEADING_NUMBER_TEXT.sub("", stem, count=1)
    for prefix in prefixes:
        if not prefix:
            continue
        pattern = rf"^\s*{re.escape(prefix)}\s*[-_.]+\s*"
        stripped = re.sub(pattern, "", core, count=1, flags=re.IGNORECASE)
        if stripped != core:
            core = stripped
            break
    return _TRAILING_BRACKET.sub("", core).strip()


def _numbering(files: list[_FileInput]) -> dict[int, _Numbering]:
    """Return each file's place in its folder's (disc, track) order."""
    result: dict[int, _Numbering] = {}
    for folder_files in group_by_folder(files).values():
        discs = {parse_position(f.tags.get("discnumber")) or 1 for f in folder_files}
        ordered = sorted(
            folder_files,
            key=lambda f: (
                parse_position(f.tags.get("discnumber")) or 1,
                parse_position(f.tags.get("tracknumber")) or 0,
                f.file_id,
            ),
        )
        for index, f in enumerate(ordered, 1):
            result[f.file_id] = _Numbering(index=index, multi_disc=len(discs) > 1)
    return result


def _judge(file: _FileInput, layout: Layout, numbering: _Numbering) -> _Judged:
    """Run every comparison that applies to *file*."""
    album_artist = file.value("albumartist") or file.value("artist")
    group_folder = file.folder if layout.disc_folder is None else str(Path(file.folder).parent)
    parsed = _parse_filename(
        file.filename,
        album_artist=album_artist,
        album=file.value("album"),
        prefixes=(file.value("artist"), file.value("albumartist")),
    )
    found = (
        _compare_album(file, layout, album_artist),
        _compare_year(file, layout),
        _compare_disc(file, layout),
        _compare_track(file, parsed, numbering),
        _compare_title(file, parsed),
    )
    return _Judged(
        file=file,
        layout=layout,
        group_folder=group_folder,
        group_key=path_keys.path_key(group_folder),
        top=_check_top(file, layout),
        others=tuple(d for d in found if d is not None),
    )


# --- classification ------------------------------------------------------------------


def _reliability(judged: list[_Judged]) -> tuple[float, bool]:
    """Return the top-folder difference rate over files with a top-folder signal."""
    checked = [item.top for item in judged if item.top is not None]
    if not checked:
        return 0.0, False
    rate = sum(1 for top in checked if top.differs) / len(checked)
    return rate, rate > RELIABILITY_FLOOR


def _group_members(judged: list[_Judged]) -> dict[str, _GroupMembers]:
    """Collect every present file of each group, keyed by the group folder's path key."""
    folders: dict[str, str] = {}
    ids: dict[str, list[int]] = {}
    for item in judged:
        folders.setdefault(item.group_key, item.group_folder)
        ids.setdefault(item.group_key, []).append(item.file.file_id)
    return {key: _GroupMembers(folder=folders[key], file_ids=tuple(ids[key])) for key in folders}


def _albumartists_by_group(judged: list[_Judged]) -> dict[str, set[str]]:
    """Collect each group's distinct ``albumartist`` values."""
    values: dict[str, set[str]] = {}
    for item in judged:
        albumartist = item.file.value("albumartist")
        if albumartist is not None:
            values.setdefault(item.group_key, set()).add(albumartist)
    return values


def _top_tier(
    item: _Judged,
    top: _TopCheck,
    *,
    group_size: int,
    albumartists: int,
    unreliable: bool,
) -> Tier:
    """Tier a top-folder difference: high in a mixed group, medium in a uniform one."""
    guarded = group_size <= 1 or item.layout.exception == CURATED
    if unreliable or top.fallback or guarded:
        return Tier.LOW
    if albumartists > 1:
        return Tier.HIGH
    return Tier.MEDIUM


def _differences(
    item: _Judged,
    *,
    group_size: int,
    albumartists: int,
    unreliable: bool,
) -> tuple[Difference, ...]:
    """Return *item*'s differences, its tiered top-folder difference first."""
    top = item.top
    if top is None or not top.differs:
        return item.others
    tier = _top_tier(
        item,
        top,
        group_size=group_size,
        albumartists=albumartists,
        unreliable=unreliable,
    )
    first = Difference(TOP_FOLDER_ARTIST, top.tag_value, top.path_value, tier.value)
    return (first, *item.others)


def _row(item: _Judged, differences: tuple[Difference, ...]) -> MismatchRow:
    """Build the report row of *item*, its tier the most severe of its differences."""
    tier = min((Tier(d.tier) for d in differences), key=TIER_RANK.__getitem__, default=None)
    return MismatchRow(
        file_id=item.file.file_id,
        folder=item.file.folder,
        filename=item.file.filename,
        tier=None if tier is None else tier.value,
        differences=differences,
        exception=item.layout.exception,
        group_folder=item.group_folder,
    )


def _disposition_blocks(disposition: store.MismatchStatusRow, f: _FileInput) -> bool:
    """Whether *f*'s stored disposition is still fresh (its snapshotted tag is unchanged).

    Delegates to :func:`tagmend.engine.axis.mismatch_decision_blocks`, so this skip path and
    the user-facing :func:`tagmend.engine.store.derived_mismatch_status` share one rule.
    """
    return axis.mismatch_decision_blocks(
        axis.StatusRow(
            status=disposition.status,
            source_primary=disposition.source_field,
            source_secondary=disposition.source_value,
        ),
        axis.Identity(
            primary=_clean(f.tags.get("albumartist")), secondary=_clean(f.tags.get("artist"))
        ),
    )


def _classify(
    files: list[_FileInput],
    music_path: Path,
    *,
    dispositions: dict[int, store.MismatchStatusRow] | None = None,
    container_keys: frozenset[str] = frozenset(),
) -> MismatchesReport:
    """Classify every present file into a full :class:`MismatchesReport` (pure core).

    The reliability guard and the group tiers see every file. A fresh disposition then
    silences its file's row and exception row, and is counted in ``suppressed``.
    """
    numbering = _numbering(files)
    judged = [
        _judge(
            f,
            _layout(_path_parts(f.folder, music_path), f.filename, container_keys),
            numbering[f.file_id],
        )
        for f in files
    ]
    rate, unreliable = _reliability(judged)
    members = _group_members(judged)
    albumartists = _albumartists_by_group(judged)
    stored = dispositions or {}

    rows: list[MismatchRow] = []
    exception_rows: list[MismatchRow] = []
    suppressed: dict[str, int] = {}
    suppressed_by_group: dict[str, dict[str, int]] = {}
    container_suppressed: dict[str, int] = {}
    for item in judged:
        if item.layout.container:
            name = item.layout.top_folder or Path(item.file.folder).name
            container_suppressed[name] = container_suppressed.get(name, 0) + 1
        differences = _differences(
            item,
            group_size=len(members[item.group_key].file_ids),
            albumartists=len(albumartists.get(item.group_key, set())),
            unreliable=unreliable,
        )
        if not differences and item.layout.exception is None:
            continue
        disposition = stored.get(item.file.file_id)
        if disposition is not None and _disposition_blocks(disposition, item.file):
            suppressed[disposition.status] = suppressed.get(disposition.status, 0) + 1
            by_status = suppressed_by_group.setdefault(item.group_key, {})
            by_status[disposition.status] = by_status.get(disposition.status, 0) + 1
            continue
        row = _row(item, differences)
        if differences:
            rows.append(row)
        if item.layout.exception is not None:
            exception_rows.append(row)

    rows.sort(key=lambda r: (min(TIER_RANK[Tier(d.tier)] for d in r.differences), r.file_id))
    exception_rows.sort(key=lambda r: r.file_id)
    mb_stamped_ids = frozenset(f.file_id for f in files if f.value("musicbrainz_albumid"))
    return _assemble_report(
        rows,
        exception_rows=exception_rows,
        total_files=len(files),
        rate=rate,
        unreliable=unreliable,
        suppressed=suppressed,
        suppressed_by_group=suppressed_by_group,
        container_suppressed=container_suppressed,
        members=members,
        mb_stamped_ids=mb_stamped_ids,
    )


def _assemble_report(  # noqa: PLR0913 - cohesive keyword-only report payload
    rows: list[MismatchRow],
    *,
    exception_rows: list[MismatchRow],
    total_files: int,
    rate: float,
    unreliable: bool,
    suppressed: dict[str, int],
    suppressed_by_group: dict[str, dict[str, int]],
    container_suppressed: dict[str, int],
    members: dict[str, _GroupMembers],
    mb_stamped_ids: frozenset[int],
) -> MismatchesReport:
    """Freeze the rows and their library-wide counts into a report."""
    tiers = {tier: sum(1 for r in rows if r.tier == tier) for tier in Tier}
    by_comparison = {
        name: count for name in COMPARISONS if (count := sum(1 for r in rows if r.carries(name)))
    }
    group_count = len({path_keys.path_key(r.group_folder) for r in (*rows, *exception_rows)})
    summary = _summarize(
        flagged=len(rows),
        tiers=tiers,
        exceptions=len(exception_rows),
        group_count=group_count,
        total_files=total_files,
        unreliable=unreliable,
        silenced=sum(suppressed.values()),
        container_files=sum(container_suppressed.values()),
    )
    return MismatchesReport(
        rows=rows,
        exception_rows=exception_rows,
        total_files=total_files,
        flagged=len(rows),
        group_count=group_count,
        by_comparison=by_comparison,
        high=tiers[Tier.HIGH],
        medium=tiers[Tier.MEDIUM],
        low=tiers[Tier.LOW],
        exceptions_undecided=len(exception_rows),
        disagreement_rate=rate,
        path_signal_unreliable=unreliable,
        summary=summary,
        suppressed=suppressed,
        container_suppressed=container_suppressed,
        members=members,
        mb_stamped_ids=mb_stamped_ids,
        suppressed_by_group=suppressed_by_group,
    )


def _summarize(  # noqa: PLR0913 - cohesive keyword-only summary inputs
    *,
    flagged: int,
    tiers: dict[Tier, int],
    exceptions: int,
    group_count: int,
    total_files: int,
    unreliable: bool,
    silenced: int,
    container_files: int,
) -> str:
    """Build a short, plain human summary of the run."""
    note = " (path signal unreliable: top-folder differences tiered low)" if unreliable else ""
    silenced_note = f" {silenced} file(s) silenced by a disposition." if silenced else ""
    container_note = (
        f" {container_files} file(s) under a container folder skip the top-folder comparison."
        if container_files
        else ""
    )
    return (
        f"Flagged {flagged} of {total_files} file(s): {tiers[Tier.HIGH]} high, "
        f"{tiers[Tier.MEDIUM]} medium, {tiers[Tier.LOW]} low{note}. "
        f"{exceptions} exception file(s) undecided. {group_count} group(s) to review."
        f"{silenced_note}{container_note}"
    )


# --- the view ------------------------------------------------------------------------


def _in_folder(row: MismatchRow, folder_key: str) -> bool:
    """Whether *row* belongs to the group, or sits in the folder, keyed *folder_key*."""
    return folder_key in (path_keys.path_key(row.group_folder), path_keys.path_key(row.folder))


def _comparison_summaries(rows: list[MismatchRow]) -> dict[str, ComparisonSummary]:
    """Count each comparison over *rows*, keeping the first file's pair as the example."""
    summaries: dict[str, ComparisonSummary] = {}
    for row in sorted(rows, key=lambda r: r.file_id):
        for difference in row.differences:
            current = summaries.get(difference.comparison)
            summaries[difference.comparison] = (
                ComparisonSummary(files=1, tag=difference.tag_value, path=difference.path_value)
                if current is None
                else replace(current, files=current.files + 1)
            )
    return {name: summaries[name] for name in COMPARISONS if name in summaries}


def _build_groups(
    rows: list[MismatchRow],
    exception_rows: list[MismatchRow],
    report: MismatchesReport,
) -> list[MismatchGroup]:
    """Fold *rows* and *exception_rows* into one group per release folder, folder-sorted."""
    listed = {r.file_id for r in report.rows} | {r.file_id for r in report.exception_rows}
    flagged_by_key: dict[str, list[MismatchRow]] = {}
    for row in rows:
        flagged_by_key.setdefault(path_keys.path_key(row.group_folder), []).append(row)
    excepted_by_key: dict[str, list[MismatchRow]] = {}
    for row in exception_rows:
        excepted_by_key.setdefault(path_keys.path_key(row.group_folder), []).append(row)

    groups: list[MismatchGroup] = []
    for key in flagged_by_key.keys() | excepted_by_key.keys():
        members = report.members[key]
        flagged = flagged_by_key.get(key, [])
        excepted = excepted_by_key.get(key, [])
        tiers = [Tier(r.tier) for r in flagged if r.tier is not None]
        tier = min(tiers, key=TIER_RANK.__getitem__, default=None)
        groups.append(
            MismatchGroup(
                folder=members.folder,
                file_count=len(members.file_ids),
                flagged=len(flagged),
                tier=None if tier is None else tier.value,
                comparisons=_comparison_summaries(flagged),
                mb_stamped=bool(flagged)
                and all(r.file_id in report.mb_stamped_ids for r in flagged),
                exception=next((r.exception for r in (*excepted, *flagged) if r.exception), None),
                suppressed=dict(report.suppressed_by_group.get(key, {})),
                file_ids=sorted({r.file_id for r in (*flagged, *excepted)}),
                unflagged_ids=sorted(set(members.file_ids) - listed),
            ),
        )
    groups.sort(key=lambda g: g.folder)
    return groups


def _narrow(  # noqa: PLR0913 - cohesive keyword-only view parameters
    report: MismatchesReport,
    *,
    tier: str | None,
    comparison: str | None,
    folder_key: str | None,
    limit: int | None,
    group: bool,
) -> MismatchesReport:
    """Return *report* with its rows filtered for display. The counts never change.

    *tier* and *comparison* filter the rows first and drop the exception rows, and the grouped
    view is built from what remains. *folder_key* wins over *group* and returns one group's
    flat rows. *limit* caps the rows, or the groups in the grouped view.
    """
    rows = [
        r
        for r in report.rows
        if (tier is None or r.tier == tier) and (comparison is None or r.carries(comparison))
    ]
    exception_rows = report.exception_rows if tier is None and comparison is None else []
    if folder_key is not None:
        rows = [r for r in rows if _in_folder(r, folder_key)]
        exception_rows = [r for r in exception_rows if _in_folder(r, folder_key)]
    if group and folder_key is None:
        groups = _build_groups(rows, exception_rows, report)
        capped = groups if limit is None else groups[:limit]
        return replace(report, rows=[], exception_rows=[], groups=capped)
    if limit is not None:
        rows = rows[:limit]
        exception_rows = exception_rows[:limit]
    return replace(report, rows=rows, exception_rows=exception_rows, groups=[])


# --- public entry --------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    """Strip *value* and return ``None`` when it is missing or blank."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _load_inputs(connection: sqlite3.Connection) -> list[_FileInput]:
    """Read every present file and the tags the comparator reads, in two queries."""
    tag_values = store.load_tag_values(connection, _DETECT_FIELDS)
    return [
        _FileInput(
            file_id=row.id,
            folder=row.folder,
            filename=row.filename,
            tags=tag_values.get(row.id, {}),
        )
        for row in store.list_files(connection)
        if not row.is_missing
    ]


def detect_mismatches(  # noqa: PLR0913 - cohesive keyword-only view parameters
    settings: Settings,
    *,
    tier: str | None = None,
    limit: int | None = None,
    group: bool = False,
    folder: str | None = None,
    comparison: str | None = None,
) -> MismatchesReport:
    """Report files whose path, at any level, disagrees with their own tags.

    A read-only pass over the snapshot: no tag writes, nothing staged, no network. Each present
    file's path is split into its top folder, release folder, disc subfolder and filename, and
    six comparisons run over them (:data:`COMPARISONS`). A blank tag never flags. Curated and
    nested files are listed in ``exception_rows``, outside ``flagged``. A file with a fresh
    disposition (see :func:`set_mismatch_status`) is silenced and counted in ``suppressed``.

    *tier* and *comparison* keep only the rows of that tier, or carrying that comparison. Each
    row still lists every difference of its file. *group* returns one group per release folder,
    where a file under a disc subfolder joins its release folder. *folder* returns the flat rows
    of the group, or the folder, it names, and wins over *group*. It is compared as a path
    (:func:`tagmend.engine.path_keys.folder_arg_key`). *limit* caps the rows, or the groups. The
    counts always describe the whole library. Raises :class:`ValueError` when no music path is
    configured, for an unknown *tier* or *comparison*, for a negative *limit* and for a *folder*
    outside ``music_path``.
    """
    check_limit(limit)
    music_path = _require_music_path(settings)
    validate_tier(tier)
    require_choice("comparison", comparison, COMPARISONS)
    folder_key = None if folder is None else path_keys.folder_arg_key(settings, folder)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        files = _load_inputs(connection)
        dispositions = store.load_mismatch_statuses(connection)
    finally:
        connection.close()

    report = _classify(
        files,
        music_path,
        dispositions=dispositions,
        container_keys=_container_keys(settings),
    )
    logger.info(
        "detect complete: total=%d flagged=%d groups=%d exceptions=%d rate=%.3f "
        "unreliable=%s silenced=%d",
        report.total_files,
        report.flagged,
        report.group_count,
        report.exceptions_undecided,
        report.disagreement_rate,
        report.path_signal_unreliable,
        sum(report.suppressed.values()),
    )
    return _narrow(
        report,
        tier=tier,
        comparison=comparison,
        folder_key=folder_key,
        limit=limit,
        group=group,
    )


# --- disposition verbs: the module's only writers, of status rows and never tags ------


def _snapshot_source(tags: dict[str, list[str]]) -> tuple[str | None, str | None]:
    """Snapshot the disagreeing tag by detect priority: albumartist-if-present, else artist.

    Returns ``(source_field, source_value)`` using the SAME cleaning the detector applies, so
    the freshness re-check compares like with like. Both ``None`` when the file has neither a
    non-blank ``albumartist`` nor ``artist``.
    """
    albumartist = _first_clean(tags, "albumartist")
    if albumartist is not None:
        return "albumartist", albumartist
    artist = _first_clean(tags, "artist")
    if artist is not None:
        return "artist", artist
    return None, None


def _first_clean(tags: dict[str, list[str]], name: str) -> str | None:
    """Return the cleaned ordinal-0 value of *name*, or ``None`` when absent/blank."""
    values = tags.get(name, [])
    return _clean(values[0]) if values else None


def set_mismatch_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
    status: str,
) -> int:
    """Set a sticky mismatch disposition (or clear it with ``pending``) for every file in scope.

    ``legit_ignore`` silences a false positive. ``misfiled_deferred`` defers a genuinely
    misfiled file. Both snapshot the file's current disagreeing tag (``source_field`` /
    ``source_value``) so a later tag change makes the disposition stale and the file
    re-surfaces on the next detect. ``pending`` deletes any row (re-queue). Scope follows
    :func:`tagmend.engine.axis_status.status_scope`: *file_ids* when given, else every file
    carrying *value* as ``artist`` OR ``albumartist``. Returns the number of files affected.
    Raises :class:`ValueError` for an unknown *status*. Owns its transaction and writes only
    ``file_mismatch_status`` rows.
    """
    require_choice("status", status, _USER_MISMATCH_STATUSES)

    def write(conn: sqlite3.Connection, file_id: int, now: str) -> None:
        if status == "pending":
            store.delete_mismatch_status(conn, file_id)
            return
        source_field, source_value = _snapshot_source(store.get_tags(conn, file_id))
        store.set_mismatch_status(
            conn,
            file_id=file_id,
            status=status,
            source_field=source_field,
            source_value=source_value,
            now=now,
        )

    affected = axis_status.apply_status(
        settings,
        axis.MISMATCH_AXIS,
        file_ids=file_ids,
        value=value,
        write=write,
    )
    logger.info("set mismatch status=%s for %d file(s)", status, affected)
    return affected


def reset_mismatch_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> int:
    """Delete the mismatch disposition for every file in scope (back to ``pending``).

    Same scoping as :func:`set_mismatch_status`. Returns the number of files affected. Owns
    its transaction.
    """
    return axis_status.reset_status(
        settings,
        axis.MISMATCH_AXIS,
        file_ids=file_ids,
        value=value,
    )
