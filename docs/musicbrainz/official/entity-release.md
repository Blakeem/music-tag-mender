Source: https://musicbrainz.org/doc/Release
Transcluded from wiki.musicbrainz.org/Release, revision #78324
Version: ws/2 (current production version) — describes the release entity's data model, i.e. the
fields returned by the API for a release (title, date, country, label, barcode, status, packaging,
language, script, quality). Not an API-mechanics page itself.
Retrieved: 2026-07-07

---

A MusicBrainz release represents the unique release (i.e. issuing) of a product containing at least one audio [medium](https://musicbrainz.org/doc/Medium) (a disc, for example, on a CD release). Each release has one or more identifying properties, such as a release date and country, a label, a barcode, a specific type of packaging or a specific cover art.

If you walk into (or digitally browse) a store and see the standard edition of an album next to a deluxe edition of that same album, you're looking at two different MusicBrainz releases, which are grouped as part of the same [release group](https://musicbrainz.org/doc/Release_Group).

Mediums have a format (such as CD, DVD, vinyl or cassette) and can optionally also have a title. For physical releases, each medium is the actual physical medium that stores the audio content. This means that each CD in a multi-disc release will be entered as separate mediums within the release, and that both sides of a vinyl record or cassette will exist on one medium. For digital releases, mediums are a more fluid concept, and should generally just follow the structure set by the artist or label. For example, a digital album that claims to include "disc 1" and "disc 2" should be added with two digital mediums, even if it can be purchased as one single folder of files or streamed in a single sitting.

Keep in mind there are some exceptional cases where the one disc to one medium equivalence does not apply. For example, the two sides of a hybrid SACD (the CD side and the SACD side) should be entered as two mediums. These exceptions are indicated in the [release guidelines](https://musicbrainz.org/doc/Style/Release).

Every medium can (and should ideally have) a tracklist, which represents the set and ordering of [tracks](https://musicbrainz.org/doc/Track) included in the medium, as listed on a liner, a digital store page, or any other official source. A medium can be empty (missing its tracklist) if its contents are not yet known to MusicBrainz users.

## Contents

-   [1 Examples](#Examples)
-   [2 Style guidelines](#Style_guidelines)
-   [3 Release properties](#Release_properties)
    -   [3.1 Title](#Title)
    -   [3.2 Artist](#Artist)
    -   [3.3 Date](#Date)
    -   [3.4 Country](#Country)
    -   [3.5 Label](#Label)
    -   [3.6 Catalog number](#Catalog_number)
    -   [3.7 Barcode](#Barcode)
    -   [3.8 Status](#Status)
    -   [3.9 Packaging](#Packaging)
    -   [3.10 Language](#Language)
    -   [3.11 Script](#Script)
    -   [3.12 MBID](#MBID)
    -   [3.13 Disambiguation comment](#Disambiguation_comment)
    -   [3.14 Annotation](#Annotation)
    -   [3.15 Data quality](#Data_quality)
-   [4 Medium properties](#Medium_properties)
    -   [4.1 Title](#Title_2)
    -   [4.2 Format](#Format)

## Examples

-   CD single: [Creep](https://musicbrainz.org/release/ed118c5f-d940-4b52-a37b-b1a205374abe)
-   2× CD album: [The Fragile](https://musicbrainz.org/release/a4864e94-6d75-4ade-bc93-0dabf3521453)
-   Vinyl single: [Feel the Music](https://musicbrainz.org/release/e6e4ae10-4241-43a5-a9ae-911277348c59)
-   2× vinyl album: [Blonde on Blonde](https://musicbrainz.org/release/2b259ee4-06b0-4dbb-a248-be983aee6fbd)
-   4x digital media album: [Terraria: Official Soundtrack](https://musicbrainz.org/release/51031f3d-033a-4ab1-9739-a33b4e3eef02)
-   Digital audiobook: [Der Schattenkrieg](https://musicbrainz.org/release/594687cc-bdc1-4fff-858f-25bb5ec0d87d)

## Style guidelines

Please see the [guidelines for releases](https://musicbrainz.org/doc/Style/Release).

## Release properties

### Title

The title of the release.

### Artist

The artist(s) that the release is primarily credited to, as [credited on the release](https://musicbrainz.org/doc/Artist_Credit).

### Date

The [date](https://musicbrainz.org/doc/Release/Date) the release was issued.

### Country

The [country](https://musicbrainz.org/doc/Release_Country) the release was issued in.

### Label

The [label](https://musicbrainz.org/doc/Label) which issued the release. There may be more than one.

### Catalog number

This is a [catalog number](https://musicbrainz.org/doc/Release/Catalog_Number) assigned to the release by the label which can often be found on the spine or near the barcode. There may be more than one, especially when multiple labels are involved. This is not the ASIN — there is a [relationship](https://musicbrainz.org/relationship/4f2e710d-166c-480c-a293-2e2c8d658d87) for that — nor the [label code](https://musicbrainz.org/doc/Label_Code).

### Barcode

The [barcode](https://musicbrainz.org/doc/barcode), if the release has one. The most common types found on releases are 12-digit [UPCs](https://en.wikipedia.org/wiki/Universal_Product_Code) and 13-digit [EANs](https://en.wikipedia.org/wiki/European_Article_Number).

### Status

The [status](https://musicbrainz.org/doc/Style/Release#Status) describes how "official" a release is. Possible values are:

<dl><dd><dl><dd><table class="wikitable "><tbody><tr><td><dl><dt>official</dt><dd>Any release officially sanctioned by the artist and/or their record company. Most releases will fit into this category.</dd></dl><dl><dt>promotion</dt><dd>A give-away release or a release intended to promote an upcoming official release (e.g. pre-release versions, releases included with a magazine, versions supplied to radio DJs for air-play).</dd></dl><dl><dt>bootleg</dt><dd>An unofficial/underground release that was not sanctioned by the artist and/or the record company. This includes unofficial live recordings and pirated releases.</dd></dl><dl><dt>pseudo-release</dt><dd>An alternate version of a release where the titles have been changed. These don't correspond to any real release and should be linked to the original release using the <a class="extiw" href="https://musicbrainz.org/relationship/fc399d47-23a7-4c28-bfcf-0607a562b644">transl(iter)ation relationship</a>.</dd></dl><dl><dt>withdrawn</dt><dd>An official release that was actively withdrawn from circulation by the artist and/or their record company after being released, whether to replace it with a new version or to retire it altogether. This does not include releases that have reached the end of their "natural" life cycle, such as being sold out and out of print.</dd></dl><dl><dt>expunged</dt><dd>A previously official release that was actively expunged from an artist or records company's discography. This should not be used in cases where the release was just withdrawn, there needs to be known artist or label intent to disown the release and no longer consider it part of their discography. If it is unclear, use Withdrawn.</dd></dl><dl><dt>cancelled</dt><dd>A planned official release that was cancelled before being released, but for which enough info is known to still confidently list it (e.g. it was available for preorder).</dd></dl></td></tr></tbody></table></dd></dl></dd></dl>

### Packaging

The outermost physical packaging that the release is sold or distributed in. See the [list of packaging](https://musicbrainz.org/doc/Release/Packaging) for more information.

### Language

The language the release title and track titles are written in. The possible values are taken from the [ISO 639-3](https://en.wikipedia.org/wiki/ISO_639-3) standard.

### Script

The script used to write the release title and track titles. The possible values are taken from the [ISO 15924](https://en.wikipedia.org/wiki/ISO_15924) standard.

<dl><dd><dl><dd><table class="wikitable collapsible collapsed wikitable"><tbody><tr><th>Guide to common scripts</th></tr><tr><td><dl><dt>Latin (also known as Roman or, incorrectly, "English")</dt><dd>Latin is the most common script, and usually the correct choice. It is used for all Western European languages, and many others. It is also the most common script used for transliterations.</dd><dt>Arabic العربية</dt><dd>The Arabic script is used for languages in the Middle East and Central Asia such as Arabic, Persian and Urdu.</dd><dt>Cyrillic Кириллица</dt><dd>Cyrillic is used for languages in Eastern Europe such as Russian, Ukrainian, Belarusian and Bulgarian.</dd><dt>Greek Ελληνικά</dt><dd>The Greek script is used for Greek, but several characters have also been adopted for mathematical uses.</dd><dt>Han 漢字/汉字</dt><dd>Han characters are used by Chinese, Japanese and Korean. Han (simplified), Han (traditional), Japanese, or Korean should be used instead when the variant is known.</dd><dt>Han (simplified) 简体字</dt><dd>The simplified variant of Han characters is used to write Chinese in mainland China, Malaysia and Singapore.</dd><dt>Han (traditional) 繁體字/正體字</dt><dd>The traditional variant of Han characters is used to write Chinese in Hong Kong, Macao and Taiwan.</dd><dt>Korean 한글</dt><dd>This covers any combination of Hangul and Hanja for Korean.</dd><dt>Hebrew עברית</dt><dd>The Hebrew script is used for Hebrew, but a few characters have also been adopted for mathematical uses.</dd><dt>Japanese 漢字 &amp; ひらがな &amp; カタカナ</dt><dd>This covers any combination of Kanji, Hiragana and Katakana for Japanese.</dd><dt>Katakana カタカナ</dt><dd>Katakana should only be used for transliterations into Japanese (example, English-&gt;Japanese). Japanese language titles with words written in Katakana should use Japanese.</dd><dt>Thai ไทย</dt><dd>The Thai script is used for Thai, as well as some minor languages in south-east Asia.</dd></dl></td></tr></tbody></table></dd></dl></dd></dl>

### MBID

See the [page about MBIDs](https://musicbrainz.org/doc/MusicBrainz_Identifier) for more information.

### Disambiguation comment

See the [page about comments](https://musicbrainz.org/doc/Disambiguation_Comment) for more information.

### Annotation

See the [page about annotations](https://musicbrainz.org/doc/Annotation) for more information.

### Data quality

Data quality indicates how good the data for a release is. It is not a mark of how good or bad the music itself is - for that, use [ratings](https://musicbrainz.org/doc/Rating_System).

<dl><dd><dl><dt>High quality</dt><dd>All available data has been added (including relationships, works, etc.), if possible alongside cover art with liner info that proves it.</dd></dl></dd></dl>
<dl><dd><dl><dt>Default quality</dt><dd>This is the default setting - technically "unknown" if the quality has never been modified, "normal" if it has.</dd></dl></dd></dl>
<dl><dd><dl><dt>Low quality</dt><dd>The release needs serious fixes, or its existence is hard to prove (but it's not clearly fake).</dd></dl></dd></dl>

Currently, data quality has no further effect than helping users know what to expect from the data. Until 2012, data quality also used to influence the voting requirements for edits made to the release. While this is no longer the case, we mention it here because it can explain the notes and voting results of some very old edits.

## Medium properties

### Title

The title of this particular medium.

### Format

The [format](https://musicbrainz.org/doc/Release/Format) of the medium.
