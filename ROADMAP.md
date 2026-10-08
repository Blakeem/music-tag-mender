# TagMend Roadmap

Updated 2026-10-07. This file lists only the remaining work, in order. `CLAUDE.md` maps what
shipped. `PLAN.md` holds the design.

The final run on a fresh copy of `E:\Music` is done. The working copy (`music/`) holds 11,104 files
after the owner's cleanup removed one-off files, duplicates, albums the owner does not play, a
second Taylor Swift album, a second rip of The Postal Service "Give Up", every LCD Soundsystem
album but "LCD Soundsystem" and the `.m4a` copies of three Neon Hitch singles. The owner added one
MAPHRA track, Carpenter Brut "Leather Terror" and "Blood Machines", Assemblage 23 "Endure",
Digitalism "Optimism" and Sync24 "Omnious" and "Source". Every audio payload that came from
`E:\Music` still matches it, and the mismatch gate is open. The file ids below name files in the
fresh ledger. A file the owner moved by hand has a new id.

## 1. Owner questions

The owner decides each one. TagMend never deletes a file.

- Duplicates. Blue Stahli 1268 holds the audio of Celldweller 1637. The owner keeps both for now.
- Long path. One My Chemical Romance track in "I Brought You My Bullets, You Brought Me Your
  Love" renders to 263 characters, over the limit of 259. Under `E:\Music` it renders to 246.
- Folder casing. 1,126 files sit in folders whose name differs from the render only in case,
  such as "ASURA" for the tag "Asura". TagMend keeps an existing folder's casing. A fix would
  rename through a temporary name, tracked like any move.
- Leftover folders. 26 folders hold no audio. 8 hold only hidden Windows Media Player images. 5
  hold only rip leftovers such as `.nfo` and `.sfv` files. 13 are scan or booklet folders inside
  an album folder. They stay until the owner removes them.
- Korn file 4852 "When Will This End" is off its tagged release, likely the hidden-track
  version.
- White Stripes file 10119 (Roskilde) is off its tagged release. It is a bootleg with no
  candidate release.
- Rammstein file 7435 carries the title "Bückstabü" in place of the release's censored
  "B********", which would render the filename "B". The owner may revert it.
- Curated folders such as Blue Stahli "Singles" and Maphra "YouTube" hold mixed `date` values.
  Navidrome shows no album year when the tracks disagree.
- Extras off their release. Skrillex "My Name Is Skrillex" holds 6 extras that carry the EP id
  but sit on no release. Neon Hitch file 6134 carries the "301 to Paradise" release id, which
  does not list it.
- Held names. "Miley Cyrus" (16 files, plus the `artists` list of file 2873) is held as `manual`,
  since MusicBrainz now credits "MILEY".
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
- L25: the reader does not read Vorbis `TRACKTOTAL` or `DISCTOTAL`, so the song convergence
  route sees a FLAC's totals only when `tracknumber` itself holds `n/N`. A release stamp also
  leaves those fields as they were. The live library holds 10 FLACs with no release id whose
  totals live only there, and all 10 are settled. 47 FLACs in 6 folders hold a `TRACKTOTAL` that
  differs from their tagged release, such as The Postal Service "Give Up" with 10 and 15 against
  12.
- L26: `resolve_songs` ends its summary with "Re-run to retry" for every errored file. A stored
  fpcalc failure and a row with no signature are not retried by a re-run. So the two live files
  fpcalc cannot decode (6466 and 9616) get that advice on every call.
- L27: fpcalc 1.6.1 stops at the first corrupt frame near the start of an MP3, even with
  `-ignore-errors`. Files 6466 (Nine Inch Nails "28 Ghosts IV") and 9616 (The Faint "Worked Up
  So Sexual") play, and ffmpeg decodes them in full (321.6 s and 159.7 s), but fpcalc returns no
  fingerprint. fpcalc fingerprints the audio ffmpeg decodes. The fix candidate decodes through
  ffmpeg when fpcalc fails.
- L24: a release stamp clears a file's ISRC that MusicBrainz does not list for its track, unless
  the file already carries the stamped recording's id. So a file with no MusicBrainz ids loses its
  label ISRC. Stamps in commits 3 to 168 cleared 125 well-formed ISRCs. Some of those files moved
  to a different recording, where the clear is right. Commit 401 restored the 11 that commit 399
  cleared from Digitalism "Optimism". The fix candidate keeps a well-formed ISRC on a file that
  carried no recording id and was placed by audio.
