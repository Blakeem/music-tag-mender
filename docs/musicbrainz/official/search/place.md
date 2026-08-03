Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## Place

#### Example

[http://musicbrainz.org/ws/2/place/?query=chipping](http://musicbrainz.org/ws/2/place/?query=chipping)

#### Search Fields

The [Place](https://musicbrainz.org/doc/Place) index contains the following fields you can search

| Field | Description |
| --- | --- |
| address | (part of) the physical address for this place |
| alias | (part of) any [alias](https://musicbrainz.org/doc/Aliases) attached to the place (diacritics are ignored) |
| area | (part of) the name of the place's main associated area |
| begin | the place's begin date (e.g. "1980-01-22") |
| comment | (part of) the place's disambiguation comment |
| end | the place's end date (e.g. "1980-01-22") |
| ended | a boolean flag (true/false) indicating whether or not the place has ended (is closed) |
| lat | the (WGS 84) latitude of the place's coordinates (e.g. "58.388226") |
| long | the (WGS 84) longitude of the place's coordinates (e.g. "26.702817") |
| place | (part of) the place's name (diacritics are ignored) |
| placeaccent | (part of) the place's name (with the specified diacritics) |
| pid | the place's MBID |
| type | the place's [type](https://musicbrainz.org/doc/Place#Type) |

  
If you don't specify a field, the terms will be searched for in the _address_, _alias_, _area_ and _place_ fields.

#### Xml

```
<metadata created="2015-02-02T17:29:02.301Z">
    <place-list count="1" offset="0">
        <place id="d1ab65f8-d082-492a-bd70-ce375548dabf" type="Studio" ext:score="100">
            <name>Chipping Norton Recording Studios</name>
            <address>28–30 New Street, Chipping Norton</address>
            <area id="44e5e20e-8fbc-4b07-b3f2-22f2199186fd">
                <name>Oxfordshire</name>
                <sort-name>Oxfordshire</sort-name>
            </area>
        <life-span>
            <begin>1971</begin>
            <end>1999-10</end>
            <ended>true</ended>
        </life-span>
        </place>
    </place-list>
</metadata>
```

#### Json

```
{
  "created": "2017-03-12T16:59:39.959Z",
  "count": 1,
  "offset": 0,
  "places": [
    {
      "id": "d1ab65f8-d082-492a-bd70-ce375548dabf",
      "type": "Studio",
      "score": "100",
      "name": "Chipping Norton Recording Studios",
      "address": "28–30 New Street, Chipping Norton",
      "coordinates": {
        "latitude": "51.9414",
        "longitude": "-1.548"
      },
      "area": {
        "id": "716234d3-b8ed-45ac-8983-e7219eb85956",
        "name": "Chipping Norton",
        "sort-name": "Chipping Norton"
      },
      "life-span": {
        "begin": "1971",
        "end": "1999-10",
        "ended": true
      }
    }
  ]
}
```

