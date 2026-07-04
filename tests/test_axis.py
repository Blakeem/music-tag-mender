"""Unit tests for :mod:`tagmend.engine.axis` — the parameterised status abstraction.

This module is covered only indirectly by the genre/artist pipeline tests; these
tests pin the public API and the two asymmetries documented in the module docstring
directly, so a refactor cannot silently change semantics.

These are pure-unit tests on frozen dataclasses and module-level constants: no DB,
no temp library, no audio files.
"""

from __future__ import annotations

from tagmend.engine.axis import (
    ALBUM_AXIS,
    ARTIST_AXIS,
    GENRE_AXIS,
    MISMATCH_AXIS,
    Identity,
    StatusRow,
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
        {"pending", "no_match", "manual", "staged", "done"}
    )


def test_genre_axis_workflow_statuses_cardinality() -> None:
    assert len(GENRE_AXIS.workflow_statuses) == 5


# ---------------------------------------------------------------------------
# Artist axis: config invariants
# ---------------------------------------------------------------------------


def test_artist_axis_name() -> None:
    assert ARTIST_AXIS.name == "artist"


def test_artist_axis_fields() -> None:
    assert ARTIST_AXIS.fields == ("artist", "albumartist")


def test_artist_axis_status_table() -> None:
    assert ARTIST_AXIS.status_table == "file_artist_status"


def test_artist_axis_source_columns() -> None:
    assert ARTIST_AXIS.source_columns == ("source_artist", "source_albumartist")


def test_artist_axis_workflow_statuses_exact_set() -> None:
    assert ARTIST_AXIS.workflow_statuses == frozenset({"pending", "manual", "staged", "done"})


def test_artist_axis_workflow_statuses_cardinality() -> None:
    assert len(ARTIST_AXIS.workflow_statuses) == 4


# ---------------------------------------------------------------------------
# The asymmetry: no_match present in genre, absent in artist
# ---------------------------------------------------------------------------


def test_no_match_in_genre_workflow_statuses() -> None:
    assert "no_match" in GENRE_AXIS.workflow_statuses


def test_no_match_not_in_artist_workflow_statuses() -> None:
    assert "no_match" not in ARTIST_AXIS.workflow_statuses


def test_genre_has_five_states_artist_has_four() -> None:
    assert len(GENRE_AXIS.workflow_statuses) == 5
    assert len(ARTIST_AXIS.workflow_statuses) == 4


# ---------------------------------------------------------------------------
# Genre decision_blocks: manual (sticky, identity-independent)
# ---------------------------------------------------------------------------


def test_genre_manual_blocks_when_identity_matches() -> None:
    decision = StatusRow(
        status="manual", source_primary="Radiohead", source_secondary="OK Computer"
    )
    identity = Identity(primary="Radiohead", secondary="OK Computer")
    assert GENRE_AXIS.decision_blocks(decision, identity) is True


def test_genre_manual_blocks_when_identity_differs() -> None:
    # manual is sticky — identity change does NOT un-block it.
    decision = StatusRow(status="manual", source_primary="Old Artist", source_secondary="Old Album")
    identity = Identity(primary="New Artist", secondary="New Album")
    assert GENRE_AXIS.decision_blocks(decision, identity) is True


def test_genre_manual_blocks_with_null_sources() -> None:
    decision = StatusRow(status="manual", source_primary=None, source_secondary=None)
    identity = Identity(primary=None, secondary=None)
    assert GENRE_AXIS.decision_blocks(decision, identity) is True


# ---------------------------------------------------------------------------
# Genre decision_blocks: no_match (stale when identity changes)
# ---------------------------------------------------------------------------


def test_genre_no_match_blocks_when_identity_unchanged() -> None:
    # Source matches current identity → decision is still valid → blocks.
    decision = StatusRow(
        status="no_match",
        source_primary="Boards of Canada",
        source_secondary="Geogaddi",
    )
    identity = Identity(primary="Boards of Canada", secondary="Geogaddi")
    assert GENRE_AXIS.decision_blocks(decision, identity) is True


def test_genre_no_match_does_not_block_when_primary_changed() -> None:
    # Artist changed → stored no_match is stale → does NOT block.
    decision = StatusRow(
        status="no_match",
        source_primary="Old Artist",
        source_secondary="Same Album",
    )
    identity = Identity(primary="New Artist", secondary="Same Album")
    assert GENRE_AXIS.decision_blocks(decision, identity) is False


def test_genre_no_match_does_not_block_when_secondary_changed() -> None:
    # Album changed → stored no_match is stale → does NOT block.
    decision = StatusRow(
        status="no_match",
        source_primary="Same Artist",
        source_secondary="Old Album",
    )
    identity = Identity(primary="Same Artist", secondary="New Album")
    assert GENRE_AXIS.decision_blocks(decision, identity) is False


def test_genre_no_match_does_not_block_when_both_changed() -> None:
    decision = StatusRow(
        status="no_match",
        source_primary="Old Artist",
        source_secondary="Old Album",
    )
    identity = Identity(primary="New Artist", secondary="New Album")
    assert GENRE_AXIS.decision_blocks(decision, identity) is False


def test_genre_no_match_blocks_when_both_null_and_identity_null() -> None:
    # Null matches null — not stale.
    decision = StatusRow(status="no_match", source_primary=None, source_secondary=None)
    identity = Identity(primary=None, secondary=None)
    assert GENRE_AXIS.decision_blocks(decision, identity) is True


def test_genre_no_match_stale_when_null_source_but_non_null_identity() -> None:
    decision = StatusRow(status="no_match", source_primary=None, source_secondary=None)
    identity = Identity(primary="Some Artist", secondary="Some Album")
    assert GENRE_AXIS.decision_blocks(decision, identity) is False


# ---------------------------------------------------------------------------
# Genre decision_blocks: other statuses do not block
# ---------------------------------------------------------------------------


def test_genre_pending_does_not_block() -> None:
    decision = StatusRow(status="pending", source_primary="A", source_secondary="B")
    identity = Identity(primary="A", secondary="B")
    assert GENRE_AXIS.decision_blocks(decision, identity) is False


def test_genre_staged_does_not_block() -> None:
    decision = StatusRow(status="staged", source_primary="A", source_secondary="B")
    identity = Identity(primary="A", secondary="B")
    assert GENRE_AXIS.decision_blocks(decision, identity) is False


def test_genre_done_does_not_block() -> None:
    decision = StatusRow(status="done", source_primary="A", source_secondary="B")
    identity = Identity(primary="A", secondary="B")
    assert GENRE_AXIS.decision_blocks(decision, identity) is False


# ---------------------------------------------------------------------------
# Artist decision_blocks: manual is sticky (identity-independent)
# ---------------------------------------------------------------------------


def test_artist_manual_blocks_when_identity_matches() -> None:
    decision = StatusRow(status="manual", source_primary="Miami Nights 84", source_secondary="VA")
    identity = Identity(primary="Miami Nights 84", secondary="VA")
    assert ARTIST_AXIS.decision_blocks(decision, identity) is True


def test_artist_manual_blocks_when_identity_differs() -> None:
    # Artist sticky: identity change does NOT un-block (no staleness re-check).
    decision = StatusRow(status="manual", source_primary="Old Artist", source_secondary="Old AA")
    identity = Identity(primary="New Artist", secondary="New AA")
    assert ARTIST_AXIS.decision_blocks(decision, identity) is True


def test_artist_manual_blocks_with_null_sources() -> None:
    decision = StatusRow(status="manual", source_primary=None, source_secondary=None)
    identity = Identity(primary=None, secondary=None)
    assert ARTIST_AXIS.decision_blocks(decision, identity) is True


# ---------------------------------------------------------------------------
# Artist decision_blocks: non-manual statuses do not block
# ---------------------------------------------------------------------------


def test_artist_pending_does_not_block() -> None:
    decision = StatusRow(status="pending", source_primary="A", source_secondary="B")
    identity = Identity(primary="A", secondary="B")
    assert ARTIST_AXIS.decision_blocks(decision, identity) is False


def test_artist_staged_does_not_block() -> None:
    decision = StatusRow(status="staged", source_primary="A", source_secondary="B")
    identity = Identity(primary="A", secondary="B")
    assert ARTIST_AXIS.decision_blocks(decision, identity) is False


def test_artist_done_does_not_block() -> None:
    decision = StatusRow(status="done", source_primary="A", source_secondary="B")
    identity = Identity(primary="A", secondary="B")
    assert ARTIST_AXIS.decision_blocks(decision, identity) is False


def test_artist_non_manual_does_not_block_even_when_identity_unchanged() -> None:
    # Confirm the identity argument is irrelevant for non-manual statuses.
    decision = StatusRow(status="staged", source_primary="X", source_secondary="Y")
    identity_match = Identity(primary="X", secondary="Y")
    identity_diff = Identity(primary="Z", secondary="W")
    assert ARTIST_AXIS.decision_blocks(decision, identity_match) is False
    assert ARTIST_AXIS.decision_blocks(decision, identity_diff) is False


# ---------------------------------------------------------------------------
# Album axis: config invariants (a near-clone of the genre axis)
# ---------------------------------------------------------------------------


def test_album_axis_name() -> None:
    assert ALBUM_AXIS.name == "album"


def test_album_axis_fields() -> None:
    assert ALBUM_AXIS.fields == ("originaldate",)


def test_album_axis_status_table() -> None:
    assert ALBUM_AXIS.status_table == "file_album_status"


def test_album_axis_source_columns() -> None:
    assert ALBUM_AXIS.source_columns == ("source_artist", "source_album")


def test_album_axis_workflow_statuses_exact_set() -> None:
    assert ALBUM_AXIS.workflow_statuses == frozenset(
        {"pending", "no_match", "manual", "staged", "done"}
    )


def test_album_axis_workflow_statuses_cardinality() -> None:
    assert len(ALBUM_AXIS.workflow_statuses) == 5


def test_no_match_in_album_workflow_statuses() -> None:
    assert "no_match" in ALBUM_AXIS.workflow_statuses


# ---------------------------------------------------------------------------
# Album decision_blocks: manual sticky; no_match stale on either identity field
# ---------------------------------------------------------------------------


def test_album_manual_blocks_regardless_of_identity() -> None:
    decision = StatusRow(status="manual", source_primary="Old", source_secondary="Old")
    assert ALBUM_AXIS.decision_blocks(decision, Identity(primary="New", secondary="New")) is True


def test_album_no_match_blocks_when_identity_unchanged() -> None:
    decision = StatusRow(
        status="no_match",
        source_primary="Black Sabbath",
        source_secondary="Paranoid",
    )
    identity = Identity(primary="Black Sabbath", secondary="Paranoid")
    assert ALBUM_AXIS.decision_blocks(decision, identity) is True


def test_album_no_match_stale_when_primary_changed() -> None:
    # Artist (or album-artist fallback) changed → stale → does NOT block.
    decision = StatusRow(status="no_match", source_primary="Old Artist", source_secondary="Album")
    identity = Identity(primary="New Artist", secondary="Album")
    assert ALBUM_AXIS.decision_blocks(decision, identity) is False


def test_album_no_match_stale_when_secondary_changed() -> None:
    # Album changed → stale → does NOT block.
    decision = StatusRow(status="no_match", source_primary="Artist", source_secondary="Old Album")
    identity = Identity(primary="Artist", secondary="New Album")
    assert ALBUM_AXIS.decision_blocks(decision, identity) is False


def test_album_pending_does_not_block() -> None:
    decision = StatusRow(status="pending", source_primary="A", source_secondary="B")
    assert ALBUM_AXIS.decision_blocks(decision, Identity(primary="A", secondary="B")) is False


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
# Mismatch decision_blocks: blocks iff the snapshotted value still matches its field
# ---------------------------------------------------------------------------
# source_primary = the FIELD NAME ('albumartist'|'artist'); source_secondary = its snapshot.
# identity.primary = current first albumartist; identity.secondary = current first artist.


def test_mismatch_albumartist_blocks_when_value_unchanged() -> None:
    decision = StatusRow(
        status="legit_ignore", source_primary="albumartist", source_secondary="Jem"
    )
    identity = Identity(primary="Jem", secondary="Ozzy Osbourne")
    assert MISMATCH_AXIS.decision_blocks(decision, identity) is True


def test_mismatch_albumartist_stale_when_value_changed() -> None:
    decision = StatusRow(
        status="legit_ignore", source_primary="albumartist", source_secondary="Jem"
    )
    identity = Identity(primary="Ozzy Osbourne", secondary="Ozzy Osbourne")
    assert MISMATCH_AXIS.decision_blocks(decision, identity) is False


def test_mismatch_albumartist_stale_when_tag_removed() -> None:
    # The albumartist tag was cleared (current is None) -> the snapshot no longer matches.
    decision = StatusRow(
        status="misfiled_deferred", source_primary="albumartist", source_secondary="Jem"
    )
    identity = Identity(primary=None, secondary="Ozzy Osbourne")
    assert MISMATCH_AXIS.decision_blocks(decision, identity) is False


def test_mismatch_artist_field_compares_against_secondary() -> None:
    decision = StatusRow(status="misfiled_deferred", source_primary="artist", source_secondary="X")
    # Its own field (artist) is compared against identity.secondary, not primary.
    assert MISMATCH_AXIS.decision_blocks(decision, Identity(primary="Y", secondary="X")) is True
    assert MISMATCH_AXIS.decision_blocks(decision, Identity(primary="X", secondary="Z")) is False


def test_mismatch_misfiled_deferred_follows_the_same_rule() -> None:
    fresh = StatusRow(
        status="misfiled_deferred", source_primary="albumartist", source_secondary="Q"
    )
    assert MISMATCH_AXIS.decision_blocks(fresh, Identity(primary="Q", secondary=None)) is True
    assert MISMATCH_AXIS.decision_blocks(fresh, Identity(primary="R", secondary=None)) is False


def test_mismatch_null_source_field_blocks_only_when_snapshot_none() -> None:
    # A file that had neither tag: field None, value None -> compares None == None -> blocks.
    both_none = StatusRow(status="legit_ignore", source_primary=None, source_secondary=None)
    assert MISMATCH_AXIS.decision_blocks(both_none, Identity(primary=None, secondary=None)) is True
    assert MISMATCH_AXIS.decision_blocks(both_none, Identity(primary="A", secondary="B")) is True


def test_mismatch_none_value_on_albumartist_field() -> None:
    # source_field set but snapshot None: blocks only while current is also None.
    decision = StatusRow(status="legit_ignore", source_primary="albumartist", source_secondary=None)
    assert MISMATCH_AXIS.decision_blocks(decision, Identity(primary=None, secondary="X")) is True
    assert MISMATCH_AXIS.decision_blocks(decision, Identity(primary="V", secondary="X")) is False
