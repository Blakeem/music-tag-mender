Source: https://musicbrainz.org/doc/Recording
Transcluded from wiki.musicbrainz.org/Recording, revision #76395
Version: ws/2 (current production version) — describes the recording entity's data model, i.e. the
fields returned by the API for a recording (title, artist, length, ISRC). Not an API-mechanics page.
Retrieved: 2026-07-07

---

## Contents

-   [1 Examples](#Examples)
-   [2 Style Guidelines](#Style_Guidelines)
-   [3 Properties](#Properties)
    -   [3.1 Title](#Title)
    -   [3.2 Artist](#Artist)
    -   [3.3 Length](#Length)
    -   [3.4 ISRC](#ISRC)
    -   [3.5 MBID](#MBID)
    -   [3.6 Disambiguation comment](#Disambiguation_comment)
    -   [3.7 Annotation](#Annotation)

A recording is an entity in MusicBrainz which can be linked to [tracks](https://musicbrainz.org/doc/Track) on [releases](https://musicbrainz.org/doc/Release). Each track must always be associated with a single recording, but a recording can be linked to any number of tracks.

A recording represents distinct audio that has been used to produce at least one released track through copying or [mastering](https://musicbrainz.org/doc/Mix_Terminology#mastering). A recording itself is never produced solely through copying or mastering.

Generally, the audio represented by a recording corresponds to the audio at a stage in the production process before any final mastering but after any editing or [mixing](https://musicbrainz.org/doc/Mix_Terminology#mixing).

## Examples

These are all different Recordings:

-   Studio recording: [Into the Blue](https://musicbrainz.org/recording/9ce76fe1-769f-481a-afbb-3b9b81c6f433) by [Moby](https://musicbrainz.org/artist/8970d868-0723-483b-a75b-51088913d3d4)
-   Remixed recording: [Into the Blue (Beatmasters mix)](https://musicbrainz.org/recording/023b1e14-0e7e-4dc1-9ab4-9ef4f0e70ce0) by [Moby](https://musicbrainz.org/artist/8970d868-0723-483b-a75b-51088913d3d4)

-   Studio recording: [Voulez-Vous](https://musicbrainz.org/recording/f449e449-503b-4ca4-967b-4aff18bc218e) by [ABBA](https://musicbrainz.org/artist/d87e52c5-bb8d-4da8-b941-9f4928627dc8)
-   Live recording: [Voulez-Vous](https://musicbrainz.org/recording/d97813dc-a5fb-4ddb-b22e-7990cd253aae) by [ABBA](https://musicbrainz.org/artist/d87e52c5-bb8d-4da8-b941-9f4928627dc8)

## Style Guidelines

Please see the [guidelines for recordings](https://musicbrainz.org/doc/Style/Recording).

## Properties

### Title

The title of the recording.

### Artist

The artist(s) that the recording is primarily credited to.

### Length

The length of the recording. It's only entered manually for [standalone recordings](https://musicbrainz.org/doc/Standalone_Recording). For recordings that are being used on releases, the recording length is the median length of all tracks (that have a track length) associated with that recording. If there is an even number of track lengths, the smaller median candidate is used.

### ISRC

The [International Standard Recording Code](https://musicbrainz.org/doc/ISRC) assigned to the recording.

### MBID

See [MusicBrainz Identifier](https://musicbrainz.org/doc/MusicBrainz_Identifier).

### Disambiguation comment

See [Disambiguation Comment](https://musicbrainz.org/doc/Disambiguation_Comment).

### Annotation

See [Annotation](https://musicbrainz.org/doc/Annotation).
