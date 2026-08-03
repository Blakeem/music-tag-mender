Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## Label

#### Example

[http://musicbrainz.org/ws/2/label/?query=%22Devil's%20Records%22](http://musicbrainz.org/ws/2/label/?query=%22Devil's%20Records%22)

#### Search Fields

The [Label](https://musicbrainz.org/doc/Label) index contains the following fields you can search

| Field | Description |
| --- | --- |
| alias | (part of) any [alias](https://musicbrainz.org/doc/Aliases) attached to the label (diacritics are ignored) |
| area | (part of) the name of the label's main associated area |
| begin | the label's begin date (e.g. "1980-01-22") |
| code | the [label code](https://musicbrainz.org/doc/Label/Label_Code) for the label (only the numbers, without "LC") |
| comment | (part of) the label's disambiguation comment |
| country | the 2-letter code (ISO 3166-1 alpha-2) for the label's associated country |
| end | the label's end date (e.g. "1980-01-22") |
| ended | a boolean flag (true/false) indicating whether or not the label has ended (is dissolved) |
| ipi | an IPI code associated with the label |
| isni | an ISNI code associated with the label |
| label | (part of) the label's name (diacritics are ignored) |
| labelaccent | (part of) the label's name (with the specified diacritics) |
| laid | the label's MBID |
| release\_count | the amount of releases related to the label |
| sortname | equivalent to name (labels no longer have separate sort names) |
| tag | (part of) a tag attached to the label |
| type | the label's [type](https://musicbrainz.org/doc/Label/Type) |

If you don't specify a field, the terms will be searched for in the _alias_ and _label_ fields.

#### Xml

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<metadata created="2017-03-12T16:59:29.160Z" xmlns="http://musicbrainz.org/ns/mmd-2.0#" xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">
  <label-list count="1" offset="0">
    <label id="d2c296e3-10a4-4ba9-97b9-5620ff8a3ce0" type="Original Production" ext:score="100">
      <name>Devil's Records</name>
      <sort-name>Devil's Records</sort-name>
      <country>FR</country>
      <area id="08310658-51eb-3801-80de-5a0739207115">
        <name>France</name>
        <sort-name>France</sort-name>
      </area>
      <life-span>
        <ended>false</ended>
      </life-span>
      <alias-list>
        <alias sort-name="Devil Records">Devil Records</alias>
        <alias sort-name="Devils Records">Devils Records</alias>
      </alias-list>
    </label>
  </label-list>
</metadata>
```

#### Json

```
{
  "created": "2017-03-12T17:31:38.969Z",
  "count": 1,
  "offset": 0,
  "labels": [
    {
      "id": "d2c296e3-10a4-4ba9-97b9-5620ff8a3ce0",
      "type": "Original Production",
      "score": "100",
      "name": "Devil's Records",
      "sort-name": "Devil's Records",
      "country": "FR",
      "area": {
        "id": "08310658-51eb-3801-80de-5a0739207115",
        "name": "France",
        "sort-name": "France"
      },
      "life-span": {
        "ended": null
      },
      "aliases": [
        {
          "sort-name": "Devil Records",
          "name": "Devil Records",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Devils Records",
          "name": "Devils Records",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        }
      ]
    }
  ]
}
```

