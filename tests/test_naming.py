"""Tests for the naming pattern grammar, the album grouper and the renderer (pure, no disk)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tagmend.engine import naming
from tagmend.engine.tags import MANAGED_TAGS

_DEFAULT = naming.parse_pattern(naming.DEFAULT_PATTERN)


def _input(
    file_id: int,
    *,
    kept_folder: tuple[str, ...] | None = None,
    container: str | None = None,
    suffix: str = ".flac",
    **values: str,
) -> naming.RenderInput:
    base = {
        "albumartist": "Artist",
        "album": "Album",
        "date": "2001",
        "tracknumber": str(file_id),
        "title": f"Song {file_id}",
    }
    return naming.RenderInput(
        file_id=file_id,
        values={**base, **values},
        container=container,
        kept_folder=kept_folder,
        suffix=suffix,
    )


def _paths(inputs: list[naming.RenderInput], pattern: naming.Pattern = _DEFAULT) -> list[str]:
    rendered = naming.render_library(pattern, inputs)
    return [rendered[item.file_id].relative_path or "" for item in inputs]


def _path(*parts: str) -> str:
    return str(Path(*parts))


# --- grammar ---------------------------------------------------------------------------


def test_the_default_pattern_parses_into_three_components() -> None:
    assert len(_DEFAULT.components) == 3
    assert _DEFAULT.component_texts[0] == "{container|albumartist|artist}"
    assert _DEFAULT.tag_names() == ("album", "albumartist", "artist", "title", "tracknumber")


def test_the_computed_names_are_not_tags() -> None:
    assert not naming.COMPUTED_NAMES & MANAGED_TAGS


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("{artist}/{nope}", "unknown field name"),
        ("{artist}/{title", "unbalanced '{'"),
        ("{artist}/title}", "unbalanced '}'"),
        ("{artist}/[{title}", "unbalanced '['"),
        ("{artist}/{title}]", "unbalanced ']'"),
        ("{artist}/[{album}/{title}]", "'/' inside a group"),
        ("{artist}/[[{album}]]{title}", "group inside a group"),
        ("{artist}/[]{title}", "empty group"),
        ("{artist}//{title}", "component 2"),
        ("/{title}", "component 1"),
        ("{artist}/", "component 2"),
        ("{artist}/{tracknumber:x}", "positive number"),
        ("{artist}/{tracknumber:0}", "positive number"),
        ("{artist}\\{title}", "cannot hold"),
        ("{artist}/a?{title}", "cannot hold"),
        ("{artist}/[{title}]", "needs a field outside every group"),
        ("{artist}/{artist||title}", "unknown field name"),
    ],
)
def test_parse_pattern_rejects(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        naming.parse_pattern(text)


def test_a_group_only_folder_component_is_accepted() -> None:
    pattern = naming.parse_pattern("{albumartist}/[CD{disc}]/{tracknumber:02} {title}")

    assert len(pattern.components) == 3


def test_validate_container_folders() -> None:
    assert naming.validate_container_folders(["Soundtracks", "Fahrenheit Project"]) == (
        "Soundtracks",
        "Fahrenheit Project",
    )
    for bad in (["a;b"], ["a/b"], ["trailing."], ["CON"], [""], [" edge"]):
        with pytest.raises(ValueError, match="invalid container_folders"):
            naming.validate_container_folders(bad)


def test_year_of_prefers_originaldate_and_skips_0000() -> None:
    assert naming.year_of({"originaldate": "1999-02-01", "date": "2005"}) == "1999"
    assert naming.year_of({"originaldate": "0000", "date": "2005-01-01"}) == "2005"
    assert naming.year_of({"date": "0000"}) == ""
    assert naming.year_of({}) == ""


# --- rendering ---------------------------------------------------------------------------


def test_the_default_pattern_renders_the_owners_layout() -> None:
    assert _paths([_input(1)]) == [
        _path("Artist", "(2001) Artist - Album", "Artist - Album - 01 - Song 1.flac"),
    ]


def test_a_multi_disc_album_renders_flat_with_disc_prefixes() -> None:
    inputs = [
        _input(1, discnumber="1/2", tracknumber="1"),
        _input(2, discnumber="2/2", tracknumber="1"),
    ]

    assert _paths(inputs) == [
        _path("Artist", "(2001) Artist - Album", "Artist - Album - 1-01 - Song 1.flac"),
        _path("Artist", "(2001) Artist - Album", "Artist - Album - 2-01 - Song 2.flac"),
    ]


def test_a_disc_total_above_one_makes_an_album_multi_disc() -> None:
    assert _paths([_input(1, discnumber="1/2")]) == [
        _path("Artist", "(2001) Artist - Album", "Artist - Album - 1-01 - Song 1.flac"),
    ]


def test_a_single_disc_album_renders_no_prefix() -> None:
    inputs = [_input(1, discnumber="1/1"), _input(2, discnumber="1")]

    assert [Path(p).name for p in _paths(inputs)] == [
        "Artist - Album - 01 - Song 1.flac",
        "Artist - Album - 02 - Song 2.flac",
    ]


def test_a_kept_folder_judges_each_album_on_its_own_discs() -> None:
    kept = ("Various", "Featured")
    inputs = [
        _input(1, kept_folder=kept, album="Double", discnumber="1/2"),
        _input(2, kept_folder=kept, album="Single", discnumber="1/1"),
    ]

    assert [Path(p).name for p in _paths(inputs)] == [
        "Artist - Double - 1-01 - Song 1.flac",
        "Artist - Single - 02 - Song 2.flac",
    ]


def test_an_empty_group_drops_and_artist_falls_back() -> None:
    item = _input(1, albumartist="", artist="Solo", date="", tracknumber="7/12")

    assert _paths([item]) == [_path("Solo", "Solo - Album", "Solo - Album - 07 - Song 1.flac")]


def test_values_lose_forbidden_characters_and_folders_lose_trailing_dots() -> None:
    item = _input(1, album='What? A/B: "C"...', title="M.I.A.", albumartist="Ünïcode")

    assert _paths([item]) == [
        _path(
            "Ünïcode",
            "(2001) Ünïcode - What AB C",
            "Ünïcode - What AB C... - 01 - M.I.A..flac",
        ),
    ]


def test_a_reserved_device_name_takes_an_underscore() -> None:
    item = _input(1, albumartist="CON")
    pattern = naming.parse_pattern("{albumartist}/{albumartist}")

    assert _paths([item], pattern) == [_path("CON_", "CON_.flac")]


def test_a_blank_required_field_holds_with_its_last_name() -> None:
    rendered = naming.render_library(_DEFAULT, [_input(1, albumartist="", title="")])

    assert rendered[1].missing == "artist"
    assert rendered[1].relative_path is None


def test_the_container_field_keeps_the_container_as_the_first_part() -> None:
    item = _input(1, container="Soundtracks", albumartist="Composer")

    assert Path(_paths([item])[0]).parts[0] == "Soundtracks"


def test_a_kept_folder_renders_only_the_filename() -> None:
    item = _input(1, kept_folder=("Odd", "Place"))
    rendered = naming.render_library(_DEFAULT, [item])[1]

    assert rendered.relative_path == _path("Odd", "Place", "Artist - Album - 01 - Song 1.flac")
    assert rendered.levels == (None, None, 2)


def test_a_width_pads_a_position_and_leaves_other_values_as_written() -> None:
    pattern = naming.parse_pattern("{albumartist}/{tracknumber:03} {title}")
    inputs = [_input(1, tracknumber="4/12"), _input(2, tracknumber="A1")]

    assert [Path(p).name for p in _paths(inputs, pattern)] == ["004 Song 1.flac", "A1 Song 2.flac"]


def test_a_required_disc_folder_renders_per_disc_and_holds_a_single_disc_album() -> None:
    pattern = naming.parse_pattern("{albumartist}/{album}/CD{disc}/{tracknumber:02}")
    multi = [_input(1, discnumber="1/2"), _input(2, discnumber="2/2")]
    single = [_input(3, album="Other", discnumber="1/1")]

    rendered = naming.render_library(pattern, [*multi, *single])

    assert rendered[1].relative_path == _path("Artist", "Album", "CD1", "01.flac")
    assert rendered[2].relative_path == _path("Artist", "Album", "CD2", "02.flac")
    assert rendered[3].missing == "disc"


def test_an_album_named_only_dots_holds_its_folder_field() -> None:
    pattern = naming.parse_pattern("{albumartist}/{album}/{title}")

    assert naming.render_library(pattern, [_input(1, album="...")])[1].missing == "album"
