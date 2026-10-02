# TagMend Roadmap

Updated 2026-10-01. This file lists only the remaining work, in order. `CLAUDE.md` maps what
shipped. `PLAN.md` holds the design.

The live run on the 11,233-file working copy (`music/`) is done. It covered the mismatch pass
(B0), the album-gap fills (B1), the resolver run over all four tag axes (B2) and the path phase.

## 1. Owner questions

The owner decides each one. TagMend never deletes a file.

- Duplicates. Pendulum "(2010) - Immersion" holds every track twice (files 6845 to 6859 and
  6860 to 6874). The remaining copy then takes a stamp onto release a8515645. Imperative
  Reaction "(1999)" and "(2006) Eulogy For The Sick Child" are the same 14 files. KMFDM 4695 is
  a second "Love Is Like". VAST 10459 is a second "You". Velvet Acid Christ 10489 holds
  the audio of Apoptygma Berzerk 437. Killswitch Engage 4637 holds the audio of 4650. Blue
  Stahli 1268 holds the audio of Celldweller 1637. Neon Hitch holds three single pairs (6134
  and 6144, 6137 and 6142, 6138 and 6143). The path planner holds the Pendulum, Imperative
  Reaction, KMFDM, VAST and Velvet Acid Christ albums until the owner decides.
- Cue sheets. 86 files sit in folders whose cue sheet names the audio files, so a rename would
  break the cue. The folders are Beck "Morning Phase", Code 64 "Departure" and "Trialogue",
  Taylor Swift "1989" and "Best Of", and The Postal Service "2 Give Up_".
- Long paths. 31 files render to a path over 259 characters. They are The Crow "City of
  Angels", Hackers 2 and 3, one My Chemical Romance track and the two Tool root files.
- Tool root files. Files 10230 "Them Bones" and 10231 "Man in the Box" sit in "Tool
  [Discography]" but are credited to Alice in Chains feat. Maynard James Keenan (a 2005 live
  concert).
- RVAD and NCON frames. 11 MP3s carry frames the ID3v2.4 save drops. 3 Skrillex "The SkrilleX
  Originals" files carry the v2.3-only RVAD frame. 8 Tool "Salival" files carry the non-standard
  NCON frame. Staging refuses them, so their genres and the "Salival" stamp stay pending.
  Writing them needs the owner's opt-in to drop those frames.
- Folder casing. 1,126 files sit in folders whose name differs from the render only in case,
  such as "ASURA" for the tag "Asura". TagMend keeps an existing folder's casing. A fix would
  rename through a temporary name, tracked like any move.
- Leftover folders. The disc joins left folders that hold only non-audio files, mostly the
  disc-2 cover art of joined disc sets. They stay until the owner removes them.
- Damaged audio. fpcalc cannot decode files 436 and 451 (Apoptygma Berzerk "Kathy's Song"),
  6429 (Nine Inch Nails "28 Ghosts IV") and 9579 (The Faint "Worked Up So Sexual").
- Korn file 4815 "When Will This End" is off its tagged release, likely the hidden-track
  version.
- White Stripes file 10082 (Roskilde) is off its tagged release. It is a bootleg with no
  candidate release.
- The Faint file 9626 moved to "Danse Macabre Remixes" as 11/11 "Let the Poison Spill
  (Remix)", with low confidence. Its 252 s audio matches no MusicBrainz recording. It needs a
  listening check.
- Calyx file 1430 "Follow the Leader" is 282 s against the release's 385 s. It may be an edit or
  a truncated rip.
- LCD Soundsystem "45_33 Remixes": 7 of 8 lengths disagree with the tagged tracks. It needs a
  listening check.
- LCD Soundsystem "All My Friends" files 4973 and 4974 may have crossed bindings. Their titles
  swap against the tracks their release-track ids name. File 4973 "All My Friends" sits on the
  album-version track. File 4974 "John Cale's version" sits on the plain track. They need a
  listening check.
- Rammstein file 7398 carries the title "Bückstabü" in place of the release's censored
  "B********", which would render the filename "B". The owner may revert it.
- Neikka RPM "(2011) Chain Letters" carries the Last.fm genre "psytrance" on 22 files. A
  `deny:` rule in the genre overlay keeps a wrong genre off those files in the final run.
- Curated folders such as Blue Stahli "Singles", Maphra "YouTube" and Scott Weiland "MP3" hold
  mixed `date` values. Navidrome shows no album year when the tracks disagree.
- Wrong credits. Stone Temple Pilots file 8722 "Cumbersome" is Seven Mary Three's song. Its
  blank album holds it in the path planner. Tool "Unreleased" file 10285 is Joe Satriani's "Drum
  Solo". File 10283 may be Pink Floyd's "Comfortably Numb" demo.
- Extras off their release. Rank 1 file 7417 is an unidentified second "Cosmomatic". Skrillex "My
  Name Is Skrillex" holds 6 extras that carry the EP id but sit on no release. Neon Hitch file 6097
  carries the "301 to Paradise" release id, which does not list it. VNV Nation file 10631 is the
  single's "Darkangel (Gabriel)" in the album's track 9 slot.
- White Stripes file 10101 "Rayed X" is probably "Rated X".
- Held names. "Miley Cyrus" (16 files, plus the `artists` list of file 2873) is held as `manual`,
  since MusicBrainz now credits "MILEY". File 4242 is held as `manual` with `artist` "J.Views"
  and `artists` "J.Viewz".
- Kept credits. ATOI, Fats, Penny, A. Hartung, Beatniks, Wumpscut and 8 collaboration credits stay
  as tagged. The owner may override any of them.

## 2. Final run on a fresh copy

- Copy `E:\Music` to `music/` again and start a fresh ledger.
- Copy the Last.fm, MusicBrainz and `acoustid_cache` tables from the current ledger, so that no
  answer is fetched twice. `fingerprint_cache` rows are keyed by file id and by the file's size
  and mtime. A row carries over only through a mapping from its file's original path, re-keyed
  to the fresh copy's signature.
- Replay the recorded decisions by path. These are the mismatch decisions, the manual stamps,
  the manual tag fixes and the `manual` statuses, plus the owner's answers to the questions
  above.
- Run the resolvers, the gate and the path phase again. Then verify every path and every audio
  payload against `E:\Music`.
- Check that no `sidecar_moves` row moves a release folder's own art after an earlier commit
  moved one of its discs elsewhere.

## 3. Copy promotion (B3)

The mended copy replaces the real library after the final run is verified. This is an owner
action, not code.

## 4. CLI

The CLI surface stays last. A tool gets a command only when it is worth running by hand. The
command mirrors its MCP name with `-` for `_`.

## 5. Packaging and polish

- Publish to PyPI, so `uv tool install tagmend` and `pipx install tagmend` work as the README
  describes.
- Genre vocabulary tuning and an album-level genre override (PLAN.md M5).

## 6. Open latent findings

- L4: an ID3v2.3 file can hold several MusicBrainz ids joined in one frame. No normalizer splits
  them yet. The live library holds none.
- L7: the disc total counts a DVD-Video medium of a CD+DVD release. This is a design question.
- L18: a `commit_tags` whose rows are all no-ops still writes an empty `applied` commit row.
- L6: a weak AcoustID recording still claims a release track, so the manual release path can
  refuse a clean folder as `ambiguous_slot` or `slot_collision`. `assignments` work around it.
  No source floor exists yet.
- F11: a lyrics sidecar (`.lrc`, same stem) moves under its own name, so a renamed audio file
  loses its lyrics link. The live library holds none.
