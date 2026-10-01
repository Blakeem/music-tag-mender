"""The value rule a tag value obeys inside one path part, and the rules an entire part obeys.

The path comparator applies the value rule to every tag value before comparing, and the path
renderer applies it to every tag value it writes, so a path rendered from a file's tags never
disagrees with them. The part rules are the ones the staging tools enforce on every target part.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

# The characters Windows refuses in a file or folder name, plus every C0 and C1 control.
_FORBIDDEN: Final = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]')

RESERVED_NAMES: Final = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    },
)


def clean_value(text: str) -> str:
    """Return *text* with forbidden and control characters deleted, whitespace collapsed, NFC."""
    deleted = _FORBIDDEN.sub("", text)
    # NFC after the deletion, so a combining mark that followed a deleted character still composes.
    composed = unicodedata.normalize("NFC", deleted)
    return " ".join(composed.split())


def is_reserved(part: str) -> bool:
    """Whether *part* names a Windows device, with or without an extension."""
    return part.split(".", maxsplit=1)[0].rstrip(" ").upper() in RESERVED_NAMES


def part_problems(part: str) -> list[str]:
    """Return every way one path *part* breaks the part rules, empty when it obeys them.

    A part keeps its value under :func:`clean_value`, ends in no dot or space and names no
    reserved device.
    """
    problems: list[str] = []
    if clean_value(part) != part:
        problems.append(
            f"part {part!r} holds a character a path part cannot hold, edge or repeated "
            "whitespace, or a non-NFC form",
        )
    if part.rstrip(". ") != part:
        problems.append(f"part {part!r} ends with a dot or a space")
    if is_reserved(part):
        problems.append(f"part {part!r} is a reserved device name")
    return problems
