"""The value rule a tag value obeys inside one path part.

The path comparator applies it to every tag value before comparing, and the path renderer applies
it to every tag value it writes, so a path rendered from a file's tags never disagrees with them.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

# The characters Windows refuses in a file or folder name, plus every C0 and C1 control.
_FORBIDDEN: Final = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]')


def clean_value(text: str) -> str:
    """Return *text* with forbidden and control characters deleted, whitespace collapsed, NFC."""
    deleted = _FORBIDDEN.sub("", text)
    # NFC after the deletion, so a combining mark that followed a deleted character still composes.
    composed = unicodedata.normalize("NFC", deleted)
    return " ".join(composed.split())
