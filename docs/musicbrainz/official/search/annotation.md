Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## Annotation

#### Example

[http://musicbrainz.org/ws/2/annotation/?query=entity:bdb24cb5-404b-4f60-bba4-7b730325ae47](http://musicbrainz.org/ws/2/annotation/?query=entity:bdb24cb5-404b-4f60-bba4-7b730325ae47)

#### Search Fields

The [Annotation](https://musicbrainz.org/doc/Annotation) index contains the following fields you can search

| Field | Description |
| --- | --- |
| entity | the annotated entity's MBID |
| id | the numeric ID of the annotation (e.g. [703027](https://musicbrainz.org/artist/c130b0fb-5dce-449d-9f40-1437f889f7fe/annotation/703027)) |
| name | the annotated entity's name or title (diacritics are ignored) |
| text | the annotation's content (includes [wiki formatting](https://musicbrainz.org/doc/Annotation#Wiki_formatting)) |
| type | the annotated entity's entity type |

If you don't specify a field, the terms will be searched for in the _name_, _text_ and _type_ fields.

#### Xml

```
<metadata created="2013-02-05T04:41:32.273Z" xmlns="http://musicbrainz.org/ns/mmd-2.0#" xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">
    <annotation-list count="1" offset="0">
        <annotation type="release" ext:score="100">
            <entity>bdb24cb5-404b-4f60-bba4-7b730325ae47</entity>
            <name>Pieds nus sur la braise</name>
            <text>Lyrics and music by Merzhin except:
04, 08, 09, 10 (V. L'hour - Merzhin),
03 (V. L'hour - P. Le Bourdonnec - Merzhin),
05 & 13 (P. Le Bourdonnec - Merzhin),
06 ([http://musicbrainz.org/artist/38cfa519-21bb-4e79-8388-3bf798b8c076.html|JM. Poisson] - Merzhin),
07 ([http://musicbrainz.org/artist/f2d7c07c-a8e7-45c9-a888-0b2e6e3a240d.html|Ignatus] - V. L'hour - Merzhin),
11 ([http://musicbrainz.org/artist/f2d7c07c-a8e7-45c9-a888-0b2e6e3a240d.html|Ignatus] - Merzhin),
12 ([http://musicbrainz.org/artist/38cfa519-21bb-4e79-8388-3bf798b8c076.html|JM. Poisson]).</text>
        </annotation>
    </annotation-list>
</metadata>
```

#### Json

```
{
  "created": "2017-03-12T16:27:16.317Z",
  "count": 1,
  "offset": 0,
  "annotations": [
    {
      "type": "release",
      "score": "100",
      "entity": "bdb24cb5-404b-4f60-bba4-7b730325ae47",
      "name": "Pieds nus sur la braise",
      "text": "Lyrics and music by Merzhin except:\r\n04, 08, 09, 10 (V. L'hour - Merzhin),\r\n03 (V. L'hour - P. Le Bourdonnec - Merzhin),\r\n05 & 13 (P. Le Bourdonnec - Merzhin),\r\n06 ([http://musicbrainz.org/artist/38cfa519-21bb-4e79-8388-3bf798b8c076.html|JM. Poisson] - Merzhin),\r\n07 ([http://musicbrainz.org/artist/f2d7c07c-a8e7-45c9-a888-0b2e6e3a240d.html|Ignatus] - V. L'hour - Merzhin),\r\n11 ([http://musicbrainz.org/artist/f2d7c07c-a8e7-45c9-a888-0b2e6e3a240d.html|Ignatus] - Merzhin),\r\n12 ([http://musicbrainz.org/artist/38cfa519-21bb-4e79-8388-3bf798b8c076.html|JM. Poisson])."
    }
  ]
}
```

