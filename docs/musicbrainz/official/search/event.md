Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## Event

#### Example

[https://musicbrainz.org/ws/2/event/?query=unique](https://musicbrainz.org/ws/2/event/?query=unique)

#### Search Fields

The [Event](https://musicbrainz.org/doc/Event) index contains the following fields you can search

| Field | Description |
| --- | --- |
| alias | (part of) any [alias](https://musicbrainz.org/doc/Aliases) attached to the artist (diacritics are ignored) |
| aid | the MBID of an area related to the event |
| area | (part of) the name of an area related to the event |
| arid | the MBID of an artist related to the event |
| artist | (part of) the name of an artist related to the event |
| begin | the event's begin date (e.g. "1980-01-22") |
| comment | (part of) the artist's disambiguation comment |
| end | the event's end date (e.g. "1980-01-22") |
| ended | a boolean flag (true/false) indicating whether or not the event has an end date set |
| eid | the MBID of the event |
| event | (part of) the event's name (diacritics are ignored) |
| eventaccent | (part of) the event's name (with the specified diacritics) |
| pid | the MBID of a place related to the event |
| place | (part of) the name of a place related to the event |
| tag | (part of) a tag attached to the event |
| type | the event's [type](https://musicbrainz.org/doc/Event#Type) |

If you don't specify a field, the terms will be searched for in the _alias_, _artist_ and _event_ fields.

### Xml

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<metadata created="2017-03-12T18:04:50.830Z" xmlns="http://musicbrainz.org/ns/mmd-2.0#" xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">
  <event-list count="1" offset="0">
    <event id="bc58d881-0b8e-45de-bee7-5d4a03d71190" type="Concert" ext:score="100">
      <name>Dominique A at Le Lieu Unique, April 2012</name>
      <life-span>
        <begin>2012-04-12</begin>
        <end>2012-04-12</end>
      </life-span>
      <time>20:30:00</time>
      <relation-list target-type="artist">
        <relation type="main performer">
          <direction>backward</direction>
          <artist id="8ae5648d-d9b7-4083-b538-17f71672990f">
            <name>Dominique A</name>
            <sort-name>A, Dominique</sort-name>
          </artist>
        </relation>
      </relation-list>
      <relation-list target-type="place">
        <relation type="held at">
          <direction>backward</direction>
          <place id="ab7fd308-da33-4d01-8bdb-d8238bb3842a">
            <name>Le Lieu Unique</name>
          </place>
        </relation>
      </relation-list>
    </event>
  </event-list>
</metadata>
```

### Json

```
{
  "created": "2017-03-12T18:04:50.83Z",
  "count": 1,
  "offset": 0,
  "events": [
    {
      "id": "bc58d881-0b8e-45de-bee7-5d4a03d71190",
      "type": "Concert",
      "score": "100",
      "name": "Dominique A at Le Lieu Unique, April 2012",
      "life-span": {
        "begin": "2012-04-12",
        "end": "2012-04-12"
      },
      "time": "20:30:00",
      "relations": [
        {
          "type": "main performer",
          "direction": "backward",
          "artist": {
            "id": "8ae5648d-d9b7-4083-b538-17f71672990f",
            "name": "Dominique A",
            "sort-name": "A, Dominique"
          }
        },
        {
          "type": "held at",
          "direction": "backward",
          "place": {
            "id": "ab7fd308-da33-4d01-8bdb-d8238bb3842a",
            "name": "Le Lieu Unique"
          }
        }
      ]
    }
  ]
}
```

