Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## Series

#### Example

[https://musicbrainz.org/ws/2/series/?query=%22Studio%20Brussel%22](https://musicbrainz.org/ws/2/series/?query=%22Studio%20Brussel%22)

#### Search Fields

The [Series](https://musicbrainz.org/doc/Series) index contains the following fields you can search

| Field | Description |
| --- | --- |
| alias | (part of) any [alias](https://musicbrainz.org/doc/Aliases) attached to the series (diacritics are ignored) |
| comment | (part of) the series' disambiguation comment |
| series | (part of) the series' name (diacritics are ignored) |
| seriesaccent | (part of) the series' name (with the specified diacritics) |
| sid | the series' MBID |
| tag | (part of) a tag attached to the series |
| type | the series' [type](https://musicbrainz.org/doc/Series#Type) |

If you don't specify a field, the terms will be searched for in the _alias_ and _series_ fields.

### Xml

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<metadata created="2017-03-12T18:06:29.595Z" xmlns="http://musicbrainz.org/ns/mmd-2.0#" xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">
  <series-list count="2" offset="0">
    <series id="dab51ad0-48ba-4ff3-aa77-8b13bf5f40d9" type="Release group" ext:score="100">
      <name>Studio Brussel: De Maxx</name>
      <disambiguation>radio show compilations</disambiguation>
    </series>
    <series id="9b5b8690-8cd8-4f1e-942c-c2031c59d3ff" type="Release group" ext:score="87">
      <name>Studio Brussel: Life Is Music</name>
      <disambiguation>radio show compilations</disambiguation>
      <tag-list>
        <tag count="1">
          <name>stubru</name>
        </tag>
      </tag-list>
    </series>
  </series-list>
</metadata>
```

### Json

```
{
  "created": "2017-03-12T18:06:29.595Z",
  "count": 2,
  "offset": 0,
  "series": [
    {
      "id": "dab51ad0-48ba-4ff3-aa77-8b13bf5f40d9",
      "type": "Release group",
      "score": "100",
      "name": "Studio Brussel: De Maxx",
      "disambiguation": "radio show compilations"
    },
    {
      "id": "9b5b8690-8cd8-4f1e-942c-c2031c59d3ff",
      "type": "Release group",
      "score": "87",
      "name": "Studio Brussel: Life Is Music",
      "disambiguation": "radio show compilations",
      "tags": [
        {
          "count": 1,
          "name": "stubru"
        }
      ]
    }
  ]
}
```

