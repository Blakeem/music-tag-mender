Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## Area

#### Example

[http://musicbrainz.org/ws/2/area/?query=%22%C3%8Ele-de-France%22](http://musicbrainz.org/ws/2/area/?query=%22%C3%8Ele-de-France%22)

#### Search Fields

The [Area](https://musicbrainz.org/doc/Area) index contains the following fields you can search

| Field | Description |
| --- | --- |
| aid | the area's MBID |
| alias | (part of) any [alias](https://musicbrainz.org/doc/Aliases) attached to the artist (diacritics are ignored) |
| area | (part of) the area's name (diacritics are ignored) |
| areaaccent | (part of) the area's name (with the specified diacritics) |
| begin | the area's begin date (e.g. "1980-01-22") |
| comment | (part of) the area's disambiguation comment |
| end | the area's end date (e.g. "1980-01-22") |
| ended | a boolean flag (true/false) indicating whether or not the area has ended (is no longer current) |
| iso | an [ISO 3166-1, 3166-2 or 3166-3](https://en.wikipedia.org/wiki/ISO_3166) code attached to the area |
| iso1 | an [ISO 3166-1](https://en.wikipedia.org/wiki/ISO_3166) code attached to the area |
| iso2 | an [ISO 3166-2](https://en.wikipedia.org/wiki/ISO_3166) code attached to the area |
| iso3 | an [ISO 3166-3](https://en.wikipedia.org/wiki/ISO_3166) code attached to the area |
| sortname | equivalent to name (areas no longer have separate sort names) |
| tag | (part of) a tag attached to the area |
| type | the area's [type](https://musicbrainz.org/doc/Area#Type) |

If you don't specify a field, the terms will be searched for in the _area_ field.

#### Xml

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<metadata created="2013-06-12T10:51:14.668Z" xmlns="http://musicbrainz.org/ns/mmd-2.0#" xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">
    <area-list count="1" offset="0">
        <area id="d79e4501-8cba-431b-96e7-bb9976f0ae76" type="Subdivision" ext:score="100">
            <name>Île-de-France</name>
            <sort-name>Île-de-France</sort-name>
            <iso-3166-2-code-list>
                <iso-3166-2-code>FR-J</iso-3166-2-code>
            </iso-3166-2-code-list>
            <life-span>
                <ended>false</ended>
            </life-span>
            <alias-list>
                <alias locale="nl" sort-name="Île-de-France" type="Area name" primary="primary">Île-de-France</alias>
                <alias locale="en" sort-name="Île-de-France" type="Area name" primary="primary">Île-de-France</alias>
                <alias locale="fr" sort-name="Île-de-France" type="Area name" primary="primary">Île-de-France</alias>
                <alias locale="it" sort-name="Île-de-France" type="Area name" primary="primary">Île-de-France</alias>
                <alias locale="fi" sort-name="Île-de-France" type="Area name" primary="primary">Île-de-France</alias>
                <alias locale="et" sort-name="Île-de-France" type="Area name" primary="primary">Île-de-France</alias>
                <alias locale="de" sort-name="Île-de-France" type="Area name" primary="primary">Île-de-France</alias>
                <alias locale="no" sort-name="Île-de-France" type="Area name" primary="primary">Île-de-France</alias>
                <alias locale="es" sort-name="Isla de Francia" type="Area name" primary="primary">Isla de Francia</alias>
                <alias locale="ja" sort-name="イル＝ド＝フランス地域圏" type="Area name" primary="primary">イル＝ド＝フランス地域圏</alias>
            </alias-list>
        </area>
    </area-list>
</metadata>
```

#### Json

```
{
  "created": "2017-03-12T16:59:34.096Z",
  "count": 1,
  "offset": 0,
  "areas": [
    {
      "id": "d79e4501-8cba-431b-96e7-bb9976f0ae76",
      "type": "Subdivision",
      "score": "100",
      "name": "Île-de-France",
      "sort-name": "Île-de-France",
      "iso-3166-2-codes": [
        "FR-J"
      ],
      "life-span": {
        "ended": null
      },
      "aliases": [
        {
          "sort-name": "Île-de-France",
          "name": "Île-de-France",
          "locale": "no",
          "type": "Area name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Île-de-France",
          "name": "Île-de-France",
          "locale": "de",
          "type": "Area name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Île-de-France",
          "name": "Île-de-France",
          "locale": "en",
          "type": "Area name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Île-de-France",
          "name": "Île-de-France",
          "locale": "et",
          "type": "Area name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Île-de-France",
          "name": "Île-de-France",
          "locale": "fi",
          "type": "Area name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Île-de-France",
          "name": "Île-de-France",
          "locale": "fr",
          "type": "Area name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Île-de-France",
          "name": "Île-de-France",
          "locale": "it",
          "type": "Area name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Île-de-France",
          "name": "Île-de-France",
          "locale": "nl",
          "type": "Area name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Isla de Francia",
          "name": "Isla de Francia",
          "locale": "es",
          "type": "Area name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "イル＝ドフランス地域圏",
          "name": "イル＝ド＝フランス地域圏",
          "locale": "ja",
          "type": "Area name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        }
      ],
      "relation-list": [
        {
          "relations": [
            {
              "type": "part of",
              "type-id": "de7cc874-8b1b-3a05-8272-f3834c968fb7",
              "target": "08310658-51eb-3801-80de-5a0739207115",
              "direction": "backward",
              "area": {
                "id": "08310658-51eb-3801-80de-5a0739207115",
                "type": "Country",
                "name": "France",
                "sort-name": "France",
                "life-span": {
                  "ended": null
                }
              }
            }
          ]
        }
      ]
    }
  ]
}
```

