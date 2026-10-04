"""Path coherence: every level of a file's path, compared with the file's own tags.

The comparator reads the ``files``/``file_tags`` snapshot, writes nothing, stages nothing and
never reaches the network. It names the levels of each present file's path (:class:`Layout`),
then runs six comparisons over them through one tolerance ladder, so a difference in formatting
alone never flags. Curated and nested folders are exceptions: listed for a decision and never
counted as flags.

A path decision (``file_mismatch_status``) records what the path planner may do with a file.
``legit_ignore`` keeps its folder, and ``misfiled_deferred`` renders every level from the tags.
No decision keeps a filename. :func:`set_mismatch_status` and :func:`reset_mismatch_status` are the
only writers of those rows. :func:`file_states` reads each file against its row, and
:func:`gate_state`, :func:`check_files` and :func:`planner_keep` are what the path tools call.
"""

from __future__ import annotations

import functools
import itertools
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tagmend.engine import axis, axis_status, clock, db, path_keys, schema, store
from tagmend.engine.detector_core import (
    MAX_DECIMAL_DIGITS,
    NON_ALBUM_FOLDERS,
    TIER_RANK,
    Tier,
    group_by_folder,
    parse_position,
    validate_tier,
)
from tagmend.engine.parsing import parse_filename_track
from tagmend.engine.path_text import clean_value
from tagmend.engine.serialize import FieldDict
from tagmend.engine.text_keys import alnum_ascii_key, loose_key
from tagmend.engine.validation import check_limit, require_choice, require_music_path
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Collection, Iterable, Mapping

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
CLASSES: Final = frozenset({CURATED, NESTED})

# Every name a decision covers, in report order. A keep binds its folder-level names to the
# folder, and every other name binds to the file's path version.
NAMES: Final = (*COMPARISONS, CURATED, NESTED)
FOLDER_NAMES: Final = frozenset(
    {TOP_FOLDER_ARTIST, RELEASE_FOLDER_ALBUM, RELEASE_FOLDER_YEAR, DISC_FOLDER_NUMBER, *CLASSES},
)

# The tags each comparison holds against the path text. A class compares no tag.
_TAG_INPUTS: Final[dict[str, tuple[str, ...]]] = {
    TOP_FOLDER_ARTIST: ("albumartist", "artist"),
    RELEASE_FOLDER_ALBUM: ("album",),
    RELEASE_FOLDER_YEAR: ("date", "originaldate"),
    DISC_FOLDER_NUMBER: ("discnumber",),
    FILENAME_TRACK: ("tracknumber", "discnumber"),
    FILENAME_TITLE: ("title",),
    CURATED: (),
    NESTED: (),
}

PENDING: Final = "pending"
LEGIT_IGNORE: Final = "legit_ignore"
MISFILED_DEFERRED: Final = "misfiled_deferred"
_SET_STATUSES: Final = frozenset({LEGIT_IGNORE, MISFILED_DEFERRED})

# Why a decided file flags again, the most specific reason first.
CHANGED_UNCOVERED: Final = "uncovered"
CHANGED_MOVED: Final = "moved"
CHANGED_TAGS: Final = "tags"
_CHANGE_ORDER: Final = (CHANGED_UNCOVERED, CHANGED_MOVED, CHANGED_TAGS)

FILENAME_NOTE: Final = (
    "No status keeps a filename. The path planner renders every filename from the tags, so "
    "write a wording you want to keep into the tag."
)

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
    if word.isdecimal() and len(word) <= MAX_DECIMAL_DIGITS:
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
    music_path = require_music_path(settings)
    return _layout(_path_parts(folder, music_path), filename, _container_keys(settings))


def _container_keys(settings: Settings) -> frozenset[str]:
    """Return the fold keys of the configured container folders."""
    return frozenset(alnum_ascii_key(name) for name in settings.container_folders)


def _path_parts(folder: str, music_path: Path | None) -> tuple[str, ...]:
    """Return *folder*'s parts under *music_path*, empty at the root, outside it or without it."""
    if music_path is None:
        return ()
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
    """One comparison that disagrees: the tag's value and the path text it was compared with.

    ``silenced`` marks a difference a decision in force silences. The report row sets it, since
    judging a file precedes reading its decision.
    """

    comparison: str
    tag_value: str
    path_value: str
    tier: str
    silenced: bool = False

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "comparison": self.comparison,
            "tag_value": self.tag_value,
            "path_value": self.path_value,
            "silenced": self.silenced,
        }


@dataclass(frozen=True, slots=True)
class MismatchRow:
    """One file: every difference it carries, its tier, and its exception class.

    ``differences`` lists silenced differences too, so a group names every flag a new decision
    must cover. ``tier`` and :meth:`carries` read only the unsilenced ones, and ``tier`` is
    ``None`` on an exception row with none. ``group_folder`` is the release folder for a file
    under a disc subfolder, else the file's folder. ``was`` and ``changed`` are set on a file
    whose decision no longer silences it (:class:`MismatchState`).
    """

    file_id: int
    folder: str
    filename: str
    tier: str | None
    differences: tuple[Difference, ...]
    exception: str | None
    group_folder: str
    was: str | None = None
    changed: str | None = None

    def carries(self, comparison: str) -> bool:
        """Whether *comparison* is one of this file's differences no decision silences."""
        return any(d.comparison == comparison and not d.silenced for d in self.differences)

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "file_id": self.file_id,
            "folder": self.folder,
            "filename": self.filename,
            "tier": self.tier,
            "differences": [d.to_dict() for d in self.differences],
            "exception": self.exception,
            "was": self.was,
            "changed": self.changed,
        }


@dataclass(frozen=True, slots=True)
class ComparisonSummary(FieldDict):
    """One comparison inside a group: how many files carry it, with one example pair."""

    files: int
    tag: str
    path: str


@dataclass(frozen=True, slots=True)
class MismatchGroup(FieldDict):
    """One release folder's flagged and exception files (the ``group=True`` view).

    The keys of ``comparisons`` plus ``exception`` are the names a decision covers. ``file_ids``
    holds the flagged and exception files, ``unflagged_ids`` the present files that flag no
    name. A file whose every flag a decision silences is in neither, and ``suppressed`` counts
    the files per decision in force.
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


@dataclass(frozen=True, slots=True)
class _GroupMembers:
    """Every present file of one group, under the group folder's display string."""

    folder: str
    file_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class MismatchesReport:
    """Immutable summary of one :func:`detect_mismatches` run, JSON-ready for the MCP tool.

    The counts describe the entire library, whatever the view. ``flagged`` counts files with a
    difference no decision silences, and the tier counts sum to it. ``exception_rows`` lists
    every curated or nested file whose class no decision silences, outside ``flagged``.
    ``gate_open`` holds when both are zero. ``group_count`` counts the groups holding a flagged
    or exception file. ``suppressed`` maps a status to the files whose decision is in force, and
    ``stale`` counts the decisions no longer in force. ``container_suppressed`` maps a container
    top folder to the files whose top-folder comparison it skipped. ``groups`` is filled only in
    the grouped view. ``members``, ``mb_stamped_ids``, ``suppressed_by_group`` and
    ``flagging_ids`` build that view and are not serialized.
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
    gate_open: bool
    suppressed: dict[str, int] = field(default_factory=dict)
    stale: int = 0
    container_suppressed: dict[str, int] = field(default_factory=dict)
    groups: list[MismatchGroup] = field(default_factory=list)
    members: dict[str, _GroupMembers] = field(default_factory=dict)
    mb_stamped_ids: frozenset[int] = frozenset()
    suppressed_by_group: dict[str, dict[str, int]] = field(default_factory=dict)
    flagging_ids: frozenset[int] = frozenset()

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
            "gate_open": self.gate_open,
            "suppressed": self.suppressed,
            "stale": self.stale,
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


def top_folder_names_artist(top: str, albumartist: str | None, artist: str | None) -> bool:
    """Whether *top* names ``albumartist``, else ``artist`` or its primary artist, on the ladder.

    Both values are already under the path value rule, ``None`` when blank after it.
    """
    if albumartist is not None:
        return _same_name(top, albumartist)
    if artist is None:
        return False
    return _same_name(top, artist) or _same_name(top, _primary_artist(artist))


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
            differs=not top_folder_names_artist(top, albumartist, None),
            fallback=False,
        )
    artist = file.value("artist")
    if artist is None:
        return None
    return _TopCheck(
        tag_value=file.shown("artist"),
        path_value=top,
        differs=not top_folder_names_artist(top, None, artist),
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
    """Parse ``AlbumArtist - Album - [D-]NN - Title`` with the file's own tags as the prefix.

    The generic template splits at the first `` - NN - ``, which an album or artist may hold.
    The prefix is tried as tagged, then as the path renderer writes it, which deletes the
    characters a path part cannot hold.
    """
    if album_artist is None or album is None:
        return None
    for artist_text, album_text in (
        (album_artist, album),
        (clean_value(album_artist), clean_value(album)),
    ):
        prefix = re.escape(f"{artist_text} - {album_text} - ")
        match = re.match(prefix + r"(?:(\d{1,2})-)?(\d+) - (.+)$", stem, re.IGNORECASE)
        if match is None:
            continue
        number = _track_number(match[2])
        if number is not None and match[1] is not None:
            number = replace(number, disc=int(match[1]), text=f"{match[1]}-{match[2]}")
        return _ParsedName(number=number, title=match[3])
    return None


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


def _row(item: _Judged, differences: tuple[Difference, ...], state: MismatchState) -> MismatchRow:
    """Build the report row of *item*, its tier the most severe of its unsilenced differences."""
    marked = tuple(replace(d, silenced=d.comparison not in state.unsilenced) for d in differences)
    open_tiers = (Tier(d.tier) for d in marked if not d.silenced)
    tier = min(open_tiers, key=TIER_RANK.__getitem__, default=None)
    return MismatchRow(
        file_id=item.file.file_id,
        folder=item.file.folder,
        filename=item.file.filename,
        tier=None if tier is None else tier.value,
        differences=marked,
        exception=item.layout.exception,
        group_folder=item.group_folder,
        was=state.was,
        changed=state.changed,
    )


# --- path decisions: silencing, keep in force and the status reading -----------------


@dataclass(frozen=True, slots=True)
class MismatchState:
    """One present file's reading on the mismatch axis.

    ``names`` holds the comparisons and the class the file flags, and ``unsilenced`` those its
    decision does not silence. ``status`` is ``pending`` while ``unsilenced`` holds a name, else
    the status of a decision in force, else ``pending``. On a decided file that flags again,
    ``was`` is the decision and ``changed`` the first reason of :data:`_CHANGE_ORDER` that
    applies. ``in_force`` says whether ``row`` still binds the file's location.
    """

    status: str
    names: frozenset[str]
    unsilenced: frozenset[str]
    was: str | None
    changed: str | None
    row: store.MismatchStatusRow | None
    in_force: bool


@dataclass(frozen=True, slots=True)
class _Location:
    """Where a file sits now, as the key of its folder and its highest path version."""

    folder_key: str
    path_version: int


def _location(file: _FileInput, versions: Mapping[int, int]) -> _Location:
    """Return *file*'s :class:`_Location`, version 0 for a file that never moved."""
    return _Location(
        folder_key=path_keys.path_key(file.folder),
        path_version=versions.get(file.file_id, 0),
    )


def _keeps_folder(row: store.MismatchStatusRow | None, folder_key: str) -> bool:
    """Whether *row* is a keep bound to the folder keyed *folder_key*."""
    return row is not None and row.status == LEGIT_IGNORE and row.folder_key == folder_key


def _in_force(row: store.MismatchStatusRow, location: _Location) -> bool:
    """Whether *row* still binds, a keep to its folder or a deferral to its path version."""
    if row.status == LEGIT_IGNORE:
        return row.folder_key == location.folder_key
    return row.path_version == location.path_version


def _bound(row: store.MismatchStatusRow, name: str, location: _Location) -> bool:
    """Whether *row* still binds *name*.

    A keep binds a folder-level name to the folder, so a revert to that folder silences it
    again. Every other name binds to the path version, which any committed move advances.
    """
    if row.status == LEGIT_IGNORE and name in FOLDER_NAMES:
        return row.folder_key == location.folder_key
    return row.path_version == location.path_version


def _tags_hold(row: store.MismatchStatusRow, file: _FileInput, name: str) -> bool:
    """Whether each tag of *name* that *row* snapshotted still holds the snapshotted value."""
    # A v23 row set on albumartist never snapshotted artist, which its comparison did not read.
    return all(
        row.tags[tag] == _clean(file.tags.get(tag)) for tag in _TAG_INPUTS[name] if tag in row.tags
    )


def _change(
    row: store.MismatchStatusRow | None,
    file: _FileInput,
    name: str,
    location: _Location,
) -> str | None:
    """Return why *row* does not silence the flagged *name*, or ``None`` when it does."""
    if row is None or name not in row.covers:
        return CHANGED_UNCOVERED
    if not _bound(row, name, location):
        return CHANGED_MOVED
    if not _tags_hold(row, file, name):
        return CHANGED_TAGS
    return None


def _state(
    file: _FileInput,
    names: frozenset[str],
    row: store.MismatchStatusRow | None,
    location: _Location,
) -> MismatchState:
    """Read *file*, which flags *names*, against its stored decision *row*."""
    changes = {
        name: change for name in names if (change := _change(row, file, name, location)) is not None
    }
    unsilenced = frozenset(changes)
    if row is None:
        return MismatchState(PENDING, names, unsilenced, None, None, row, in_force=False)
    in_force = _in_force(row, location)
    if changes:
        reason = next(change for change in _CHANGE_ORDER if change in changes.values())
        return MismatchState(PENDING, names, unsilenced, row.status, reason, row, in_force)
    status = row.status if in_force else PENDING
    return MismatchState(status, names, unsilenced, None, None, row, in_force)


def _names_of(item: _Judged) -> frozenset[str]:
    """Return the comparisons *item* differs on, plus its exception class."""
    names = {difference.comparison for difference in item.others}
    if item.top is not None and item.top.differs:
        names.add(TOP_FOLDER_ARTIST)
    if item.layout.exception is not None:
        names.add(item.layout.exception)
    return frozenset(names)


def _read_state(
    item: _Judged,
    stored: Mapping[int, store.MismatchStatusRow],
    versions: Mapping[int, int],
) -> MismatchState:
    """Read judged *item* against its stored decision, at its current path version."""
    return _state(
        item.file,
        _names_of(item),
        stored.get(item.file.file_id),
        _location(item.file, versions),
    )


def _judge_all(
    files: list[_FileInput],
    music_path: Path | None,
    container_keys: frozenset[str],
) -> list[_Judged]:
    """Lay out and judge every file in *files*, each beside its folder siblings."""
    numbering = _numbering(files)
    return [
        _judge(
            f,
            _layout(_path_parts(f.folder, music_path), f.filename, container_keys),
            numbering[f.file_id],
        )
        for f in files
    ]


@dataclass(slots=True)
class _Tally:
    """The per-file counts :func:`_classify` gathers beside the rows."""

    suppressed: dict[str, int] = field(default_factory=dict)
    suppressed_by_group: dict[str, dict[str, int]] = field(default_factory=dict)
    container_suppressed: dict[str, int] = field(default_factory=dict)
    flagging_ids: set[int] = field(default_factory=set)
    stale: int = 0

    def add(self, item: _Judged, state: MismatchState) -> None:
        """Count *item*'s container skip, its decision in force or stale, and its flags."""
        if item.layout.container:
            name = item.layout.top_folder or Path(item.file.folder).name
            self.container_suppressed[name] = self.container_suppressed.get(name, 0) + 1
        if state.row is not None and not state.in_force:
            self.stale += 1
        if state.status != PENDING:
            self.suppressed[state.status] = self.suppressed.get(state.status, 0) + 1
            by_status = self.suppressed_by_group.setdefault(item.group_key, {})
            by_status[state.status] = by_status.get(state.status, 0) + 1
        if state.names:
            self.flagging_ids.add(item.file.file_id)


def _classify(
    files: list[_FileInput],
    music_path: Path,
    *,
    dispositions: Mapping[int, store.MismatchStatusRow] | None = None,
    path_versions: Mapping[int, int] | None = None,
    container_keys: frozenset[str] = frozenset(),
) -> MismatchesReport:
    """Classify every present file into a full :class:`MismatchesReport` (pure core).

    The reliability guard and the group tiers see every file. Each file is then read against its
    stored decision (:func:`_read_state`). It joins ``rows`` while a comparison is unsilenced and
    ``exception_rows`` while its class is.
    """
    judged = _judge_all(files, music_path, container_keys)
    rate, unreliable = _reliability(judged)
    members = _group_members(judged)
    albumartists = _albumartists_by_group(judged)
    stored = dispositions or {}
    versions = path_versions or {}

    rows: list[MismatchRow] = []
    exception_rows: list[MismatchRow] = []
    tally = _Tally()
    for item in judged:
        state = _read_state(item, stored, versions)
        tally.add(item, state)
        if not state.unsilenced:
            continue
        differences = _differences(
            item,
            group_size=len(members[item.group_key].file_ids),
            albumartists=len(albumartists.get(item.group_key, set())),
            unreliable=unreliable,
        )
        row = _row(item, differences, state)
        if state.unsilenced - CLASSES:
            rows.append(row)
        if state.unsilenced & CLASSES:
            exception_rows.append(row)

    rows.sort(
        key=lambda r: (TIER_RANK[Tier(r.tier)] if r.tier is not None else len(TIER_RANK), r.file_id)
    )
    exception_rows.sort(key=lambda r: r.file_id)
    mb_stamped_ids = frozenset(f.file_id for f in files if f.value("musicbrainz_albumid"))
    return _assemble_report(
        rows,
        exception_rows=exception_rows,
        total_files=len(files),
        rate=rate,
        unreliable=unreliable,
        tally=tally,
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
    tally: _Tally,
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
        decided=sum(tally.suppressed.values()),
        stale=tally.stale,
        container_files=sum(tally.container_suppressed.values()),
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
        gate_open=not rows and not exception_rows,
        suppressed=tally.suppressed,
        stale=tally.stale,
        container_suppressed=tally.container_suppressed,
        members=members,
        mb_stamped_ids=mb_stamped_ids,
        suppressed_by_group=tally.suppressed_by_group,
        flagging_ids=frozenset(tally.flagging_ids),
    )


def _summarize(  # noqa: PLR0913 - cohesive keyword-only summary inputs
    *,
    flagged: int,
    tiers: dict[Tier, int],
    exceptions: int,
    group_count: int,
    total_files: int,
    unreliable: bool,
    decided: int,
    stale: int,
    container_files: int,
) -> str:
    """Build a short, plain human summary of the run."""
    note = " (path signal unreliable: top-folder differences tiered low)" if unreliable else ""
    decided_note = f" {decided} file(s) hold a decision in force." if decided else ""
    stale_note = f" {stale} decision(s) no longer in force." if stale else ""
    container_note = (
        f" {container_files} file(s) under a container folder skip the top-folder comparison."
        if container_files
        else ""
    )
    return (
        f"Flagged {flagged} of {total_files} file(s): {tiers[Tier.HIGH]} high, "
        f"{tiers[Tier.MEDIUM]} medium, {tiers[Tier.LOW]} low{note}. "
        f"{exceptions} exception file(s) undecided. {group_count} group(s) to review."
        f"{decided_note}{stale_note}{container_note}"
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
                unflagged_ids=sorted(set(members.file_ids) - report.flagging_ids),
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


def _load_inputs(
    connection: sqlite3.Connection,
    *,
    near: Collection[int] | None = None,
) -> list[_FileInput]:
    """Read every present file and the tags the comparator reads, in two queries.

    With *near*, keep only the files sharing a folder with one of those ids, since the track
    comparison numbers a file among its folder siblings.
    """
    rows = [row for row in store.list_files(connection) if not row.is_missing]
    if near is not None:
        wanted = set(near)
        folders = {path_keys.path_key(row.folder) for row in rows if row.id in wanted}
        rows = [row for row in rows if path_keys.path_key(row.folder) in folders]
    tag_values = store.load_tag_values(connection, _DETECT_FIELDS)
    return [
        _FileInput(
            file_id=row.id,
            folder=row.folder,
            filename=row.filename,
            tags=tag_values.get(row.id, {}),
        )
        for row in rows
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
    nested files are listed in ``exception_rows``, outside ``flagged``. A path decision
    (:func:`set_mismatch_status`) silences the names it covers while it binds the file
    (:class:`MismatchState`). A file it no longer silences carries ``was`` and ``changed``.
    ``gate_open`` holds when no flagged file and no undecided exception remains.

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
    music_path = require_music_path(settings)
    validate_tier(tier)
    require_choice("comparison", comparison, COMPARISONS)
    folder_key = None if folder is None else path_keys.folder_arg_key(settings, folder)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        files = _load_inputs(connection)
        dispositions = store.load_mismatch_statuses(connection)
        versions = store.path_versions(connection)
    finally:
        connection.close()

    report = _classify(
        files,
        music_path,
        dispositions=dispositions,
        path_versions=versions,
        container_keys=_container_keys(settings),
    )
    logger.info(
        "detect complete: total=%d flagged=%d groups=%d exceptions=%d rate=%.3f "
        "unreliable=%s decided=%d stale=%d gate_open=%s",
        report.total_files,
        report.flagged,
        report.group_count,
        report.exceptions_undecided,
        report.disagreement_rate,
        report.path_signal_unreliable,
        sum(report.suppressed.values()),
        report.stale,
        report.gate_open,
    )
    return _narrow(
        report,
        tier=tier,
        comparison=comparison,
        folder_key=folder_key,
        limit=limit,
        group=group,
    )


def file_states(
    conn: sqlite3.Connection,
    settings: Settings,
    file_ids: Collection[int] | None = None,
) -> dict[int, MismatchState]:
    """Return the reading of every present file, or of the present files among *file_ids*.

    The reading behind :func:`check_files`, ``list_files``, ``get_file`` and the library stats.
    It shares :func:`_read_state` with :func:`detect_mismatches`, so the gate and the commit
    check read a file alike. Without ``music_path`` no folder level can be named, so only the
    filename comparisons run. Reads only, on the caller's connection.
    """
    wanted = None if file_ids is None else set(file_ids)
    stored = store.load_mismatch_statuses(conn)
    versions = store.path_versions(conn)
    judged = _judge_all(
        _load_inputs(conn, near=wanted),
        settings.music_path,
        _container_keys(settings),
    )
    return {
        item.file.file_id: _read_state(item, stored, versions)
        for item in judged
        if wanted is None or item.file.file_id in wanted
    }


def status_counts(conn: sqlite3.Connection, settings: Settings) -> dict[str, int]:
    """Count the present files per mismatch status. Every status is a key, summing to present."""
    counts = dict.fromkeys(sorted(axis.MISMATCH_AXIS.workflow_statuses), 0)
    for state in file_states(conn, settings).values():
        counts[state.status] += 1
    return counts


# --- the contract with the path tools ------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateState(FieldDict):
    """The library-wide stage gate, open when no file flags and no exception is undecided."""

    open: bool
    flagged: int
    exceptions_undecided: int


def gate_state(settings: Settings) -> GateState:
    """Return the gate ``stage_paths`` and ``stage_paths_batch`` read before staging.

    Raises :class:`ValueError` when no music path is configured.
    """
    report = detect_mismatches(settings)
    return GateState(
        open=report.gate_open,
        flagged=report.flagged,
        exceptions_undecided=report.exceptions_undecided,
    )


def check_files(
    conn: sqlite3.Connection,
    settings: Settings,
    file_ids: Collection[int],
) -> list[tuple[int, str]]:
    """Return each ``(file_id, name)`` the given files flag that no decision silences.

    The commit check ``commit_paths`` runs on its forward rows, at the location the ledger
    records. A missing file is not judged. Raises :class:`ValueError` when no music path is
    configured.
    """
    require_music_path(settings)
    states = file_states(conn, settings, file_ids)
    return [
        (file_id, name)
        for file_id in sorted(states)
        for name in NAMES
        if name in states[file_id].unsilenced
    ]


def planner_keep(conn: sqlite3.Connection, file_id: int) -> bool:
    """Whether *file_id*'s folder stays, since it holds a keep bound to its current folder.

    Tags play no part, so a later tag edit never releases a kept folder. A name that flags
    again closes the gate instead.
    """
    row = store.get_file_by_id(conn, file_id)
    if row is None:
        return False
    return _keeps_folder(store.get_mismatch_status(conn, file_id), path_keys.path_key(row.folder))


# --- the status tools: the only writers of file_mismatch_status ----------------------


@dataclass(frozen=True, slots=True)
class DecidedFile(FieldDict):
    """What the path planner does with one decided file's folder and filename."""

    file_id: int
    folder: str
    filename: str


@dataclass(frozen=True, slots=True)
class MismatchStatusResult:
    """One :func:`set_mismatch_status` call: the rows it wrote and what the planner will do.

    ``covers`` counts the written files per covered name. ``skipped_unflagged`` counts the
    carriers of a *value* scope whose top folder agrees with their tags.
    """

    status: str
    affected: int
    skipped_unflagged: int
    covers: dict[str, int]
    files: list[DecidedFile]

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable form for the MCP tool."""
        return {
            "status": self.status,
            "affected": self.affected,
            "skipped_unflagged": self.skipped_unflagged,
            "covers": self.covers,
            "files": [decided.to_dict() for decided in self.files],
            "note": FILENAME_NOTE,
        }


def _require_covers(covers: Collection[str]) -> frozenset[str]:
    """Return *covers* as a set, or raise :class:`ValueError` naming each unknown name."""
    unknown = sorted(set(covers) - set(NAMES))
    if unknown:
        message = f"unknown covers name(s): {unknown} (expected some of {', '.join(NAMES)})"
        raise ValueError(message)
    return frozenset(covers)


def _require_value_decision(status: str, covered: frozenset[str]) -> None:
    """Refuse a *value* scope for anything but a top-folder deferral."""
    if status == MISFILED_DEFERRED and covered == {TOP_FOLDER_ARTIST}:
        return
    message = (
        'a value scope takes only status="misfiled_deferred" with '
        'covers=["top_folder_artist"]. Pass file_ids for any other decision'
    )
    raise ValueError(message)


def _refuse_missing(conn: sqlite3.Connection, file_ids: list[int]) -> None:
    """Raise :class:`ValueError` naming each id of *file_ids* the last scan found missing."""
    missing = store.missing_file_ids(conn)
    absent = [file_id for file_id in file_ids if file_id in missing]
    if absent:
        message = f"file_id(s) {absent} are missing from disk. Rescan, or leave them out"
        raise ValueError(message)


def _refuse_staged_paths(conn: sqlite3.Connection, file_ids: list[int]) -> None:
    """Raise :class:`ValueError` naming each id of *file_ids* with a staged path change."""
    staged = store.staged_path_file_ids(conn, file_ids)
    if staged:
        message = f"file_id(s) {staged} hold a staged path change. Run unstage_paths on them first"
        raise ValueError(message)


def _refuse_spanning_groups(targets: list[_Judged]) -> None:
    """Refuse a scope spanning more than one release-folder group, naming the groups."""
    folders = {item.group_key: item.group_folder for item in targets}
    if len(folders) > 1:
        message = (
            f"one call decides one release-folder group, and these files span "
            f"{len(folders)}: {sorted(folders.values())}. Call once per group"
        )
        raise ValueError(message)


def _refuse_uncovered(
    targets: list[_Judged],
    covered: frozenset[str],
    names: Mapping[int, frozenset[str]],
) -> None:
    """Refuse a file flagging a name outside *covered*, naming each ``(file_id, name)``."""
    pairs = [
        (item.file.file_id, name)
        for item in targets
        for name in NAMES
        if name in names[item.file.file_id] - covered
    ]
    if pairs:
        message = (
            f"file(s) flag names outside covers: {pairs}. Read "
            f"detect_mismatches(folder={targets[0].group_folder!r}) and pass every name it "
            f"lists, or fix the tags first"
        )
        raise ValueError(message)


def _refuse_unflagged_deferral(
    targets: list[_Judged],
    names: Mapping[int, frozenset[str]],
) -> None:
    """Refuse ``misfiled_deferred`` on a file that flags no comparison and holds no class."""
    unflagged = [item.file.file_id for item in targets if not names[item.file.file_id]]
    if unflagged:
        message = (
            f"misfiled_deferred needs a flag or a class, and file_id(s) {unflagged} have "
            f"neither. Omit unflagged_ids from a misfiled_deferred call"
        )
        raise ValueError(message)


def _refuse_split_keep(
    targets: list[_Judged],
    judged: list[_Judged],
    stored: Mapping[int, store.MismatchStatusRow],
) -> None:
    """Refuse a keep that would leave a present member of its group without a keep in force."""
    group_key = targets[0].group_key
    target_ids = {item.file.file_id for item in targets}
    left = [
        item.file.file_id
        for item in judged
        if item.group_key == group_key
        and item.file.file_id not in target_ids
        and not _keeps_folder(stored.get(item.file.file_id), path_keys.path_key(item.file.folder))
    ]
    if left:
        message = (
            f"legit_ignore keeps an entire group, and member file_id(s) {left} would hold no "
            f"keep. Pass the group's file_ids and unflagged_ids together"
        )
        raise ValueError(message)


def _refuse_unsafe(  # noqa: PLR0913 - one guard pass over the call's entire context
    status: str,
    covered: frozenset[str],
    targets: list[_Judged],
    judged: list[_Judged],
    names: Mapping[int, frozenset[str]],
    stored: Mapping[int, store.MismatchStatusRow],
) -> None:
    """Run every guard that refuses the entire call before anything is written."""
    if not targets:
        return
    _refuse_spanning_groups(targets)
    _refuse_uncovered(targets, covered, names)
    if status == MISFILED_DEFERRED:
        _refuse_unflagged_deferral(targets, names)
    else:
        _refuse_split_keep(targets, judged, stored)


def _decision_row(
    status: str,
    item: _Judged,
    names: frozenset[str],
    versions: Mapping[int, int],
) -> store.MismatchStatusRow:
    """Snapshot *item*'s flagged *names*, their tags, its path version and a keep's folder."""
    covers = tuple(name for name in NAMES if name in names)
    location = _location(item.file, versions)
    return store.MismatchStatusRow(
        status=status,
        covers=covers,
        tags={tag: _clean(item.file.tags.get(tag)) for name in covers for tag in _TAG_INPUTS[name]},
        path_version=location.path_version,
        folder_key=location.folder_key if status == LEGIT_IGNORE else None,
    )


def set_mismatch_status(
    settings: Settings,
    *,
    status: str,
    covers: Collection[str],
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> MismatchStatusResult:
    """Record one path decision for every file in scope, or refuse the entire call.

    ``legit_ignore`` keeps each file's folder, so the path planner renders only its filename.
    ``misfiled_deferred`` says the tags are right, so the planner renders every level. No status
    keeps a filename. Covering a filename difference means the tag is right and the filename
    will follow it. To keep a filename's wording, write it into the tag.

    *covers* names the comparisons and classes decided, read from the group row. Each written
    row covers the names its file flags now, and snapshots their tags, the file's path version
    and, on a keep, its folder key (:class:`tagmend.engine.store.MismatchStatusRow`). A keep may
    cover nothing, a durable keep of an unflagged file's folder.

    Scope is *file_ids* when given, else the present files carrying *value* as ``artist`` or
    ``albumartist`` whose top folder differs (other carriers count as ``skipped_unflagged``). A
    *value* scope takes only ``misfiled_deferred`` with ``covers=["top_folder_artist"]``.

    Raises :class:`ValueError`, writing nothing, for an unknown *status* or name, a missing music
    path, an unknown or missing id, a file with a staged path change, a scope spanning two
    release-folder groups, a file flagging a name outside *covers*, a deferral of a file that
    flags nothing, and a keep that would leave a present group member without a keep in force.
    Owns its transaction.
    """
    require_choice("status", status, _SET_STATUSES)
    covered = _require_covers(covers)
    by_value = file_ids is None and value is not None
    if by_value:
        _require_value_decision(status, covered)
    music_path = require_music_path(settings)
    rows: dict[int, store.MismatchStatusRow] = {}
    candidates: list[_Judged] = []

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        scoped = axis_status.status_scope(
            connection,
            axis.MISMATCH_AXIS,
            file_ids=file_ids,
            value=value,
        )
        if not by_value:
            _refuse_missing(connection, scoped)
        judged = _judge_all(_load_inputs(connection), music_path, _container_keys(settings))
        names = {item.file.file_id: _names_of(item) for item in judged}
        by_id = {item.file.file_id: item for item in judged}
        candidates = [by_id[file_id] for file_id in scoped if file_id in by_id]
        targets = [
            item
            for item in candidates
            if not by_value or TOP_FOLDER_ARTIST in names[item.file.file_id]
        ]
        _refuse_staged_paths(connection, [item.file.file_id for item in targets])
        versions = store.path_versions(connection)
        _refuse_unsafe(
            status,
            covered,
            targets,
            judged,
            names,
            store.load_mismatch_statuses(connection),
        )
        rows = {
            item.file.file_id: _decision_row(status, item, names[item.file.file_id], versions)
            for item in targets
        }
        now = clock.utc_now()
        for file_id, row in rows.items():
            store.set_mismatch_status(connection, file_id=file_id, row=row, now=now)
        connection.commit()
    finally:
        connection.close()

    result = MismatchStatusResult(
        status=status,
        affected=len(rows),
        skipped_unflagged=len(candidates) - len(rows),
        covers=dict(Counter(name for row in rows.values() for name in row.covers)),
        files=[
            DecidedFile(
                file_id=file_id,
                folder="kept" if status == LEGIT_IGNORE else "rendered",
                filename="rendered",
            )
            for file_id in rows
        ],
    )
    logger.info("set mismatch status=%s for %d file(s)", status, result.affected)
    return result


def reset_mismatch_status(
    settings: Settings,
    *,
    file_ids: list[int] | None = None,
    value: str | None = None,
) -> int:
    """Delete the path decision of every file in scope, so each reads its flags again.

    Scope is *file_ids* when given, else every file carrying *value* as ``artist`` or
    ``albumartist``. A delete authorises nothing, so no covers or group rule applies. Raises
    :class:`ValueError`, deleting nothing, for an unknown or missing id and for a file with a
    staged path change. Returns the number of files in scope. Owns its transaction.
    """
    scoped: list[int] = []
    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        scoped = axis_status.status_scope(
            connection,
            axis.MISMATCH_AXIS,
            file_ids=file_ids,
            value=value,
        )
        if file_ids is not None:
            _refuse_missing(connection, scoped)
        _refuse_staged_paths(connection, scoped)
        for file_id in scoped:
            store.delete_mismatch_status(connection, file_id)
        connection.commit()
    finally:
        connection.close()
    logger.info("reset mismatch status for %d file(s)", len(scoped))
    return len(scoped)
