Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## Work

#### Example

[https://beta.musicbrainz.org/ws/2/work/?query=work:Frozen%20AND%20arid:4c006444-ccbf-425e-b3e7-03a98bab5997](https://beta.musicbrainz.org/ws/2/work/?query=work:Frozen%20AND%20arid:4c006444-ccbf-425e-b3e7-03a98bab5997)

#### Search Fields

The [Work](https://musicbrainz.org/doc/Work) index contains the following fields you can search

| Field | Description |
| --- | --- |
| alias | (part of) any [alias](https://musicbrainz.org/doc/Aliases) attached to the work (diacritics are ignored) |
| arid | the MBID of an artist related to the event (e.g. a composer or lyricist) |
| artist | (part of) the name of an artist related to the work (e.g. a composer or lyricist) |
| comment | (part of) the work's disambiguation comment |
| iswc | any [ISWC](https://musicbrainz.org/doc/ISWC) associated to the work |
| lang | the [ISO 639-3 code](https://iso639-3.sil.org/code_tables/639/data) for any of the languages of the work's lyrics |
| recording | (part of) the title of a recording related to the work |
| recording\_count | the number of recordings related to the work |
| rid | the MBID of a recording related to the work |
| tag | (part of) a tag attached to the work |
| type | the work's type (e.g. "opera", "song", "symphony") |
| wid | the work's MBID |
| work | (part of) the work's title (diacritics are ignored) |
| workaccent | (part of) the work's title (with the specified diacritics) |

If you don't specify a field, the terms will be searched for in the _alias_ and _work_ fields.

#### Xml

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#" xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">
    <work-list offset="0" count="1">
        <work ext:score="100" id="10c1a66a-8166-32ec-a00f-540f111ce7a3">
            <title>Frozen Fred</title>
            <relation-list>
                <relation type="composer">
                    <artist id="4c006444-ccbf-425e-b3e7-03a98bab5997">
                        <name>Michiel Peters</name>             
                        <sort-name>Peters, Michiel</sort-name>
                    </artist>
                </relation>
            </relation-list>
         </work>
    </work-list>
</metadata>
```

#### Json

```
{
  "created": "2017-03-12T17:25:03.53Z",
  "count": 1,
  "offset": 0,
  "works": [
    {
      "id": "10c1a66a-8166-32ec-a00f-540f111ce7a3",
      "score": "100",
      "title": "Frozen Fred",
      "relations": [
        {
          "type": "composer",
          "direction": "backward",
          "artist": {
            "id": "4c006444-ccbf-425e-b3e7-03a98bab5997",
            "name": "Michiel Peters",
            "sort-name": "Peters, Michiel"
          }
        },
        {
          "type": "performance",
          "direction": "backward",
          "recording": {
            "id": "17b376c8-68a8-43bb-a065-ff27c04cfd5f",
            "title": "Frozen Fred",
            "video": null
          }
        }
      ]
    }
  ]
}
