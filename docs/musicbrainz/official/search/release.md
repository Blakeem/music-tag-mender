Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## Release

#### Example

[http://musicbrainz.org/ws/2/release/?query=release:Schneider%20AND%20Shake](http://musicbrainz.org/ws/2/release/?query=release:Schneider%20AND%20Shake)

#### Search Fields

The [Release](https://musicbrainz.org/doc/Release) index contains the following fields you can search

| Field | Description |
| --- | --- |
| alias | (part of) any [alias](https://musicbrainz.org/doc/Aliases) attached to the release (diacritics are ignored) |
| arid | the MBID of any of the release artists |
| artist | (part of) the combined credited artist name for the release, including join phrases (e.g. "Artist X feat.") |
| artistname | (part of) the name of any of the release artists |
| asin | an [Amazon ASIN](https://musicbrainz.org/doc/ASIN) for the release |
| barcode | the barcode for the release |
| catno | any catalog number for this release (insensitive to case, spaces, and separators) |
| comment | (part of) the release's disambiguation comment |
| country | the 2-letter code (ISO 3166-1 alpha-2) for any country the release was released in |
| creditname | (part of) the credited name of any of the release artists on this particular release |
| date | a release date for the release (e.g. "1980-01-22") |
| discids | the total number of [disc IDs](https://musicbrainz.org/doc/Disc_ID) attached to all mediums on the release |
| discidsmedium | the number of [disc IDs](https://musicbrainz.org/doc/Disc_ID) attached to any one medium on the release |
| format | the [format](https://musicbrainz.org/doc/Release/Format) of any medium in the release (insensitive to case, spaces, and separators) |
| laid | the MBID of any of the release labels |
| label | (part of) the name of any of the release labels |
| lang | the [ISO 639-3 code](https://iso639-3.sil.org/code_tables/639/data) for the release language |
| mediumid | the MBID of any of the mediums in the release |
| mediums | the number of mediums on the release |
| packaging | the [format](https://musicbrainz.org/doc/Release/Packaging) of the release (insensitive to case, spaces, and separators) |
| primarytype | the [primary type](https://musicbrainz.org/doc/Release_Group/Type#Primary_types) of the release group for this release |
| quality | the listed [quality](https://musicbrainz.org/doc/Release#Data_quality) of the data for the release (2 for “high”, 1 for “normal”; cannot search for “low” at the moment; see the related [bug report](https://tickets.metabrainz.org/browse/SEARCH-666)) |
| reid | the release's MBID |
| release | (part of) the release's title (diacritics are ignored) |
| releaseaccent | (part of) the release's title (with the specified diacritics) |
| rgid | the MBID of the release group for this release |
| script | the [ISO 15924 code](http://unicode.org/iso15924/iso15924-codes.html) for the release script |
| secondarytype | any of the [secondary types](https://musicbrainz.org/doc/Release_Group/Type#Secondary_types) of the release group for this release |
| status | the [status](https://musicbrainz.org/doc/Release#Status) of the release |
| tag | (part of) a tag attached to the release |
| tracks | the total number of tracks on the release |
| tracksmedium | the number of tracks on any one medium on the release |
| type | legacy [release group type](https://musicbrainz.org/doc/Release_Group/Type) field that predates the ability to set multiple types ([see calculation code](https://github.com/metabrainz/musicbrainz-server/blob/f8bd7e9366eb6e836dfcee9b626e1ca969db1c4f/lib/MusicBrainz/Server/WebService/XMLSerializer.pm#L305-L326)) |

If you don't specify a field, the terms will be searched for in the _release_ field.

#### Xml

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<metadata created="2017-03-12T17:20:00.235Z" xmlns="http://musicbrainz.org/ns/mmd-2.0#" xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">
  <release-list count="1" offset="0">
    <release id="62d1c4ef-fc00-37af-8df7-485f6a31fcc4" ext:score="100">
      <title>Fred Schneider & The Shake Society</title>
      <status id="4e304316-386d-3409-af2e-78857eec5cfe">Official</status>
      <packaging>Cardboard/Paper Sleeve</packaging>
      <text-representation>
        <language>eng</language>
        <script>Latn</script>
      </text-representation>
      <artist-credit id="04590110-2280-322a-a0c4-6d6bc8dc17e8">
        <name-credit>
          <artist id="43bcca8b-9edc-4997-8343-122350e790bf">
            <name>Fred Schneider</name>
            <sort-name>Schneider, Fred</sort-name>
            <alias-list>
              <alias sort-name="Schneider, Frederick William" type="Legal name">Frederick William Schneider</alias>
              <alias sort-name="Fred Schneider & the Shake Society">Fred Schneider & the Shake Society</alias>
            </alias-list>
          </artist>
        </name-credit>
      </artist-credit>
      <release-group id="0ef97d52-3f00-31bf-8413-f83ccb362675" type="Album">
        <primary-type>Album</primary-type>
      </release-group>
      <date>1984</date>
      <country>US</country>
      <release-event-list>
        <release-event>
          <date>1984</date>
          <area id="489ce91b-6658-3307-9877-795b68554c98">
            <name>United States</name>
            <sort-name>United States</sort-name>
            <iso-3166-1-code-list>
              <iso-3166-1-code>US</iso-3166-1-code>
            </iso-3166-1-code-list>
          </area>
        </release-event>
      </release-event-list>
      <barcode>07599251581</barcode>
      <label-info-list>
        <label-info>
          <catalog-number>1-25158</catalog-number>
          <label id="c595c289-47ce-4fba-b999-b87503e8cb71">
            <name>Warner Bros. Records</name>
          </label>
        </label-info>
      </label-info-list>
      <medium-list count="1">
        <track-count>9</track-count>
        <medium id="a6280646-e0be-4a35-8550-50bab13de3b8">
          <format>12" Vinyl</format>
          <disc-list count="0"/>
          <track-list count="9"/>
        </medium>
      </medium-list>
    </release>
  </release-list>
</metadata>
```

#### Json

```
{
  "created": "2017-03-12T17:20:00.235Z",
  "count": 1,
  "offset": 0,
  "releases": [
    {
      "id": "62d1c4ef-fc00-37af-8df7-485f6a31fcc4",
      "score": "100",
      "count": 1,
      "title": "Fred Schneider & The Shake Society",
      "status-id": "4e304316-386d-3409-af2e-78857eec5cfe",
      "status": "Official",
      "packaging": "Cardboard/Paper Sleeve",
      "text-representation": {
        "language": "eng",
        "script": "Latn"
      },
      "artist-credit": [
        {
          "artist": {
            "id": "43bcca8b-9edc-4997-8343-122350e790bf",
            "name": "Fred Schneider",
            "sort-name": "Schneider, Fred",
            "aliases": [
              {
                "sort-name": "Schneider, Frederick William",
                "name": "Frederick William Schneider",
                "locale": null,
                "type": "Legal name",
                "primary": null,
                "begin-date": null,
                "end-date": null
              },
              {
                "sort-name": "Fred Schneider & the Shake Society",
                "name": "Fred Schneider & the Shake Society",
                "locale": null,
                "type": null,
                "primary": null,
                "begin-date": null,
                "end-date": null
              }
            ]
          }
        }
      ],
      "artist-credit-id": "04590110-2280-322a-a0c4-6d6bc8dc17e8",
      "release-group": {
        "id": "0ef97d52-3f00-31bf-8413-f83ccb362675",
        "primary-type": "Album"
      },
      "date": "1984",
      "country": "US",
      "release-events": [
        {
          "date": "1984",
          "area": {
            "id": "489ce91b-6658-3307-9877-795b68554c98",
            "name": "United States",
            "sort-name": "United States",
            "iso-3166-1-codes": [
              "US"
            ]
          }
        }
      ],
      "barcode": "07599251581",
      "label-info": [
        {
          "catalog-number": "1-25158",
          "label": {
            "id": "c595c289-47ce-4fba-b999-b87503e8cb71",
            "name": "Warner Bros. Records"
          }
        }
      ],
      "track-count": 9,
      "media": [
        {
          "id": "a6280646-e0be-4a35-8550-50bab13de3b8",
          "format": "12\" Vinyl",
          "disc-count": 0,
          "track-count": 9
        }
      ]
    }
  ]
}
```

