"""detect_path_deviations: each file's current path against the path its tags render.

The comparison behind the finding noun ``deviation``: the current path against the canonical
path the naming pattern generates from the file's tags. Read-only: nothing is staged, no setting
is saved and the network is never reached. The planner (:func:`tagmend.engine.paths.plan_library`)
renders every present file and applies every hold ``stage_paths`` applies, so a file held here
is held there for the same reason.

The header is the discovery aid for choosing a pattern: the counts per plan status and per hold
reason, the fit of each pattern component against the current path, the most common current
shapes of the top folder, the leaf folder and the filename, the top folders that look like
containers, the mismatch gate and the volume check. The body lists one group per source folder
holding a file that deviates, or one row per such file.
"""

from __future__ import annotations

import dataclasses
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tagmend.engine import db, mismatch, naming, path_keys, paths, schema, store
from tagmend.engine.detector_core import parse_position
from tagmend.engine.path_text import clean_value
from tagmend.engine.serialize import FieldDict
from tagmend.engine.validation import check_limit, require_music_path
from tagmend.log import get_logger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping, Sequence

    from tagmend.config import Settings

logger = get_logger(__name__)

# The current path levels the shape census reads.
LEVEL_TOP: Final = "top_folder"
LEVEL_LEAF: Final = "leaf_folder"
LEVEL_FILENAME: Final = "filename"
CENSUS_LEVELS: Final = (LEVEL_TOP, LEVEL_LEAF, LEVEL_FILENAME)
_CENSUS_SIZE: Final = 8
_CANDIDATE_CAP: Final = 20

# The fields a current name is abstracted over, longest value first, ties in this order.
_SHAPE_TEXT_FIELDS: Final = ("albumartist", "artist", "album", "title")
_CENSUS_TAGS: Final = (*_SHAPE_TEXT_FIELDS, "date", "originaldate", "tracknumber", "discnumber")
# Private-use placeholders keep a replaced field out of reach of the next field's match.
_PLACEHOLDER_BASE: Final = 0xE000
_DIGITS: Final = re.compile(r"[0-9]+")

_KIND_ORDER: Final = (paths.KIND_MOVE, paths.KIND_RENAME, paths.KIND_CASE)


@dataclass(frozen=True, slots=True)
class LevelFit(FieldDict):
    """How one pattern component fits the current path: same text, same key, or different."""

    component: str
    exact: int
    case: int
    differs: int


@dataclass(frozen=True, slots=True)
class ContainerCandidate(FieldDict):
    """A top folder most of whose files name another album artist.

    It is either a container (``Soundtracks``) or a tag spelling ``detect_mismatches`` owns.
    ``top_album_artist`` tells the two apart. ``listed`` marks a configured container.
    """

    folder: str
    files: int
    album_artists: int
    top_album_artist: str | None
    listed: bool


@dataclass(frozen=True, slots=True)
class DeviationGroup(FieldDict):
    """One source folder holding a file that deviates. Paths are relative to ``music_path``.

    ``kind`` is the largest change among its files: ``move``, then ``rename``, then ``case``.
    ``held`` counts its held files per reason. ``example`` is one deviating file's pair.
    """

    folder: str
    files: int
    destinations: tuple[str, ...]
    kind: str | None
    held: dict[str, int]
    example: dict[str, str | None] | None


@dataclass(frozen=True, slots=True)
class DeviationsReport(FieldDict):
    """The entire ``detect_path_deviations`` answer. Counts always describe the entire library."""

    pattern: str
    persisted: bool
    default_pattern: str
    container_folders: tuple[str, ...]
    total_files: int
    counts: dict[str, int]
    held: dict[str, int]
    fit: tuple[LevelFit, ...]
    shapes: dict[str, list[dict[str, object]]]
    container_candidates: tuple[ContainerCandidate, ...]
    gate: mismatch.GateState
    volume_refusal: str | None
    group_count: int
    groups: tuple[DeviationGroup, ...]
    rows: tuple[paths.FilePlan, ...]


# --- the header ----------------------------------------------------------------------


def _fit(pattern: naming.Pattern, plans: Sequence[paths.FilePlan]) -> tuple[LevelFit, ...]:
    """Compare each rendered part with the current part at the same depth from the filename."""
    tallies = [[0, 0, 0] for _ in pattern.components]
    for plan in plans:
        current = Path(plan.from_path).parts
        offset = len(current) - len(plan.rendered)
        for index, (part, level) in enumerate(zip(plan.rendered, plan.levels, strict=True)):
            if level is None:
                continue
            here = current[offset + index] if offset + index >= 0 else None
            if here == part:
                tallies[level][0] += 1
            elif here is not None and path_keys.path_key(here) == path_keys.path_key(part):
                tallies[level][1] += 1
            else:
                tallies[level][2] += 1
    return tuple(
        LevelFit(component=text, exact=exact, case=case, differs=differs)
        for text, (exact, case, differs) in zip(pattern.component_texts, tallies, strict=True)
    )


def _shape_rules(values: Mapping[str, str]) -> list[tuple[str, re.Pattern[str]]]:
    """Return each ``(placeholder, regex)`` a name's shape replaces, in replacement order."""
    rules: list[tuple[str, re.Pattern[str]]] = []
    texts = sorted(
        ((field, clean_value(values.get(field, ""))) for field in _SHAPE_TEXT_FIELDS),
        key=lambda pair: -len(pair[1]),
    )
    rules.extend(
        (f"{{{field}}}", re.compile(rf"(?<![^\W_]){re.escape(value)}(?![^\W_])", re.IGNORECASE))
        for field, value in texts
        if value
    )
    year = naming.year_of(values)
    if year:
        rules.append((f"{{{naming.YEAR}}}", re.compile(rf"(?<![0-9]){year}(?![0-9])")))
    disc = parse_position(values.get("discnumber"))
    track = parse_position(values.get("tracknumber"))
    if disc is not None and track is not None:
        pair = re.compile(rf"(?<![0-9])0*{disc}[-.]0*{track}(?![0-9])")
        rules.append(("{discnumber}-{tracknumber}", pair))
    if track is not None:
        rules.append(("{tracknumber}", re.compile(rf"(?<![0-9])0*{track}(?![0-9])")))
    return rules


def shape_of(name: str, values: Mapping[str, str]) -> str:
    """Return *name* with the file's own tag values written as ``{field}`` placeholders.

    Text values are replaced only where they stand as separate words, the longest first. The
    year, a ``D-NN`` disc and track pair and the track number follow. Any digits left become
    ``#``.
    """
    rules = _shape_rules(values)
    shaped = name
    for index, (_, regex) in enumerate(rules):
        shaped = regex.sub(chr(_PLACEHOLDER_BASE + index), shaped, count=1)
    shaped = _DIGITS.sub("#", shaped)
    for index, (placeholder, _) in enumerate(rules):
        shaped = shaped.replace(chr(_PLACEHOLDER_BASE + index), placeholder)
    return shaped


def _census(
    plans: Sequence[paths.FilePlan],
    tag_values: Mapping[int, Mapping[str, str]],
) -> dict[str, list[dict[str, object]]]:
    """Count the most common current shapes of the top folder, the leaf folder and the filename."""
    counters: dict[str, Counter[str]] = {level: Counter() for level in CENSUS_LEVELS}
    for plan in plans:
        values = tag_values.get(plan.file_id, {})
        parts = Path(plan.from_path).parts
        counters[LEVEL_FILENAME][shape_of(Path(parts[-1]).stem, values)] += 1
        if len(parts) > 1:
            counters[LEVEL_TOP][shape_of(parts[0], values)] += 1
            counters[LEVEL_LEAF][shape_of(parts[-2], values)] += 1
    return {
        level: [
            {"shape": shape, "files": files}
            for shape, files in counters[level].most_common(_CENSUS_SIZE)
        ]
        for level in CENSUS_LEVELS
    }


def _candidates(
    settings: Settings,
    rows: Sequence[store.FileRow],
    tag_values: Mapping[int, Mapping[str, str]],
) -> tuple[ContainerCandidate, ...]:
    """Return the top folders whose name matches under half of their files' album artists."""
    by_top: dict[str, list[tuple[str | None, bool]]] = {}
    container_of: dict[str, bool] = {}
    for row in rows:
        layout = mismatch.layout_of(settings, row.folder, row.filename)
        top = layout.top_folder
        if top is None:
            continue
        values = tag_values.get(row.id, {})
        albumartist = clean_value(values.get("albumartist", "")) or None
        artist = clean_value(values.get("artist", "")) or None
        container_of[top] = layout.container
        names_top = mismatch.top_folder_names_artist(top, albumartist, artist)
        by_top.setdefault(top, []).append((albumartist or artist, names_top))
    candidates: list[ContainerCandidate] = []
    for top, files in by_top.items():
        matching = sum(1 for _, names_top in files if names_top)
        if matching * 2 >= len(files):
            continue
        named = Counter(name for name, _ in files if name is not None)
        candidates.append(
            ContainerCandidate(
                folder=top,
                files=len(files),
                album_artists=len(named),
                top_album_artist=named.most_common(1)[0][0] if named else None,
                listed=container_of[top],
            ),
        )
    candidates.sort(key=lambda candidate: (-candidate.files, candidate.folder))
    return tuple(candidates[:_CANDIDATE_CAP])


# --- the body ------------------------------------------------------------------------


def _deviates(plan: paths.FilePlan) -> bool:
    """Whether the plan is anything but ``at_target``.

    A move, a case difference, a hold or a kept staged move each count.
    """
    return plan.status != paths.STATUS_AT_TARGET


def _group(folder: str, members: list[paths.FilePlan]) -> DeviationGroup:
    """Summarize one source folder's files."""
    kinds = {plan.kind for plan in members}
    example = next((plan for plan in members if _deviates(plan)), None)
    return DeviationGroup(
        folder=folder,
        files=len(members),
        destinations=tuple(
            sorted({str(Path(plan.to_path).parent) for plan in members if plan.to_path}),
        ),
        kind=next((kind for kind in _KIND_ORDER if kind in kinds), None),
        held=paths.held_counts(members),
        example=(
            None
            if example is None
            else {"from_path": example.from_path, "to_path": example.to_path}
        ),
    )


def _body(
    plans: list[paths.FilePlan],
    *,
    folder_key: str | None,
    group: bool,
    limit: int | None,
) -> tuple[int, tuple[DeviationGroup, ...], tuple[paths.FilePlan, ...]]:
    """Return the group count, the groups and the rows the view asks for."""
    ordered = sorted(plans, key=lambda plan: plan.from_path)
    group_count = len({plan.source_folder for plan in plans if _deviates(plan)})
    if folder_key is not None:
        rows = [plan for plan in ordered if plan.folder_key == folder_key]
        return group_count, (), tuple(rows[:limit])
    if not group:
        rows = [plan for plan in ordered if _deviates(plan)]
        return group_count, (), tuple(rows[:limit])
    by_folder: dict[str, list[paths.FilePlan]] = {}
    for plan in ordered:
        by_folder.setdefault(plan.source_folder, []).append(plan)
    groups = [
        _group(folder, members)
        for folder, members in by_folder.items()
        if any(_deviates(plan) for plan in members)
    ]
    return group_count, tuple(groups[:limit]), ()


def _effective_settings(
    settings: Settings,
    pattern: str | None,
    container_folders: Sequence[str] | None,
) -> tuple[Settings, naming.Pattern]:
    """Return *settings* with the previewed pattern and container folders, and the pattern."""
    parsed = (
        naming.effective_pattern(settings)
        if pattern is None
        else naming.parse_pattern(pattern.strip())
    )
    folders = (
        settings.container_folders
        if container_folders is None
        else naming.validate_container_folders(container_folders)
    )
    previewed = dataclasses.replace(settings, naming_pattern=parsed.text, container_folders=folders)
    return previewed, parsed


def _load(
    connection: sqlite3.Connection,
    settings: Settings,
    pattern: naming.Pattern,
) -> tuple[list[paths.FilePlan], list[store.FileRow], dict[int, dict[str, str]]]:
    """Plan the library and read what the census and the container candidates need."""
    plans = paths.plan_library(connection, settings, pattern=pattern)
    planned = {plan.file_id for plan in plans}
    rows = [row for row in store.list_files(connection) if row.id in planned]
    return plans, rows, store.load_tag_values(connection, _CENSUS_TAGS)


def detect_path_deviations(  # noqa: PLR0913 - cohesive keyword-only view parameters
    settings: Settings,
    *,
    pattern: str | None = None,
    container_folders: Sequence[str] | None = None,
    folder: str | None = None,
    group: bool = True,
    limit: int | None = 50,
) -> DeviationsReport:
    """Report every file whose plan is not ``at_target`` under the path its tags render. Read-only.

    The persisted naming settings render each present file, unless *pattern* or
    *container_folders* preview a candidate, which is never saved. Save the winner with
    ``set_naming_pattern``. Set ``container_folders`` before the first ``set_mismatch_status``
    decision, since ``detect_mismatches`` and the gate read the same list. A file is
    ``at_target``, ``case_only`` (a folder differs only in case, which nothing stages),
    ``will_move``, ``held`` with every reason ``stage_paths`` would hold it by, or
    ``kept_staged`` (a manual or revert move is staged). Allowed while the gate is closed.

    *group* returns one group per source folder holding a deviating file. Otherwise the rows
    list each deviating file. *folder* lists every file of that exact folder, compared as a
    path, and wins over *group*. *limit* caps the groups or rows. Raises :class:`ValueError`
    for an invalid pattern or folder name, a negative *limit*, a *folder* outside
    ``music_path`` and a missing ``music_path``.
    """
    check_limit(limit)
    music_path = require_music_path(settings)
    previewed, parsed = _effective_settings(settings, pattern, container_folders)
    folder_key = None if folder is None else path_keys.folder_arg_key(settings, folder)

    connection = db.connect(settings.db_path)
    try:
        schema.apply_schema(connection)
        plans, rows, tag_values = _load(connection, previewed, parsed)
    finally:
        connection.close()
    gate = mismatch.gate_state(previewed)

    group_count, groups, view_rows = _body(plans, folder_key=folder_key, group=group, limit=limit)
    counts = dict.fromkeys(paths.PLAN_STATUSES, 0)
    for plan in plans:
        counts[plan.status] += 1
    report = DeviationsReport(
        pattern=parsed.text,
        persisted=parsed.text == naming.pattern_text(settings)
        and previewed.container_folders == settings.container_folders,
        default_pattern=naming.DEFAULT_PATTERN,
        container_folders=previewed.container_folders,
        total_files=len(plans),
        counts=counts,
        held=paths.held_counts(plans),
        fit=_fit(parsed, plans),
        shapes=_census(plans, tag_values),
        container_candidates=_candidates(previewed, rows, tag_values),
        gate=gate,
        volume_refusal=paths.volume_refusal(music_path),
        group_count=group_count,
        groups=groups,
        rows=view_rows,
    )
    logger.info(
        "path deviations: total=%d at_target=%d will_move=%d held=%d groups=%d",
        report.total_files,
        counts[paths.STATUS_AT_TARGET],
        counts[paths.STATUS_WILL_MOVE],
        counts[paths.STATUS_HELD],
        group_count,
    )
    return report
