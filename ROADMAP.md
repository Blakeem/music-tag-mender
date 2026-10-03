# TagMend Roadmap

Updated 2026-10-02. This file lists only the remaining work, in order. `CLAUDE.md` maps what
shipped. `PLAN.md` holds the design.

The final run on a fresh copy of `E:\Music` is done. The working copy (`music/`) holds 11,148 files
after the owner's cleanup removed one-off files, duplicates, albums the owner does not play, a
second Taylor Swift album and a second rip of The Postal Service "Give Up". The owner added one
MAPHRA track, Carpenter Brut "Leather Terror" and Assemblage 23 "Endure". Every audio payload
that came from `E:\Music` still matches it, and the mismatch gate is open. The file ids below
name files in the fresh ledger. A file the owner moved by hand has a new id.

## 1. Owner questions

The owner decides each one. TagMend never deletes a file.

- Duplicates. Pendulum "(2010) - Immersion" holds every track twice (files 6882 to 6896 and
  6897 to 6911). The remaining copy then takes a stamp onto release a8515645. Imperative
  Reaction "(1999)" and "(2006) Eulogy For The Sick Child" are the same 14 files. KMFDM 4732 is
  a second "Love Is Like". Velvet Acid Christ 10526 holds the audio of Apoptygma Berzerk 437.
  Killswitch Engage 4674 holds the audio of 4687. Blue Stahli 1268 holds the audio of
  Celldweller 1637. Neon Hitch holds three single pairs (6171 and 6181, 6174 and 6179, 6175 and
  6180). The path planner holds the Pendulum, Imperative Reaction, KMFDM and Velvet Acid Christ
  albums until the owner decides.
- Covers. 13 albums show no cover after commit 380. Weezer "Raditude" holds two images that may be
  the front. The owner kept the concert cover off "Tool\Other". No source exists for Gary Numan
  "Replicas" and "The Fury", team sleep, Leaf Yard, Roberto Paci Dalò "Sparks", Solitary
  Experiments "The Great Illusion", The White Stripes "BBC Radio 1 John Peel Show", Tool
  "Unreleased" and the FiXT sampler. `stage_covers(folder=..., image=...)` stages an image the
  owner chooses. Blue Stahli "Blue Stahli Corner Competition" and Sybreed "Doomsday Party" share
  "The Luna Sequence\Other" with other albums, so a folder image there would cover every album in
  it.
- Long paths. 29 files render to a path over 259 characters. They are The Crow "City of
  Angels", Hackers 2 and 3, and one My Chemical Romance track.
- Folder casing. 1,126 files sit in folders whose name differs from the render only in case,
  such as "ASURA" for the tag "Asura". TagMend keeps an existing folder's casing. A fix would
  rename through a temporary name, tracked like any move.
- Leftover folders. 26 folders hold no audio. 8 hold only hidden Windows Media Player images. 5
  hold only rip leftovers such as `.nfo` and `.sfv` files. 13 are scan or booklet folders inside
  an album folder. They stay until the owner removes them.
- Damaged audio. fpcalc cannot decode files 436 and 451 (Apoptygma Berzerk "Kathy's Song"),
  6466 (Nine Inch Nails "28 Ghosts IV") and 9616 (The Faint "Worked Up So Sexual").
- Korn file 4852 "When Will This End" is off its tagged release, likely the hidden-track
  version.
- White Stripes file 10119 (Roskilde) is off its tagged release. It is a bootleg with no
  candidate release.
- Calyx file 1430 "Follow the Leader" is 282 s against the release's 385 s. It may be an edit or
  a truncated rip.
- LCD Soundsystem "45_33 Remixes": 7 of 8 lengths disagree with the tagged tracks. It needs a
  listening check.
- LCD Soundsystem "All My Friends" files 5010 and 5011 may have crossed bindings. Their titles
  swap against the tracks their release-track ids name. File 5010 "All My Friends" sits on the
  album-version track. File 5011 "John Cale's version" sits on the plain track. They need a
  listening check.
- Rammstein file 7435 carries the title "Bückstabü" in place of the release's censored
  "B********", which would render the filename "B". The owner may revert it.
- Neikka RPM "(2011) Chain Letters" carries the Last.fm genre "psytrance" on 22 files. A
  `deny:` rule in the genre overlay keeps a wrong genre off those files in the final run.
- Curated folders such as Blue Stahli "Singles" and Maphra "YouTube" hold mixed `date` values.
  Navidrome shows no album year when the tracks disagree.
- Wrong credits. Tool "Unreleased" file 10322 is Joe Satriani's "Drum Solo". File 10320 may be
  Pink Floyd's "Comfortably Numb" demo.
- Extras off their release. Rank 1 file 7454 is an unidentified second "Cosmomatic". Skrillex "My
  Name Is Skrillex" holds 6 extras that carry the EP id but sit on no release. Neon Hitch file 6134
  carries the "301 to Paradise" release id, which does not list it. VNV Nation file 10668 is the
  single's "Darkangel (Gabriel)" in the album's track 9 slot.
- White Stripes file 10138 "Rayed X" is probably "Rated X".
- Held names. "Miley Cyrus" (16 files, plus the `artists` list of file 2873) is held as `manual`,
  since MusicBrainz now credits "MILEY". File 4279 is held as `manual` with `artist` "J.Views"
  and `artists` "J.Viewz".
- Kept credits. ATOI, Fats, Penny, A. Hartung, Beatniks, Wumpscut and 8 collaboration credits stay
  as tagged. The owner may override any of them.

## 2. Copy promotion (B3)

The owner replaces `E:\Music` with the mended `music/` folder.

## 3. CLI

The CLI surface stays last. A tool gets a command only when it is worth running by hand. The
command mirrors its MCP name with `-` for `_`.

## 4. Packaging and polish

- Publish to PyPI, so `uv tool install tagmend` and `pipx install tagmend` work as the README
  describes.
- Genre vocabulary tuning and an album-level genre override (PLAN.md M5).

## 5. Open latent findings

- L4: an ID3v2.3 file can hold several MusicBrainz ids joined in one frame. No normalizer splits
  them yet. The live library holds none.
- L7: the disc total counts a DVD-Video medium of a CD+DVD release. This is a design question.
- L18: a `commit_tags` whose rows are all no-ops still writes an empty `applied` commit row.
- L6: a weak AcoustID recording still claims a release track, so the manual release path can
  refuse a clean folder as `ambiguous_slot` or `slot_collision`. `assignments` work around it.
  No source floor exists yet.
- F11: a lyrics sidecar (`.lrc`, same stem) moves under its own name, so a renamed audio file
  loses its lyrics link. The live library holds none.
- L19: a release stamp writes each `artists` element as the release credits it, and
  `resolve_artists` writes the artist's canonical name. The value left on disk depends on which
  ran last. The fix candidate is a stamp that writes the canonical name of each credited artist
  id.
- L21: no decision keeps a file name. The owner keeps the names of the two Alice in Chains covers
  in "Tool\Other" (files 11251 and 11252), so a library-wide `stage_paths` stages a rename the
  owner must unstage. The fix candidate is a keep that also holds the file names.
- L22: Navidrome keys an album on an MP3's `TDRL` release date, and the reader does not read
  `TDRL`. So `detect_cover_gaps` keeps MP3 tracks with different `TDRL` dates in one album where
  Navidrome splits them. The live library holds 29 such MP3s in 3 folders. Only Neon Hitch
  "Unreleased" mixes dates, and it shows a cover. The fix candidate is a raw `releasedate` read of
  `TDRL`, with a reader version bump.
- L20: TagLib reads an MP3's APEv2 tag before ID3v1, and `read_tags` does not model APEv2. The
  live library holds no MP3 this affects, since all 550 MP3s with an APEv2 tag also hold ID3v2
  frames. A write that creates the first ID3v2 frame also leaves the ID3v1 comment out of ID3v2,
  so Navidrome stops showing it.
