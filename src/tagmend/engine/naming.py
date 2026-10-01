"""The naming pattern: its grammar, the album grouper and the renderer.

A pattern is a ``/``-separated list of components, the last one the filename::

    pattern   = component ("/" component)*
    component = (literal | field | group)+
    field     = "{" name ("|" name)* (":" width)? "}"
    group     = "[" (literal | field)+ "]"

A field renders its first non-empty name, and ``:02`` zero-pads a position such as ``3/12``. A
group renders only when every field inside it is non-empty. A field outside every group is
required: when it renders empty the file is held ``missing_<name>``, naming its last name. A
component built only from groups is dropped when it renders empty. The file's own extension is
appended to the filename and is never written in the pattern.

A name is a managed tag or one of three computed names. ``year`` is the first four digits of
``originaldate``, else of ``date``, with ``0000`` empty. ``disc`` is the file's disc number when
its album is multi-disc, else empty. ``container`` is the configured container folder the file
sits under, else empty.

Every value passes :func:`tagmend.engine.path_text.clean_value`, the rule the mismatch comparator
applies, so a rendered path never disagrees with the tags it came from. Pure: the path planner
(:mod:`tagmend.engine.paths`) loads the inputs, respells folders on disk and applies the holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from tagmend.engine import path_keys
from tagmend.engine.detector_core import parse_position
from tagmend.engine.path_text import clean_value, is_reserved, part_problems
from tagmend.engine.tags import MANAGED_TAGS

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from tagmend.config import Settings

DEFAULT_PATTERN: Final = (
    "{container|albumartist|artist}/[({year}) ]{albumartist|artist} - {album}/"
    "{albumartist|artist} - {album} - [{disc}-]{tracknumber:02} - {title}"
)

YEAR: Final = "year"
DISC: Final = "disc"
CONTAINER: Final = "container"
COMPUTED_NAMES: Final = frozenset({YEAR, DISC, CONTAINER})
FIELD_NAMES: Final = MANAGED_TAGS | COMPUTED_NAMES

# The tags the computed names and the planner's holds read, whatever the pattern names.
BASE_TAGS: Final = ("date", "originaldate", "discnumber", "tracknumber")

_SEPARATOR: Final = "/"
_SPECIAL: Final = frozenset("{}[]/")
_WIDTH: Final = re.compile(r"[0-9]+")
_POSITION: Final = re.compile(r"\s*([0-9]+)\s*(?:/.*)?", re.DOTALL)
_YEAR_PREFIX: Final = re.compile(r"\s*([0-9]{4})")
_EMPTY_YEAR: Final = "0000"
_FOLDER_LIST_DELIMITER: Final = ";"


# --- the grammar ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Field:
    """One ``{name|name:width}`` field. ``width`` is ``None`` when no padding is asked."""

    names: tuple[str, ...]
    width: int | None


@dataclass(frozen=True, slots=True)
class Group:
    """One ``[...]`` group of literal text and fields."""

    items: tuple[str | Field, ...]


type Item = str | Field | Group


@dataclass(frozen=True, slots=True)
class Pattern:
    """A parsed naming pattern. ``components[-1]`` is the filename component."""

    text: str
    components: tuple[tuple[Item, ...], ...]

    @property
    def component_texts(self) -> tuple[str, ...]:
        """Each component's source text, in order."""
        return tuple(self.text.split(_SEPARATOR))

    def tag_names(self) -> tuple[str, ...]:
        """Return every managed tag the pattern names, sorted."""
        names = {
            name
            for component in self.components
            for field in _fields_of(component)
            for name in field.names
            if name not in COMPUTED_NAMES
        }
        return tuple(sorted(names))


def _fields_of(items: Sequence[Item]) -> list[Field]:
    """Return every field in *items*, group members included."""
    fields: list[Field] = []
    for item in items:
        if isinstance(item, Field):
            fields.append(item)
        elif isinstance(item, Group):
            fields.extend(member for member in item.items if isinstance(member, Field))
    return fields


def parse_pattern(text: str) -> Pattern:
    """Parse *text* into a :class:`Pattern`. Raises :class:`ValueError` naming the first problem.

    Refused: an empty pattern, an empty component, an unknown name, an unbalanced brace or
    bracket, a ``/`` or a group inside a group, an empty group, a width that is not a positive
    number, a literal holding a character a path part cannot hold, and a filename component
    with no field outside every group.
    """
    if not text.strip():
        message = "the naming pattern is empty"
        raise ValueError(message)

    components: list[tuple[Item, ...]] = []
    items: list[Item] = []
    position = 0
    while position < len(text):
        char = text[position]
        if char == _SEPARATOR:
            components.append(_close_component(items, len(components)))
            items = []
            position += 1
        elif char == "{":
            field, position = _parse_field(text, position)
            items.append(field)
        elif char == "[":
            group, position = _parse_group(text, position)
            items.append(group)
        elif char in "}]":
            message = f"unbalanced {char!r} at position {position} of the naming pattern"
            raise ValueError(message)
        else:
            literal, position = _parse_literal(text, position)
            items.append(literal)
    components.append(_close_component(items, len(components)))

    if not any(isinstance(item, Field) for item in components[-1]):
        message = "the filename component needs a field outside every group"
        raise ValueError(message)
    return Pattern(text=text, components=tuple(components))


def _close_component(items: list[Item], index: int) -> tuple[Item, ...]:
    """Return *items* as one component, or raise when it is empty."""
    if not items:
        message = (
            f"component {index + 1} of the naming pattern is empty: a leading, trailing or "
            "doubled '/'"
        )
        raise ValueError(message)
    return tuple(items)


def _parse_literal(text: str, start: int) -> tuple[str, int]:
    """Read literal text from *start* to the next special character."""
    end = start
    while end < len(text) and text[end] not in _SPECIAL:
        end += 1
    literal = text[start:end]
    forbidden = sorted({char for char in literal if not char.isspace() and not clean_value(char)})
    if forbidden:
        message = (
            f"the literal {literal!r} holds {''.join(forbidden)!r}, which a path part cannot "
            "hold. Separate components with '/'"
        )
        raise ValueError(message)
    return literal, end


def _parse_field(text: str, start: int) -> tuple[Field, int]:
    """Read one ``{...}`` field starting at *start*."""
    end = start + 1
    while end < len(text) and text[end] not in _SPECIAL:
        end += 1
    if end >= len(text) or text[end] != "}":
        message = f"unbalanced '{{' at position {start} of the naming pattern"
        raise ValueError(message)
    body = text[start + 1 : end]
    names_text, has_width, width_text = body.partition(":")
    names = tuple(names_text.split("|"))
    unknown = [name for name in names if name not in FIELD_NAMES]
    if unknown:
        message = (
            f"unknown field name(s) {unknown} in {{{body}}}. Known names: "
            f"{', '.join(sorted(FIELD_NAMES))}"
        )
        raise ValueError(message)
    width = None
    if has_width:
        if _WIDTH.fullmatch(width_text) is None or int(width_text) == 0:
            message = f"the width in {{{body}}} must be a positive number such as 02"
            raise ValueError(message)
        width = int(width_text)
    return Field(names=names, width=width), end + 1


def _parse_group(text: str, start: int) -> tuple[Group, int]:
    """Read one ``[...]`` group starting at *start*."""
    items: list[str | Field] = []
    position = start + 1
    while position < len(text):
        char = text[position]
        if char == "]":
            if not items:
                message = f"empty group at position {start} of the naming pattern"
                raise ValueError(message)
            return Group(items=tuple(items)), position + 1
        if char == "{":
            field, position = _parse_field(text, position)
            items.append(field)
        elif char == _SEPARATOR:
            message = f"a '/' inside a group at position {position} of the naming pattern"
            raise ValueError(message)
        elif char == "[":
            message = f"a group inside a group at position {position} of the naming pattern"
            raise ValueError(message)
        elif char == "}":
            message = f"unbalanced '}}' at position {position} of the naming pattern"
            raise ValueError(message)
        else:
            literal, position = _parse_literal(text, position)
            items.append(literal)
    message = f"unbalanced '[' at position {start} of the naming pattern"
    raise ValueError(message)


def pattern_text(settings: Settings) -> str:
    """Return the persisted pattern's text, the default when the setting is empty."""
    return settings.naming_pattern or DEFAULT_PATTERN


def effective_pattern(settings: Settings) -> Pattern:
    """Return the persisted pattern, parsed. Raises :class:`ValueError` for an invalid setting."""
    try:
        return parse_pattern(pattern_text(settings))
    except ValueError as exc:
        message = f"the naming_pattern setting is invalid: {exc}. Fix it with set_naming_pattern"
        raise ValueError(message) from exc


def validate_container_folders(names: Sequence[str]) -> tuple[str, ...]:
    """Return *names* when each is one valid top folder name, or raise :class:`ValueError`.

    Each name is one path part that obeys the part rules and holds no ``;``, the delimiter of
    the stored list.
    """
    problems: list[str] = []
    for name in names:
        if not name:
            problems.append("an empty folder name")
            continue
        if any(char in name for char in (_SEPARATOR, "\\", _FOLDER_LIST_DELIMITER)):
            problems.append(f"{name!r} must be one folder name with no '/', '\\' or ';'")
            continue
        problems.extend(part_problems(name))
    if problems:
        message = "invalid container_folders: " + "; ".join(problems)
        raise ValueError(message)
    return tuple(names)


def folder_list_setting(names: Sequence[str]) -> str:
    """Return *names* in the stored ``container_folders`` form."""
    return _FOLDER_LIST_DELIMITER.join(names)


# --- the renderer --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RenderInput:
    """One file as the renderer reads it.

    ``values`` maps tag names to their first value. ``container`` is the container folder the
    file sits under. ``kept_folder`` holds the file's folder parts when a keep holds its folder,
    so only its filename renders. ``suffix`` is the file's extension as spelled on disk.
    """

    file_id: int
    values: Mapping[str, str]
    container: str | None
    kept_folder: tuple[str, ...] | None
    suffix: str


@dataclass(frozen=True, slots=True)
class Rendered:
    """One file's render.

    ``parts`` are the folder parts then the filename, relative to ``music_path``, empty when
    ``missing`` names the required field that rendered empty. ``levels`` gives each part's
    pattern component index, ``None`` for a kept folder part. ``album_key`` keys the album
    folder (the folders rendered with ``disc`` empty), and ``disc`` is the disc number the
    render used, ``None`` outside a multi-disc album.
    """

    file_id: int
    parts: tuple[str, ...]
    levels: tuple[int | None, ...]
    album_key: str | None
    disc: int | None
    missing: str | None

    @property
    def relative_path(self) -> str | None:
        """The rendered path relative to ``music_path``, ``None`` when held."""
        return str(Path(*self.parts)) if self.parts else None


@dataclass(frozen=True, slots=True)
class _Missing:
    """A required field that rendered empty."""

    name: str


def year_of(values: Mapping[str, str]) -> str:
    """Return the ``year`` computed name: four digits of ``originaldate``, else of ``date``."""
    for name in ("originaldate", "date"):
        match = _YEAR_PREFIX.match(values.get(name, ""))
        if match is not None and match[1] != _EMPTY_YEAR:
            return match[1]
    return ""


def _disc_total(value: str) -> int | None:
    """Return the ``M`` of an ``N/M`` disc number, or ``None``."""
    _, slash, total = value.partition("/")
    stripped = total.strip()
    return int(stripped) if slash and stripped.isdecimal() else None


def _pad(raw: str, width: int) -> str:
    """Zero-pad a position value, dropping its ``/total``. Any other value stays as written."""
    match = _POSITION.fullmatch(raw)
    if match is None:
        return raw
    return str(int(match[1])).zfill(width)


def _field_text(field: Field, values: Mapping[str, str]) -> str:
    """Return the first of *field*'s names that renders non-empty, or ``""``."""
    for name in field.names:
        raw = values.get(name, "").strip()
        if field.width is not None:
            raw = _pad(raw, field.width)
        text = clean_value(raw)
        if text:
            return text
    return ""


def _render_items(
    items: Sequence[Item],
    values: Mapping[str, str],
    *,
    lenient: bool,
) -> str | _Missing:
    """Render one component's items, or name the first required field that renders empty.

    *lenient* lets a required ``disc`` render empty, for the album grouper's first pass.
    """
    pieces: list[str] = []
    for item in items:
        if isinstance(item, str):
            pieces.append(item)
        elif isinstance(item, Field):
            text = _field_text(item, values)
            if not text and not (lenient and DISC in item.names):
                return _Missing(item.names[-1])
            pieces.append(text)
        else:
            texts = [m if isinstance(m, str) else _field_text(m, values) for m in item.items]
            members = zip(item.items, texts, strict=True)
            if all(text for member, text in members if isinstance(member, Field)):
                pieces.extend(texts)
    return "".join(pieces)


def _unreserve(part: str) -> str:
    """Suffix ``_`` to a reserved device name, before its extension."""
    if not is_reserved(part):
        return part
    head, dot, rest = part.partition(".")
    return f"{head.rstrip(' ')}_{dot}{rest}"


def _first_required(items: Sequence[Item]) -> str | None:
    """Return the last name of the component's first required field, or ``None``."""
    return next((item.names[-1] for item in items if isinstance(item, Field)), None)


def _render_folders(
    pattern: Pattern,
    values: Mapping[str, str],
    *,
    lenient: bool,
) -> tuple[tuple[str, ...], tuple[int, ...]] | _Missing:
    """Render the folder components, dropping an empty group-only one.

    A component with a required field that still ends empty, such as an album named ``...``
    once its trailing dots go, holds that field.
    """
    parts: list[str] = []
    levels: list[int] = []
    for index, items in enumerate(pattern.components[:-1]):
        rendered = _render_items(items, values, lenient=lenient)
        if isinstance(rendered, _Missing):
            return rendered
        part = _unreserve(clean_value(rendered).rstrip(". "))
        if part:
            parts.append(part)
            levels.append(index)
            continue
        required = _first_required(items)
        if required is not None and not lenient:
            return _Missing(required)
    return tuple(parts), tuple(levels)


def _render_filename(
    pattern: Pattern,
    values: Mapping[str, str],
    suffix: str,
    *,
    lenient: bool,
) -> str | _Missing:
    """Render the filename component with the file's extension appended.

    The stem is never empty: the component holds a required field, which rendered non-empty.
    """
    rendered = _render_items(pattern.components[-1], values, lenient=lenient)
    if isinstance(rendered, _Missing):
        return rendered
    return _unreserve(f"{clean_value(rendered)}{suffix}".rstrip(". "))


def _render_one(
    pattern: Pattern,
    item: RenderInput,
    values: Mapping[str, str],
    *,
    lenient: bool,
) -> tuple[tuple[str, ...], tuple[int | None, ...]] | _Missing:
    """Render one file's parts and their levels with the computed names already in *values*."""
    folders: tuple[str, ...]
    folder_levels: tuple[int | None, ...]
    if item.kept_folder is not None:
        folders = item.kept_folder
        folder_levels = tuple(None for _ in folders)
    else:
        rendered = _render_folders(pattern, values, lenient=lenient)
        if isinstance(rendered, _Missing):
            return rendered
        folders, folder_levels = rendered
    filename = _render_filename(pattern, values, item.suffix, lenient=lenient)
    if isinstance(filename, _Missing):
        return filename
    return (*folders, filename), (*folder_levels, len(pattern.components) - 1)


def _values(item: RenderInput, disc: str) -> dict[str, str]:
    """Return *item*'s tag values with the three computed names added."""
    return {
        **item.values,
        YEAR: year_of(item.values),
        CONTAINER: item.container or "",
        DISC: disc,
    }


def _album_key(parts: tuple[str, ...]) -> str:
    """Return the key of the album folder holding the file *parts* name."""
    return path_keys.path_key(Path(*parts[:-1])) if len(parts) > 1 else ""


def _multi_disc(members: list[RenderInput]) -> bool:
    """Whether an album's files carry more than one disc number, or any total above 1."""
    discs = {parse_position(member.values.get("discnumber")) for member in members}
    totals = (_disc_total(member.values.get("discnumber", "")) for member in members)
    return len(discs - {None}) > 1 or any(total is not None and total > 1 for total in totals)


def render_library(pattern: Pattern, inputs: Sequence[RenderInput]) -> dict[int, Rendered]:
    """Render every file in *inputs*, judging multi-disc albums across them.

    A first pass renders each file with ``disc`` empty, holding a file whose required field
    renders empty, and keys the rest by their album folder. An album is multi-disc when its
    files carry more than one disc number, or when any file's disc total is above 1. The
    second pass renders each file with its disc number when its album is multi-disc.
    """
    results: dict[int, Rendered] = {}
    albums: dict[str, list[RenderInput]] = {}
    album_of: dict[int, str] = {}
    for item in inputs:
        first = _render_one(pattern, item, _values(item, ""), lenient=True)
        if isinstance(first, _Missing):
            results[item.file_id] = Rendered(item.file_id, (), (), None, None, first.name)
            continue
        album_of[item.file_id] = _album_key(first[0])
        albums.setdefault(album_of[item.file_id], []).append(item)

    multi = {key: _multi_disc(members) for key, members in albums.items()}
    for item in inputs:
        key = album_of.get(item.file_id)
        if key is None:
            continue
        disc = parse_position(item.values.get("discnumber")) if multi[key] else None
        values = _values(item, "" if disc is None else str(disc))
        final = _render_one(pattern, item, values, lenient=False)
        if isinstance(final, _Missing):
            results[item.file_id] = Rendered(item.file_id, (), (), key, disc, final.name)
            continue
        parts, levels = final
        results[item.file_id] = Rendered(item.file_id, parts, levels, key, disc, None)
    return results
