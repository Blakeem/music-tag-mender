Source: https://musicbrainz.org/doc/Artist
Transcluded from wiki.musicbrainz.org/Artist, revision #76977
Version: ws/2 (current production version) — describes the artist entity's data model, i.e. the
fields returned by the API for an artist (name, sort-name, type, gender, area, life-span, IPI, ISNI,
alias). Not an API-mechanics page.
Retrieved: 2026-07-07

---

An artist is generally a musician (or musician persona), group of musicians, or other music professional (like a producer or engineer). Occasionally, it can also be a non-musical person (like a photographer, an illustrator, or a poet whose writings are set to music), or even a [fictional character](https://musicbrainz.org/doc/Fictitious_Artist). For some other special cases, see [special purpose artists](https://musicbrainz.org/doc/Style/Unknown_and_untitled/Special_purpose_artist).

## Contents

-   [1 Examples](#Examples)
-   [2 Style guidelines](#Style_guidelines)
-   [3 Artist properties](#Artist_properties)
    -   [3.1 Name](#Name)
    -   [3.2 Sort name](#Sort_name)
    -   [3.3 Type](#Type)
    -   [3.4 Gender](#Gender)
    -   [3.5 Area](#Area)
    -   [3.6 Begin and end dates](#Begin_and_end_dates)
    -   [3.7 IPI code](#IPI_code)
    -   [3.8 ISNI code](#ISNI_code)
    -   [3.9 Alias](#Alias)
    -   [3.10 MBID](#MBID)
    -   [3.11 Disambiguation comment](#Disambiguation_comment)
    -   [3.12 Annotation](#Annotation)
-   [4 Additional information](#Additional_information)

## Examples

-   [Coldplay](https://musicbrainz.org/artist/cc197bad-dc9c-440d-a5b5-d52ba2e14234)
-   [Snoop Dogg](https://musicbrainz.org/artist/f90e8b26-9e52-4669-a5c9-e28529c47894)
-   [John Williams](https://musicbrainz.org/artist/53b106e7-0cc6-42cc-ac95-ed8d30a3a98e) (soundtrack composer & conductor)
-   [John Williams](https://musicbrainz.org/artist/8b8a38a9-a290-4560-84f6-3d4466e8d791) (classical guitar player)
-   [Pink Floyd](https://musicbrainz.org/artist/83d91898-7763-47d7-b03b-b92132375c47)
-   [Seattle Symphony](https://musicbrainz.org/artist/0b51c328-1f2b-464c-9e2c-0c2a8cce20ae)
-   [Bill Porter](https://musicbrainz.org/artist/86437518-fca1-4117-b698-b371b72d76a5)

## Style guidelines

Please see the [guidelines for artists](https://musicbrainz.org/doc/Style/Artist).

## Artist properties

### Name

The official name of an artist, be it a person or a band.

### Sort name

The sort name is a variant of the artist name which would be used when sorting artists by name, such as in record shops or libraries. Among other things, sort names help to ensure that all the artists that start with "The" don't end up up under "T". [The guidelines for sort names](https://musicbrainz.org/doc/Style/Artist/Sort_Name) are the best place to check for more specific usage info.

### Type

The type is used to state whether an artist is a person, a group, or something else.

<dl><dd><dl><dt>Person</dt><dd>This indicates an individual person.</dd></dl></dd></dl>
<dl><dd><dl><dt>Group</dt><dd>This indicates a group of people that may or may not have a distinctive name.</dd></dl></dd></dl>
<dl><dd><dl><dt>Orchestra</dt><dd>This indicates an orchestra (a large instrumental ensemble).</dd></dl></dd></dl>
<dl><dd><dl><dt>Choir</dt><dd>This indicates a choir/chorus (a large vocal ensemble).</dd></dl></dd></dl>
<dl><dd><dl><dt>Character</dt><dd>This indicates an individual fictional character.</dd></dl></dd></dl>
<dl><dd><dl><dt>Other</dt><dd>Anything which does not fit into the above categories.</dd></dl></dd></dl>

Note that not every ensemble related to classical music is an orchestra or choir. The [Borodin Quartet](https://musicbrainz.org/artist/598063d1-1fc6-496a-8e91-2c21c38d8c92) and [The Hilliard Ensemble](https://musicbrainz.org/artist/c8db3d2b-19d8-4dc7-b2cb-deea37aa274a), for example, are simply groups.

### Gender

The gender is used to explicitly state whether a person or character identifies as male, female or neither. Groups do not have genders.

### Area

The artist area, as the name suggests, indicates the area with which an artist is primarily identified with. It is often, but not always, its birth/formation country.

### Begin and end dates

The begin and end dates indicate when an artist started and finished its existence. Its exact meaning depends on the type of artist:

<dl><dd><dl><dt>For a person</dt><dd></dd><dd>Begin date represents date of birth, and end date represents date of death.</dd></dl></dd></dl>
<dl><dd><dl><dt>For a group (or orchestra/choir)</dt><dd></dd><dd>Begin date represents the date the group <i>first</i> formed, and the end date represents the date when the group <i>last</i> dissolved.</dd></dl></dd></dl>
<dl><dd><dl><dt>For a character</dt><dd></dd><dd>Begin date represents the date (in real life) when the character concept was created. The end date should not be set. These fields do not hold fictional birth and death dates.</dd></dl></dd></dl>
<dl><dd><dl><dt>For others</dt><dd></dd><dd>These fields are currently undefined for artists of the type Other.</dd></dl></dd></dl>

### IPI code

An IPI (interested party information) code is an identifying number assigned by the CISAC database for musical rights management. See [IPI](https://musicbrainz.org/doc/IPI) for more information, including how to find these codes.

### ISNI code

The International Standard Name Identifier for the artist. See [ISNI](https://musicbrainz.org/doc/ISNI) for more information.

### Alias

Aliases are used to store alternate names or misspellings. For more information and examples, see the [page about aliases](https://musicbrainz.org/doc/Aliases).

### MBID

See the [page about MBIDs](https://musicbrainz.org/doc/MusicBrainz_Identifier) for more information.

### Disambiguation comment

See the [page about comments](https://musicbrainz.org/doc/Disambiguation_Comment) for more information.

### Annotation

See the [page about annotations](https://musicbrainz.org/doc/Annotation) for more information.

## Additional information

-   [How to add an artist](https://musicbrainz.org/doc/How_to_Add_an_Artist)
-   [How to use artist credits](https://musicbrainz.org/doc/How_to_Use_Artist_Credits)
-   [How to split artists](https://musicbrainz.org/doc/How_to_Split_Artists)
-   [How to merge artists](https://musicbrainz.org/doc/How_To_Merge_Artists)
