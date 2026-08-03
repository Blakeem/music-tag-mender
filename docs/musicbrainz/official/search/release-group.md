Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## Release Group

#### Example

[http://musicbrainz.org/ws/2/release-group/?query=release:Tenance](http://musicbrainz.org/ws/2/release-group/?query=release:Tenance)

#### Search Fields

The [release group](https://musicbrainz.org/doc/Release_Group) index contains the following fields you can search:

| Field | Description |
| --- | --- |
| alias | (part of) any [alias](https://musicbrainz.org/doc/Aliases) attached to the release group (diacritics are ignored) |
| arid | the MBID of any of the release group artists |
| artist | (part of) the combined credited artist name for the release group, including join phrases (e.g. "Artist X feat.") |
| artistname | (part of) the name of any of the release group artists |
| comment | (part of) the release group's disambiguation comment |
| creditname | (part of) the credited name of any of the release group artists on this particular release group |
| firstreleasedate | the release date of the earliest release in this release group (e.g. "1980-01-22") |
| primarytype | the [primary type](https://musicbrainz.org/doc/Release_Group/Type#Primary_types) of the release group |
| reid | the MBID of any of the releases in the release group |
| release | (part of) the title of any of the releases in the release group |
| releasegroup | (part of) the release group's title (diacritics are ignored) |
| releasegroupaccent | (part of) the release group's title (with the specified diacritics) |
| releases | the number of releases in the release group |
| rgid | the release group's MBID |
| secondarytype | any of the [secondary types](https://musicbrainz.org/doc/Release_Group/Type#Secondary_types) of the release group |
| status | the [status](https://musicbrainz.org/doc/Release#Status) of any of the releases in the release group |
| tag | (part of) a tag attached to the release group |
| type | legacy [release group type](https://musicbrainz.org/doc/Release_Group/Type) field that predates the ability to set multiple types ([see calculation code](https://github.com/metabrainz/musicbrainz-server/blob/f8bd7e9366eb6e836dfcee9b626e1ca969db1c4f/lib/MusicBrainz/Server/WebService/XMLSerializer.pm#L305-L326)) |

If you don't specify a field, the terms will be searched for in the _releasegroup_ field.

#### Xml

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#" xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">
      <release-group-list offset="0" count="1">
          <release-group ext:score="100" type="Single" id="70664047-2545-4e46-b75f-4556f2a7b83e">
              <title>Main Tenance</title>
              <first-release-date>1998</first-release-date>
              <artist-credit id="ce1e40a1-048d-3ffb-afd7-c9d6eb6984c5">
                  <name-credit> 
                      <artist id="a8fa58d8-f60b-4b83-be7c-aea1af11596b">
                          <name>Fred Giannelli</name>
                          <sort-name>Giannelli, Fred</sort-name>
                          <disambiguation>US electronic artist</disambiguation>
                      </artist>
                  </name-credit>
              </artist-credit>
              <release-list count="1">
                  <release id="9168f4cc-a852-4ba5-bf85-602996625651">
                      <title>Main Tenance</title>
                  </release>
              </release-list>
          </release-group>
    </release-group-list>
</metadata>
```

#### Json

```
{
  "created": "2017-03-12T17:27:52.777Z",
  "count": 1,
  "offset": 0,
  "release-groups": [
    {
      "id": "70664047-2545-4e46-b75f-4556f2a7b83e",
      "score": "100",
      "count": 1,
      "title": "Main Tenance",
      "first-release-date": "1998",
      "primary-type": "Single",
      "artist-credit": [
        {
          "artist": {
            "id": "a8fa58d8-f60b-4b83-be7c-aea1af11596b",
            "name": "Fred Giannelli",
            "sort-name": "Giannelli, Fred",
            "disambiguation": "US electronic artist",
            "aliases": [
              {
                "sort-name": "Fred",
                "name": "Fred",
                "locale": null,
                "type": null,
                "primary": null,
                "begin-date": null,
                "end-date": null
              },
              {
                "sort-name": "Giannelli, Fred Domenic, II",
                "name": "Fred Domenic Giannelli II",
                "locale": null,
                "type": "Legal name",
                "primary": null,
                "begin-date": null,
                "end-date": null
              },
              {
                "sort-name": "Fred Gianelli",
                "name": "Fred Gianelli",
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
      "artist-credit-id": "ce1e40a1-048d-3ffb-afd7-c9d6eb6984c5",
      "releases": [
        {
          "id": "9168f4cc-a852-4ba5-bf85-602996625651",
          "title": "Main Tenance",
          "status": "Official"
        }
      ]
    }
  ]
}
```

