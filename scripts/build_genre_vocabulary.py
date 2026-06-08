"""Regenerate ``src/tagmend/data/genre_vocabulary.yml`` from the MusicBrainz dump.

This is a **maintenance script**, not part of the runtime tagging path. Most users
never run it; it exists so the bundled genre vocabulary can be refreshed when
MusicBrainz adds genres or aliases.

Why the dump and not the API: the MusicBrainz WS2 API exposes genre *names* but **not**
genre *aliases* (``inc=aliases`` is silently ignored for genres). The aliases — the
Last.fm-style spelling variants we need to match (``rnb`` -> ``r&b``,
``rhythm and blues`` -> ``r&b``) — live only on the website and in the database dump.

The "PostgreSQL database dump" is not a Postgres file: it is a ``.tar.bz2`` of plain
tab-separated table exports (Postgres ``COPY`` format). We never need a database. The
``genre`` and ``genre_alias`` tables are tiny and sort early in the (alphabetical)
archive — before the multi-GB ``recording``/``release``/``track`` tables — so we
**stream** the archive and stop the download as soon as both tables are read. That pulls
a few hundred MB instead of the full ~6.8 GB, and nothing is written to disk.

Output format (see ``docs/genre-tagging-spec.md`` §4)::

    version: 1
    source: musicbrainz
    source_dump: 20260606-002104
    genres:
      - name: r&b                 # canonical spelling written to files (MB name, lowercase)
        mbid: 31be54b2-...
        aliases: [rnb, rhythm and blues, rhythm & blues]

Aliases are deduplicated by *fold-key* (lowercase, strip non-alphanumerics): an alias is
kept only if it folds to a key the genre name does not already cover, and only one
readable representative is kept per fold-key. Cross-genre fold collisions are reported;
genre-name-vs-genre-name collisions abort the build.

Usage::

    python scripts/build_genre_vocabulary.py                 # stream the latest dump
    python scripts/build_genre_vocabulary.py --dump-file mbdump.tar.bz2   # use a local copy
    python scripts/build_genre_vocabulary.py --date 20260606-002104       # pin a dump
"""

from __future__ import annotations

import argparse
import re
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING

import httpx
import yaml

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# ── Constants ────────────────────────────────────────────────────────────────────────
DEFAULT_MIRROR = "https://data.musicbrainz.org/pub/musicbrainz/data/fullexport"
DEFAULT_USER_AGENT = "tagmend-vocab-builder/0.1 (https://github.com/blakeem/music-tag-mender)"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1] / "src" / "tagmend" / "data" / "genre_vocabulary.yml"
)

# Column positions in the Postgres COPY exports (from admin/sql/CreateTables.sql).
GENRE_COL_GID = 1
GENRE_COL_NAME = 2
GENRE_ALIAS_COL_GENRE_ID = 1
GENRE_ALIAS_COL_NAME = 2

_PROGRESS_STEP = 50 * 1024 * 1024  # log every ~50 MB streamed
_FOLD_RE = re.compile(r"[^a-z0-9]+")


# ── Domain types ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class GenreRow:
    """One row of the MusicBrainz ``genre`` table (only the fields we keep)."""

    mbid: str
    name: str


@dataclass(slots=True)
class VocabularyEntry:
    """A genre plus its kept (deduplicated, readable) aliases."""

    name: str
    mbid: str
    aliases: list[str] = field(default_factory=list)


# ── Pure helpers (Process) ───────────────────────────────────────────────────────────
def fold(value: str) -> str:
    """Reduce a genre string to its match-key: lowercase, drop all non-alphanumerics.

    ``"Synth-Pop"``, ``"synth pop"`` and ``"synthpop"`` all fold to ``"synthpop"``.
    See ``docs/genre-tagging-spec.md`` §3.
    """
    return _FOLD_RE.sub("", value.lower())


def _unescape(field_value: str) -> str | None:
    r"""Decode one Postgres COPY field: ``\N`` -> None, plus the standard escapes."""
    if field_value == r"\N":
        return None
    return (
        field_value.replace(r"\t", "\t")
        .replace(r"\n", "\n")
        .replace(r"\r", "\r")
        .replace(r"\\", "\\")
    )


def _iter_rows(tsv: bytes) -> Iterator[list[str | None]]:
    """Yield each non-empty line of a COPY export as a list of decoded fields."""
    text = tsv.decode("utf-8")
    for line in text.split("\n"):
        if line:
            yield [_unescape(cell) for cell in line.split("\t")]


def parse_genres(tsv: bytes) -> dict[str, GenreRow]:
    """Build ``genre.id -> GenreRow`` from the ``genre`` table export."""
    genres: dict[str, GenreRow] = {}
    for row in _iter_rows(tsv):
        genre_id, gid, name = row[0], row[GENRE_COL_GID], row[GENRE_COL_NAME]
        if genre_id and gid and name:
            genres[genre_id] = GenreRow(mbid=gid, name=name)
    return genres


def parse_aliases(tsv: bytes) -> dict[str, list[str]]:
    """Build ``genre.id -> [alias name, ...]`` from the ``genre_alias`` table export."""
    aliases: dict[str, list[str]] = {}
    for row in _iter_rows(tsv):
        genre_id = row[GENRE_ALIAS_COL_GENRE_ID]
        name = row[GENRE_ALIAS_COL_NAME]
        if genre_id and name:
            aliases.setdefault(genre_id, []).append(name)
    return aliases


def _separator_count(name: str) -> int:
    """Count non-alphanumeric characters — a readability proxy for genre names."""
    return sum(not char.isalnum() for char in name)


def _pick_canonical(rows: list[GenreRow]) -> GenreRow:
    """Choose the canonical spelling among genres that share a name fold-key.

    Real MusicBrainz data carries near-duplicate genres like ``hyper techno`` and
    ``hypertechno`` as separate entities. We standardize on the most readable form:
    most separators first (``hyper techno`` over ``hypertechno``), then shortest, then
    alphabetical — fully deterministic so refreshes are stable.
    """
    return min(rows, key=lambda row: (-_separator_count(row.name), len(row.name), row.name))


def _collect_aliases(
    name: str,
    ids: list[str],
    aliases: dict[str, list[str]],
    owner_of_fold: dict[str, str],
    warnings: list[str],
) -> list[str]:
    """Gather the kept aliases for one genre across every merged source id.

    Skips aliases that are redundant (fold to a key already covered for this genre) or
    that collide with another genre's fold-key (reported, not silently dropped).
    """
    kept: list[str] = []
    for source_id in ids:
        for alias in aliases.get(source_id, []):
            alias_key = fold(alias)
            if not alias_key:
                continue
            owner = owner_of_fold.get(alias_key)
            if owner is not None:
                if owner != name:
                    warnings.append(
                        f"alias {alias!r} of {name!r} folds to {alias_key!r}, "
                        f"already owned by {owner!r} — skipped",
                    )
                continue  # already covered by this genre's name or an earlier alias
            owner_of_fold[alias_key] = name
            kept.append(alias)
    return kept


def build_vocabulary(
    genres: dict[str, GenreRow],
    aliases: dict[str, list[str]],
) -> tuple[list[VocabularyEntry], list[str]]:
    """Merge genres + aliases into vocabulary entries, deduping everything by fold-key.

    Applies the spec rules (``docs/genre-tagging-spec.md`` §4.2/§4.4):

    * Genres whose *names* fold to the same key are merged to one canonical spelling
      (their aliases pooled), and the merge is reported — not fatal, because MusicBrainz
      legitimately contains such near-duplicate names.
    * An alias is kept only if it folds to a key no genre already owns (via name or an
      earlier alias). Cross-genre alias collisions are reported and skipped.

    Returns the entries (sorted by name) and human-readable collision/merge warnings.
    """
    # Group genre ids by their name's fold-key so duplicate spellings collapse together.
    groups: dict[str, list[str]] = {}
    for genre_id, genre in genres.items():
        groups.setdefault(fold(genre.name), []).append(genre_id)

    # Names claim their fold-keys first (names always win over aliases).
    warnings: list[str] = []
    owner_of_fold: dict[str, str] = {}
    canonical: dict[str, list[str]] = {}  # canonical name -> all source ids merged into it
    for key, ids in groups.items():
        winner = _pick_canonical([genres[i] for i in ids])
        owner_of_fold[key] = winner.name
        canonical[winner.name] = ids
        if len(ids) > 1:
            spellings = sorted(genres[i].name for i in ids)
            warnings.append(
                f"genre names {spellings!r} share fold-key {key!r}; keeping {winner.name!r}",
            )

    # Attach deduped aliases, then emit entries sorted by canonical name.
    entries: list[VocabularyEntry] = []
    for name in sorted(canonical):
        ids = canonical[name]
        mbid = _pick_canonical([genres[i] for i in ids]).mbid
        kept = _collect_aliases(name, ids, aliases, owner_of_fold, warnings)
        entries.append(VocabularyEntry(name=name, mbid=mbid, aliases=kept))
    return entries, warnings


# ── I/O: streaming the dump (Input) ──────────────────────────────────────────────────
class _ChunkReader:
    """Adapt an iterator of byte chunks into a non-seekable ``read(n)`` file object.

    :mod:`tarfile` in streaming (``r|bz2``) mode only calls ``read``; we buffer chunks
    from the HTTP response and hand back exactly what is asked for, counting bytes so we
    can report download progress.
    """

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks = iter(chunks)
        self._buffer = bytearray()
        self._exhausted = False
        self.bytes_read = 0
        self._next_progress = _PROGRESS_STEP

    def read(self, size: int = -1) -> bytes:
        """Return up to *size* bytes (all remaining if *size* < 0)."""
        if size < 0:
            for chunk in self._chunks:
                self._buffer += chunk
            self._exhausted = True
        else:
            while len(self._buffer) < size and not self._exhausted:
                try:
                    chunk = next(self._chunks)
                except StopIteration:
                    self._exhausted = True
                    break
                self._buffer += chunk
                self.bytes_read += len(chunk)
                if self.bytes_read >= self._next_progress:
                    _log(f"  …streamed {self.bytes_read // (1024 * 1024)} MB")
                    self._next_progress += _PROGRESS_STEP
        taken = bytes(self._buffer[:size]) if size >= 0 else bytes(self._buffer)
        del self._buffer[: len(taken)]
        return taken


def _extract_two_tables(tar: tarfile.TarFile) -> tuple[bytes, bytes]:
    """Iterate a (streaming) tar, returning the ``genre`` and ``genre_alias`` exports.

    Stops as soon as both members are read so the caller can abandon the rest of the
    download. Raises :class:`LookupError` if the archive ends without both tables.
    """
    genre_tsv: bytes | None = None
    alias_tsv: bytes | None = None
    for member in tar:
        if member.name.endswith("/genre"):
            genre_tsv = _read_member(tar, member)
            _log("  found mbdump/genre")
        elif member.name.endswith("/genre_alias"):
            alias_tsv = _read_member(tar, member)
            _log("  found mbdump/genre_alias")
        if genre_tsv is not None and alias_tsv is not None:
            return genre_tsv, alias_tsv
    message = "archive ended before both 'genre' and 'genre_alias' were found"
    raise LookupError(message)


def _read_member(tar: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    """Read a tar member's bytes, guarding against a missing stream."""
    stream: IO[bytes] | None = tar.extractfile(member)
    if stream is None:
        message = f"could not read member {member.name!r} from archive"
        raise LookupError(message)
    return stream.read()


def load_tables_from_url(url: str, user_agent: str) -> tuple[bytes, bytes]:
    """Stream a remote ``mbdump.tar.bz2`` and return the two genre tables."""
    _log(f"streaming {url}")
    headers = {"User-Agent": user_agent}
    timeout = httpx.Timeout(60.0, read=120.0)
    with (
        httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        reader = _ChunkReader(response.iter_bytes(chunk_size=1024 * 1024))
        # _ChunkReader is read-only by design; streaming "r|bz2" never seeks, but
        # typeshed's _Fileobj protocol demands the full file API, so the overload check
        # cannot see the match.
        with tarfile.open(fileobj=reader, mode="r|bz2") as tar:  # type: ignore[call-overload]
            genre_tsv, alias_tsv = _extract_two_tables(tar)
    _log(f"done streaming after {reader.bytes_read // (1024 * 1024)} MB (full dump is ~6.8 GB)")
    return genre_tsv, alias_tsv


def load_tables_from_file(path: Path) -> tuple[bytes, bytes]:
    """Read the two genre tables from a local ``mbdump.tar.bz2``."""
    _log(f"reading local dump {path}")
    with tarfile.open(path, mode="r:bz2") as tar:
        return _extract_two_tables(tar)


def resolve_dump_url(mirror: str, date: str | None, user_agent: str) -> tuple[str, str]:
    """Return ``(tarball_url, dump_id)`` for *date* or the published LATEST dump."""
    if date is None:
        resp = httpx.get(f"{mirror}/LATEST", headers={"User-Agent": user_agent}, timeout=30.0)
        resp.raise_for_status()
        date = resp.text.strip()
    return f"{mirror}/{date}/mbdump.tar.bz2", date


# ── I/O: writing the vocabulary (Output) ─────────────────────────────────────────────
def write_vocabulary(entries: list[VocabularyEntry], dump_id: str, output: Path) -> None:
    """Serialize *entries* to the vocabulary YAML at *output*."""
    document = {
        "version": 1,
        "source": "musicbrainz",
        "source_dump": dump_id,
        "genres": [{"name": e.name, "mbid": e.mbid, "aliases": e.aliases} for e in entries],
    }
    header = (
        "# TagMend genre vocabulary — GENERATED, do not hand-edit names.\n"
        "# Regenerate with: python scripts/build_genre_vocabulary.py\n"
        "# Source: MusicBrainz genre + genre_alias tables (CC0). See docs/genre-tagging-spec.md.\n"
        "# `name` is the canonical spelling written to files; `aliases` are extra spellings\n"
        "# (deduped by fold-key) that should match the same genre.\n"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(header)
        yaml.safe_dump(
            document,
            handle,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=None,
            width=120,
        )


# ── Orchestration (IPO top level) ────────────────────────────────────────────────────
def _log(message: str) -> None:
    """Write progress to stderr (stdout is reserved for nothing here, but be tidy)."""
    print(message, file=sys.stderr)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dump-file",
        type=Path,
        default=None,
        help="use a local mbdump.tar.bz2 instead of downloading",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="dump directory id (default: the published LATEST)",
    )
    parser.add_argument("--mirror", default=DEFAULT_MIRROR, help="dump mirror base URL")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="vocabulary file to write",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="HTTP User-Agent (MusicBrainz requires a real one)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Build the vocabulary; return a process exit code."""
    # Input
    args = _parse_args(argv)
    if args.dump_file is not None:
        genre_tsv, alias_tsv = load_tables_from_file(args.dump_file)
        dump_id = "local"
    else:
        url, dump_id = resolve_dump_url(args.mirror, args.date, args.user_agent)
        genre_tsv, alias_tsv = load_tables_from_url(url, args.user_agent)

    # Process
    genres = parse_genres(genre_tsv)
    aliases = parse_aliases(alias_tsv)
    _log(f"parsed {len(genres)} genres, {sum(len(v) for v in aliases.values())} raw aliases")
    entries, warnings = build_vocabulary(genres, aliases)
    kept_aliases = sum(len(e.aliases) for e in entries)
    _log(f"kept {kept_aliases} aliases after fold-dedup; {len(warnings)} collision warning(s)")
    for warning in warnings:
        _log(f"  collision: {warning}")

    # Output
    write_vocabulary(entries, dump_id, args.output)
    _log(f"wrote {len(entries)} genres to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
