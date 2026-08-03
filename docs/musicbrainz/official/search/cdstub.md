Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## CDStubs

#### Example

[http://musicbrainz.org/ws/2/cdstub/?query=title:Doo](http://musicbrainz.org/ws/2/cdstub/?query=title:Doo)

#### Search Fields

The [CD stub](https://musicbrainz.org/doc/CD_Stub) index contains the following fields you can search:

| Field | Description |
| --- | --- |
| added | the date the CD stub was added (e.g. "2020-01-22") |
| artist | (part of) the artist name set on the CD stub |
| barcode | the barcode set on the CD stub |
| comment | (part of) the comment set on the CD stub |
| discid | the CD stub's [Disc ID](https://musicbrainz.org/doc/Disc_ID) |
| title | (part of) the release title set on the CD stub |
| tracks | the number of tracks on the CD stub |

If you don't specify a field, the terms will be searched for in the _artist_ and _title_ fields.

#### Xml

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<metadata created="2013-02-05T08:20:53.231Z" xmlns="http://musicbrainz.org/ns/mmd-2.0#" xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">
    <cdstub-list count="1" offset="0">
        <cdstub id="NKpg6j_S6swmiKYEYsFhYrSwgQA-" ext:score="100">
            <title>Stupid Doo Doo Dumb</title>
            <artist>Mac Dre</artist>
            <barcode>618763105025</barcode>
            <comment>CD Baby id:macdremusic5</comment>
            <track-list count="15"/>
        </cdstub>
    </cdstub-list>
</metadata>
```

#### Json

```
{
    "created": "2017-02-14T11:00:32.046Z",
    "count": 53,
    "offset": 0,
    "cdstubs": [
        {
            "id": "NKpg6j_S6swmiKYEYsFhYrSwgQA-",
            "score": "100",
            "count": 15,
            "title": "Stupid Doo Doo Dumb",
            "artist": "Mac Dre",
            "barcode": "618763105025",
            "comment": "CD Baby id:macdremusic5"
        }
    ]
}
```

