"""Unit tests for :mod:`tagmend.engine.axis` — the parameterised status abstraction.

These tests pin each axis's descriptor (fields, identity, scope fields, statuses) and the
mismatch disposition rule directly, so a refactor cannot silently change semantics. The
classifier that reads the outcome rows is covered in ``test_store_genre.py`` and
``test_axis_status.py``.

These are pure-unit tests on frozen dataclasses and module-level constants: no DB,
no temp library, no audio files.
"""

from __future__ import annotations

import pytest

from tagmend.engine.axis import (
    ARTIST_AXIS,
    GENRE_AXIS,
    MISMATCH_AXIS,
    RESOLVER_OUTCOMES,
    SONG_AXIS,
    TAG_AXES,
    YEAR_AXIS,
    Identity,
    LookupIdentity,
    StatusRow,
    field_values,
    identity_of,
    lookup_identity,
    mismatch_decision_blocks,
)

# ---------------------------------------------------------------------------
# Genre axis: config invariants
# ---------------------------------------------------------------------------


def test_genre_axis_name() -> None:
    assert GENRE_AXIS.name == "genre"


def test_genre_axis_fields() -> None:
    assert GENRE_AXIS.fields == ("genre",)


def test_genre_axis_status_table() -> None:
    assert GENRE_AXIS.status_table == "file_genre_status"


def test_genre_axis_source_columns() -> None:
    assert GENRE_AXIS.source_columns == ("source_artist", "source_album")


def test_genre_axis_workflow_statuses_exact_set() -> None:
    assert GENRE_AXIS.workflow_statuses == frozenset(
        {"pending", "no_identity", "no_match", "manual", "staged", "done"}
    )


def test_genre_axis_workflow_statuses_cardinality() -> None:
    assert len(GENRE_AXIS.workflow_statuses) == 6


# ---------------------------------------------------------------------------
# Artist axis: config invariants
# ---------------------------------------------------------------------------


def test_artist_axis_name() -> None:
    assert ARTIST_AXIS.name == "artist"


def test_artist_axis_fields() -> None:
    # Every field resolve_artists writes, so each is part of the decided value.
    assert ARTIST_AXIS.fields == (
        "artist",
        "albumartist",
        "artists",
        "musicbrainz_artistid",
        "musicbrainz_albumartistid",
        "artistsort",
        "albumartistsort",
    )


def test_artist_axis_status_table() -> None:
    assert ARTIST_AXIS.status_table == "file_artist_status"


def test_artist_axis_source_columns() -> None:
    assert ARTIST_AXIS.source_columns == ("source_artist", "source_albumartist")


def test_artist_axis_workflow_statuses_exact_set() -> None:
    # A held name records no_match, so the artist axis carries the full tag-axis set.
    assert ARTIST_AXIS.workflow_statuses == frozenset(
        {"pending", "no_identity", "no_match", "manual", "staged", "done"}
    )


def test_scope_fields_are_each_axis_lookup_names() -> None:
    assert GENRE_AXIS.scope_fields == ("artist", "albumartist")
    assert ARTIST_AXIS.scope_fields == ("artist", "albumartist")
    assert YEAR_AXIS.scope_fields == ("album",)
    assert MISMATCH_AXIS.scope_fields == ("artist", "albumartist")


def test_no_identity_in_genre_artist_year_but_not_mismatch() -> None:
    # The derived worklist state rides the three identity-driven axes; the mismatch axis
    # (stored-or-pending only) must stay unchanged.
    assert "no_identity" in GENRE_AXIS.workflow_statuses
    assert "no_identity" in ARTIST_AXIS.workflow_statuses
    assert "no_identity" in YEAR_AXIS.workflow_statuses
    assert "no_identity" not in MISMATCH_AXIS.workflow_statuses


def test_tag_axes_are_genre_artist_year_song_in_report_order() -> None:
    assert TAG_AXES == (GENRE_AXIS, ARTIST_AXIS, YEAR_AXIS, SONG_AXIS)


def test_song_axis_descriptor() -> None:
    assert SONG_AXIS.name == "song"
    assert SONG_AXIS.fields == ("title", "tracknumber", "discnumber")
    assert SONG_AXIS.status_table == "file_song_status"
    assert SONG_AXIS.scope_fields == ("album",)
    # The resolver never writes no_match, and the identity is never None.
    assert frozenset({"pending", "manual", "staged", "done"}) == SONG_AXIS.workflow_statuses


def test_song_identity_is_the_release_ids_and_never_none() -> None:
    assert identity_of(SONG_AXIS, {}) == Identity(primary="", secondary="")
    tags = {"musicbrainz_albumid": ["rel"], "musicbrainz_releasetrackid": [" ", "rt"]}
    assert identity_of(SONG_AXIS, tags) == Identity(primary="rel", secondary="rt")


def test_resolver_outcomes_are_done_and_no_match() -> None:
    assert frozenset({"done", "no_match"}) == RESOLVER_OUTCOMES


# ---------------------------------------------------------------------------
# Axis identity: the tuple an outcome is decided against
# ---------------------------------------------------------------------------


def test_genre_identity_is_albumartist_else_artist_and_album() -> None:
    tags = {"artist": ["Track"], "albumartist": ["Album Artist"], "album": ["LP"]}
    assert identity_of(GENRE_AXIS, tags) == Identity(primary="Album Artist", secondary="LP")
    assert identity_of(GENRE_AXIS, {"artist": ["Solo"]}) == Identity(
        primary="Solo",
        secondary=None,
    )


def test_genre_identity_is_none_without_either_artist_field() -> None:
    assert identity_of(GENRE_AXIS, {"album": ["LP"], "artist": ["  "]}) is None


def test_year_identity_also_needs_an_album() -> None:
    assert identity_of(YEAR_AXIS, {"artist": ["Band"]}) is None
    assert identity_of(YEAR_AXIS, {"artist": ["Band"], "album": ["LP"]}) == Identity(
        primary="Band",
        secondary="LP",
    )


def test_artist_identity_is_first_artist_and_first_albumartist() -> None:
    tags = {"artist": ["", "Track"], "albumartist": ["Album Artist"]}
    assert identity_of(ARTIST_AXIS, tags) == Identity(primary="Track", secondary="Album Artist")
    assert identity_of(ARTIST_AXIS, {"albumartist": ["AA"]}) == Identity(
        primary=None,
        secondary="AA",
    )
    assert identity_of(ARTIST_AXIS, {"album": ["LP"]}) is None


def test_mismatch_axis_has_no_identity() -> None:
    with pytest.raises(ValueError, match="mismatch axis keeps no outcome rows"):
        identity_of(MISMATCH_AXIS, {"artist": ["A"]})


def test_field_values_lists_every_axis_field_and_absent_as_empty() -> None:
    tags = {"artist": ["A"], "artistsort": ["A, The"], "genre": ["rock"]}
    assert field_values(ARTIST_AXIS, tags) == {
        "artist": ["A"],
        "albumartist": [],
        "artists": [],
        "musicbrainz_artistid": [],
        "musicbrainz_albumartistid": [],
        "artistsort": ["A, The"],
        "albumartistsort": [],
    }
    assert field_values(GENRE_AXIS, tags) == {"genre": ["rock"]}


# ---------------------------------------------------------------------------
# Year axis: config invariants (a near-clone of the genre axis)
# ---------------------------------------------------------------------------


def test_year_axis_name() -> None:
    assert YEAR_AXIS.name == "year"


def test_year_axis_fields() -> None:
    assert YEAR_AXIS.fields == ("originaldate",)


def test_year_axis_status_table() -> None:
    assert YEAR_AXIS.status_table == "file_year_status"


def test_year_axis_source_columns() -> None:
    assert YEAR_AXIS.source_columns == ("source_artist", "source_album")


def test_year_axis_workflow_statuses_exact_set() -> None:
    assert YEAR_AXIS.workflow_statuses == frozenset(
        {"pending", "no_identity", "no_match", "manual", "staged", "done"}
    )


def test_year_axis_workflow_statuses_cardinality() -> None:
    assert len(YEAR_AXIS.workflow_statuses) == 6


def test_no_match_in_year_workflow_statuses() -> None:
    assert "no_match" in YEAR_AXIS.workflow_statuses


# ---------------------------------------------------------------------------
# Mismatch axis: config invariants (positional source: field name + value)
# ---------------------------------------------------------------------------


def test_mismatch_axis_name() -> None:
    assert MISMATCH_AXIS.name == "mismatch"


def test_mismatch_axis_fields() -> None:
    # The detect fields — recorded but NEVER used for staged/done derivation on this axis.
    assert MISMATCH_AXIS.fields == ("albumartist", "artist")


def test_mismatch_axis_status_table() -> None:
    assert MISMATCH_AXIS.status_table == "file_mismatch_status"


def test_mismatch_axis_source_columns() -> None:
    assert MISMATCH_AXIS.source_columns == ("source_field", "source_value")


def test_mismatch_axis_workflow_statuses_exact_set() -> None:
    assert MISMATCH_AXIS.workflow_statuses == frozenset(
        {"pending", "legit_ignore", "misfiled_deferred"}
    )


def test_mismatch_axis_workflow_statuses_cardinality() -> None:
    assert len(MISMATCH_AXIS.workflow_statuses) == 3


def test_mismatch_has_no_no_match_or_staged_done_states() -> None:
    assert "no_match" not in MISMATCH_AXIS.workflow_statuses
    assert "staged" not in MISMATCH_AXIS.workflow_statuses
    assert "done" not in MISMATCH_AXIS.workflow_statuses


# ---------------------------------------------------------------------------
# mismatch_decision_blocks: blocks iff the snapshotted value still matches its field
# ---------------------------------------------------------------------------
# source_primary = the FIELD NAME ('albumartist'|'artist'); source_secondary = its snapshot.
# identity.primary = current first albumartist; identity.secondary = current first artist.


def test_mismatch_albumartist_blocks_when_value_unchanged() -> None:
    decision = StatusRow(
        status="legit_ignore", source_primary="albumartist", source_secondary="Jem"
    )
    identity = Identity(primary="Jem", secondary="Ozzy Osbourne")
    assert mismatch_decision_blocks(decision, identity) is True


def test_mismatch_albumartist_stale_when_value_changed() -> None:
    decision = StatusRow(
        status="legit_ignore", source_primary="albumartist", source_secondary="Jem"
    )
    identity = Identity(primary="Ozzy Osbourne", secondary="Ozzy Osbourne")
    assert mismatch_decision_blocks(decision, identity) is False


def test_mismatch_albumartist_stale_when_tag_removed() -> None:
    # The albumartist tag was cleared (current is None) -> the snapshot no longer matches.
    decision = StatusRow(
        status="misfiled_deferred", source_primary="albumartist", source_secondary="Jem"
    )
    identity = Identity(primary=None, secondary="Ozzy Osbourne")
    assert mismatch_decision_blocks(decision, identity) is False


def test_mismatch_artist_field_compares_against_secondary() -> None:
    decision = StatusRow(status="misfiled_deferred", source_primary="artist", source_secondary="X")
    # Its own field (artist) is compared against identity.secondary, not primary.
    assert mismatch_decision_blocks(decision, Identity(primary="Y", secondary="X")) is True
    assert mismatch_decision_blocks(decision, Identity(primary="X", secondary="Z")) is False


def test_mismatch_misfiled_deferred_follows_the_same_rule() -> None:
    fresh = StatusRow(
        status="misfiled_deferred", source_primary="albumartist", source_secondary="Q"
    )
    assert mismatch_decision_blocks(fresh, Identity(primary="Q", secondary=None)) is True
    assert mismatch_decision_blocks(fresh, Identity(primary="R", secondary=None)) is False


def test_mismatch_null_source_field_blocks_only_when_snapshot_none() -> None:
    # A file that had neither tag: field None, value None -> compares None == None -> blocks.
    both_none = StatusRow(status="legit_ignore", source_primary=None, source_secondary=None)
    assert mismatch_decision_blocks(both_none, Identity(primary=None, secondary=None)) is True
    assert mismatch_decision_blocks(both_none, Identity(primary="A", secondary="B")) is True


def test_mismatch_none_value_on_albumartist_field() -> None:
    # source_field set but snapshot None: blocks only while current is also None.
    decision = StatusRow(status="legit_ignore", source_primary="albumartist", source_secondary=None)
    assert mismatch_decision_blocks(decision, Identity(primary=None, secondary="X")) is True
    assert mismatch_decision_blocks(decision, Identity(primary="V", secondary="X")) is False


# ---------------------------------------------------------------------------
# Lookup identity
# ---------------------------------------------------------------------------


def test_lookup_identity_prefers_first_nonblank_albumartist_over_artist() -> None:
    tags = {"albumartist": ["", "Various Artists"], "artist": ["Track Artist"], "album": ["A"]}
    assert lookup_identity(tags) == LookupIdentity(artist="Various Artists", album="A")


def test_lookup_identity_treats_whitespace_only_as_absent() -> None:
    tags = {"albumartist": [" \t"], "artist": ["\n", "Real Artist"], "album": ["  "]}
    assert lookup_identity(tags) == LookupIdentity(artist="Real Artist", album=None)
    assert lookup_identity({"artist": ["   "]}) == LookupIdentity(artist=None, album=None)


def test_lookup_identity_takes_first_nonblank_album_at_any_ordinal() -> None:
    tags = {"artist": ["Band"], "album": ["", " ", "Third Ordinal"]}
    assert lookup_identity(tags) == LookupIdentity(artist="Band", album="Third Ordinal")
