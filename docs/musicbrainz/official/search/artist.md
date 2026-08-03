Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## Artist

#### Example

[http://musicbrainz.org/ws/2/artist/?query=artist:fred%20AND%20type:group%20AND%20country:US](http://musicbrainz.org/ws/2/artist/?query=artist:fred%20AND%20type:group%20AND%20country:US)

#### Search Fields

The [Artist](https://musicbrainz.org/doc/Artist) index contains the following fields you can search

| Field | Description |
| --- | --- |
| alias | (part of) any [alias](https://musicbrainz.org/doc/Aliases) attached to the artist (diacritics are ignored) |
| primary\_alias | (part of) any primary alias attached to the artist (diacritics are ignored) |
| area | (part of) the name of the artist's main associated area |
| arid | the artist's MBID |
| artist | (part of) the artist's name (diacritics are ignored) |
| artistaccent | (part of) the artist's name (with the specified diacritics) |
| begin | the artist's begin date (e.g. "1980-01-22") |
| beginarea | (part of) the name of the artist's begin area |
| comment | (part of) the artist's disambiguation comment |
| country | the 2-letter code (ISO 3166-1 alpha-2) for the artist's main associated country |
| end | the artist's end date (e.g. "1980-01-22") |
| endarea | (part of) the name of the artist's end area |
| ended | a boolean flag (true/false) indicating whether or not the artist has ended (is dissolved/deceased) |
| gender | the artist's gender (“male”, “female”, “other” or “not applicable”) |
| ipi | an IPI code associated with the artist |
| isni | an ISNI code associated with the artist |
| sortname | (part of) the artist's [sort name](https://musicbrainz.org/doc/Artist#Sort_name) |
| tag | (part of) a tag attached to the artist |
| type | the artist's [type](https://musicbrainz.org/doc/Artist#Type) (“person”, “group”, etc.) |

If you don't specify a field, the terms will be searched for in the _alias_, _artist_ and _sortname_ fields.

#### Xml

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<metadata created="2017-03-12T16:54:57.165Z" xmlns="http://musicbrainz.org/ns/mmd-2.0#" xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">
  <artist-list count="11" offset="0">
    <artist id="e56fd97e-c18f-4e5e-9b4d-f9fc21b4973f" type="Group" ext:score="100">
      <name>Fred</name>
      <sort-name>Fred</sort-name>
      <country>US</country>
      <area id="489ce91b-6658-3307-9877-795b68554c98">
        <name>United States</name>
        <sort-name>United States</sort-name>
      </area>
      <begin-area id="489ce91b-6658-3307-9877-795b68554c98">
        <name>United States</name>
        <sort-name>United States</sort-name>
      </begin-area>
      <disambiguation>US progressive rock band</disambiguation>
      <life-span>
        <begin>1969</begin>
        <end>1974</end>
        <ended>true</ended>
      </life-span>
    </artist>
    <artist id="4a024fd4-305e-4fea-9d3f-4ec858766e6e" type="Group" ext:score="100">
      <name>Fred</name>
      <sort-name>Fred</sort-name>
      <country>US</country>
      <area id="489ce91b-6658-3307-9877-795b68554c98">
        <name>United States</name>
        <sort-name>United States</sort-name>
      </area>
      <disambiguation>US comic barbershop quartet</disambiguation>
      <life-span>
        <ended>false</ended>
      </life-span>
    </artist>
    <artist id="1065e6e0-b6b3-4224-9121-4502e52b9f9e" type="Group" ext:score="61">
      <name>A Halo Called Fred</name>
      <sort-name>A Halo Called Fred</sort-name>
      <country>US</country>
      <area id="489ce91b-6658-3307-9877-795b68554c98">
        <name>United States</name>
        <sort-name>United States</sort-name>
      </area>
      <life-span>
        <ended>false</ended>
      </life-span>
    </artist>
    <artist id="6c843bf3-a854-47ea-b1cf-ba40675710b7" type="Group" ext:score="61">
      <name>Fred Sherry String Quartet</name>
      <sort-name>Fred Sherry String Quartet</sort-name>
      <country>US</country>
      <area id="489ce91b-6658-3307-9877-795b68554c98">
        <name>United States</name>
        <sort-name>United States</sort-name>
      </area>
      <life-span>
        <ended>false</ended>
      </life-span>
    </artist>
    <artist id="be136e58-ccf5-47f0-a000-6e8d7eb3bae0" type="Group" ext:score="61">
      <name>The Color Fred</name>
      <sort-name>Color Fred, The</sort-name>
      <country>US</country>
      <area id="489ce91b-6658-3307-9877-795b68554c98">
        <name>United States</name>
        <sort-name>United States</sort-name>
      </area>
      <life-span>
        <begin>2003</begin>
        <ended>false</ended>
      </life-span>
    </artist>
    <artist id="2cea1456-daef-4bb1-b63c-4b6df03ebbd5" type="Group" ext:score="61">
      <name>The Fred Hersch Group</name>
      <sort-name>Hersch, Fred, The, Group</sort-name>
      <country>US</country>
      <area id="489ce91b-6658-3307-9877-795b68554c98">
        <name>United States</name>
        <sort-name>United States</sort-name>
      </area>
      <life-span>
        <ended>false</ended>
      </life-span>
    </artist>
    <artist id="28b020fa-d9b8-4d92-805c-54cd1b0197b7" type="Group" ext:score="61">
      <name>Jacob Fred Jazz Odyssey</name>
      <sort-name>Jacob Fred Jazz Odyssey</sort-name>
      <country>US</country>
      <area id="489ce91b-6658-3307-9877-795b68554c98">
        <name>United States</name>
        <sort-name>United States</sort-name>
      </area>
      <life-span>
        <begin>1994</begin>
        <ended>false</ended>
      </life-span>
      <alias-list>
        <alias sort-name="JFJO">JFJO</alias>
      </alias-list>
      <tag-list>
        <tag count="1">
          <name>jazz</name>
        </tag>
        <tag count="1">
          <name>jazz fusion</name>
        </tag>
        <tag count="1">
          <name>free jazz</name>
        </tag>
        <tag count="1">
          <name>oklahoma</name>
        </tag>
        <tag count="1">
          <name>us</name>
        </tag>
        <tag count="1">
          <name>tulsa</name>
        </tag>
      </tag-list>
    </artist>
    <artist id="4a903390-4381-4c32-90d0-803bd4f9ce34" type="Group" ext:score="56">
      <name>Fred Waring & His Pennsylvanians</name>
      <sort-name>Waring, Fred & His Pennsylvanians</sort-name>
      <country>US</country>
      <area id="489ce91b-6658-3307-9877-795b68554c98">
        <name>United States</name>
        <sort-name>United States</sort-name>
      </area>
      <life-span>
        <ended>false</ended>
      </life-span>
      <alias-list>
        <alias sort-name="Fred Waring and the Pennsylvanians">Fred Waring and the Pennsylvanians</alias>
        <alias sort-name="Fred Waring & Pennsylvanians">Fred Waring & Pennsylvanians</alias>
        <alias sort-name="Fred Waring's Pennsylvanians">Fred Waring's Pennsylvanians</alias>
        <alias sort-name="Fred Waring's Pennyslvanians">Fred Waring's Pennyslvanians</alias>
        <alias sort-name="Fred Waring & the Pennsylvanians">Fred Waring & the Pennsylvanians</alias>
        <alias sort-name="Waring, Fred & Pennsylvanians, The">Fred Waring & The Pennsylvanians</alias>
        <alias sort-name="Fred Warring & His Pennsylvanians">Fred Warring & His Pennsylvanians</alias>
        <alias sort-name="Waring's Pensylvanians">Waring's Pensylvanians</alias>
      </alias-list>
    </artist>
    <artist id="0980008b-f9cd-412e-b062-84212b756f4a" type="Group" ext:score="51">
      <name>Fred Williams & The Jewels Band</name>
      <sort-name>Williams, Fred & Jewels Band, The</sort-name>
      <country>US</country>
      <area id="489ce91b-6658-3307-9877-795b68554c98">
        <name>United States</name>
        <sort-name>United States</sort-name>
      </area>
      <life-span>
        <ended>false</ended>
      </life-span>
    </artist>
    <artist id="985bdbe7-ebf2-42b1-8ad6-5bb619385e67" type="Group" ext:score="51">
      <name>Fred Wesley and The J.B.’s</name>
      <sort-name>Wesley, Fred and J.B.’s, the</sort-name>
      <country>US</country>
      <area id="489ce91b-6658-3307-9877-795b68554c98">
        <name>United States</name>
        <sort-name>United States</sort-name>
      </area>
      <life-span>
        <ended>false</ended>
      </life-span>
      <alias-list>
        <alias sort-name="Fred and the New J.B.'s">Fred and the New J.B.'s</alias>
        <alias sort-name="Fred Wesley & The J.B.'s">Fred Wesley & The J.B.'s</alias>
        <alias sort-name="Fred Wesley & The JB's">Fred Wesley & The JB's</alias>
        <alias sort-name="Fred Wesley & The New J.B.'s">Fred Wesley & The New J.B.'s</alias>
        <alias sort-name="Fred Wesley & The New JB's">Fred Wesley & The New JB's</alias>
      </alias-list>
    </artist>
    <artist id="05cfb5c7-0152-41f4-a9c9-622e8f710dfa" type="Group" ext:score="51">
      <name>John Fred & His Playboy Band</name>
      <sort-name>Fred, John & His Playboy Band</sort-name>
      <country>US</country>
      <area id="489ce91b-6658-3307-9877-795b68554c98">
        <name>United States</name>
        <sort-name>United States</sort-name>
      </area>
      <begin-area id="34f02dc4-3173-4c68-86d1-c82504759342">
        <name>Baton Rouge</name>
        <sort-name>Baton Rouge</sort-name>
      </begin-area>
      <life-span>
        <begin>1956</begin>
        <end>1969</end>
        <ended>true</ended>
      </life-span>
      <alias-list>
        <alias sort-name="John Fred">John Fred</alias>
        <alias sort-name="John Fred and His Playboyband">John Fred and His Playboyband</alias>
        <alias sort-name="John Fred and His Play Boy Band">John Fred and His Play Boy Band</alias>
        <alias sort-name="John Fred And His Playboy Band">John Fred And His Playboy Band</alias>
        <alias sort-name="John Fred and His Playboys">John Fred and His Playboys</alias>
        <alias sort-name="John Fred and the Play Boy Band">John Fred and the Play Boy Band</alias>
        <alias sort-name="John Fred and the Playboys">John Fred and the Playboys</alias>
        <alias sort-name="John Fred & His Playboyband">John Fred & His Playboyband</alias>
        <alias sort-name="John Fred & His Playboys">John Fred & His Playboys</alias>
        <alias sort-name="John Fred & Hiss Playboy Band">John Fred & Hiss Playboy Band</alias>
        <alias sort-name="John Fred & Playboyband">John Fred & Playboyband</alias>
        <alias sort-name="John Fred & Playboy Band">John Fred & Playboy Band</alias>
        <alias sort-name="John Fred & the Play Boy Band">John Fred & the Play Boy Band</alias>
        <alias sort-name="John Fred & The Playboyband">John Fred & The Playboyband</alias>
        <alias sort-name="John Fred & The Playboy Band">John Fred & The Playboy Band</alias>
        <alias sort-name="John Fred & The Playboys">John Fred & The Playboys</alias>
      </alias-list>
    </artist>
  </artist-list>
</metadata>
```

#### Json

```
{
  "created": "2017-03-12T16:54:57.165Z",
  "count": 11,
  "offset": 0,
  "artists": [
    {
      "id": "e56fd97e-c18f-4e5e-9b4d-f9fc21b4973f",
      "type": "Group",
      "score": "100",
      "name": "Fred",
      "sort-name": "Fred",
      "country": "US",
      "area": {
        "id": "489ce91b-6658-3307-9877-795b68554c98",
        "name": "United States",
        "sort-name": "United States"
      },
      "begin-area": {
        "id": "489ce91b-6658-3307-9877-795b68554c98",
        "name": "United States",
        "sort-name": "United States"
      },
      "disambiguation": "US progressive rock band",
      "life-span": {
        "begin": "1969",
        "end": "1974",
        "ended": true
      }
    },
    {
      "id": "4a024fd4-305e-4fea-9d3f-4ec858766e6e",
      "type": "Group",
      "score": "100",
      "name": "Fred",
      "sort-name": "Fred",
      "country": "US",
      "area": {
        "id": "489ce91b-6658-3307-9877-795b68554c98",
        "name": "United States",
        "sort-name": "United States"
      },
      "disambiguation": "US comic barbershop quartet",
      "life-span": {
        "ended": null
      }
    },
    {
      "id": "1065e6e0-b6b3-4224-9121-4502e52b9f9e",
      "type": "Group",
      "score": "61",
      "name": "A Halo Called Fred",
      "sort-name": "A Halo Called Fred",
      "country": "US",
      "area": {
        "id": "489ce91b-6658-3307-9877-795b68554c98",
        "name": "United States",
        "sort-name": "United States"
      },
      "life-span": {
        "ended": null
      }
    },
    {
      "id": "6c843bf3-a854-47ea-b1cf-ba40675710b7",
      "type": "Group",
      "score": "61",
      "name": "Fred Sherry String Quartet",
      "sort-name": "Fred Sherry String Quartet",
      "country": "US",
      "area": {
        "id": "489ce91b-6658-3307-9877-795b68554c98",
        "name": "United States",
        "sort-name": "United States"
      },
      "life-span": {
        "ended": null
      }
    },
    {
      "id": "be136e58-ccf5-47f0-a000-6e8d7eb3bae0",
      "type": "Group",
      "score": "61",
      "name": "The Color Fred",
      "sort-name": "Color Fred, The",
      "country": "US",
      "area": {
        "id": "489ce91b-6658-3307-9877-795b68554c98",
        "name": "United States",
        "sort-name": "United States"
      },
      "life-span": {
        "begin": "2003",
        "ended": null
      }
    },
    {
      "id": "2cea1456-daef-4bb1-b63c-4b6df03ebbd5",
      "type": "Group",
      "score": "61",
      "name": "The Fred Hersch Group",
      "sort-name": "Hersch, Fred, The, Group",
      "country": "US",
      "area": {
        "id": "489ce91b-6658-3307-9877-795b68554c98",
        "name": "United States",
        "sort-name": "United States"
      },
      "life-span": {
        "ended": null
      }
    },
    {
      "id": "28b020fa-d9b8-4d92-805c-54cd1b0197b7",
      "type": "Group",
      "score": "61",
      "name": "Jacob Fred Jazz Odyssey",
      "sort-name": "Jacob Fred Jazz Odyssey",
      "country": "US",
      "area": {
        "id": "489ce91b-6658-3307-9877-795b68554c98",
        "name": "United States",
        "sort-name": "United States"
      },
      "life-span": {
        "begin": "1994",
        "ended": null
      },
      "aliases": [
        {
          "sort-name": "JFJO",
          "name": "JFJO",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        }
      ],
      "tags": [
        {
          "count": 1,
          "name": "jazz"
        },
        {
          "count": 1,
          "name": "jazz fusion"
        },
        {
          "count": 1,
          "name": "free jazz"
        },
        {
          "count": 1,
          "name": "oklahoma"
        },
        {
          "count": 1,
          "name": "us"
        },
        {
          "count": 1,
          "name": "tulsa"
        }
      ]
    },
    {
      "id": "4a903390-4381-4c32-90d0-803bd4f9ce34",
      "type": "Group",
      "score": "56",
      "name": "Fred Waring & His Pennsylvanians",
      "sort-name": "Waring, Fred & His Pennsylvanians",
      "country": "US",
      "area": {
        "id": "489ce91b-6658-3307-9877-795b68554c98",
        "name": "United States",
        "sort-name": "United States"
      },
      "life-span": {
        "ended": null
      },
      "aliases": [
        {
          "sort-name": "Fred Waring and the Pennsylvanians",
          "name": "Fred Waring and the Pennsylvanians",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Fred Waring & Pennsylvanians",
          "name": "Fred Waring & Pennsylvanians",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Fred Waring's Pennsylvanians",
          "name": "Fred Waring's Pennsylvanians",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Fred Waring's Pennyslvanians",
          "name": "Fred Waring's Pennyslvanians",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Fred Waring & the Pennsylvanians",
          "name": "Fred Waring & the Pennsylvanians",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Waring, Fred & Pennsylvanians, The",
          "name": "Fred Waring & The Pennsylvanians",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Fred Warring & His Pennsylvanians",
          "name": "Fred Warring & His Pennsylvanians",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Waring's Pensylvanians",
          "name": "Waring's Pensylvanians",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        }
      ]
    },
    {
      "id": "0980008b-f9cd-412e-b062-84212b756f4a",
      "type": "Group",
      "score": "51",
      "name": "Fred Williams & The Jewels Band",
      "sort-name": "Williams, Fred & Jewels Band, The",
      "country": "US",
      "area": {
        "id": "489ce91b-6658-3307-9877-795b68554c98",
        "name": "United States",
        "sort-name": "United States"
      },
      "life-span": {
        "ended": null
      }
    },
    {
      "id": "985bdbe7-ebf2-42b1-8ad6-5bb619385e67",
      "type": "Group",
      "score": "51",
      "name": "Fred Wesley and The J.B.’s",
      "sort-name": "Wesley, Fred and J.B.’s, the",
      "country": "US",
      "area": {
        "id": "489ce91b-6658-3307-9877-795b68554c98",
        "name": "United States",
        "sort-name": "United States"
      },
      "life-span": {
        "ended": null
      },
      "aliases": [
        {
          "sort-name": "Fred and the New J.B.'s",
          "name": "Fred and the New J.B.'s",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Fred Wesley & The J.B.'s",
          "name": "Fred Wesley & The J.B.'s",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Fred Wesley & The JB's",
          "name": "Fred Wesley & The JB's",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Fred Wesley & The New J.B.'s",
          "name": "Fred Wesley & The New J.B.'s",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Fred Wesley & The New JB's",
          "name": "Fred Wesley & The New JB's",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        }
      ]
    },
    {
      "id": "05cfb5c7-0152-41f4-a9c9-622e8f710dfa",
      "type": "Group",
      "score": "51",
      "name": "John Fred & His Playboy Band",
      "sort-name": "Fred, John & His Playboy Band",
      "country": "US",
      "area": {
        "id": "489ce91b-6658-3307-9877-795b68554c98",
        "name": "United States",
        "sort-name": "United States"
      },
      "begin-area": {
        "id": "34f02dc4-3173-4c68-86d1-c82504759342",
        "name": "Baton Rouge",
        "sort-name": "Baton Rouge"
      },
      "life-span": {
        "begin": "1956",
        "end": "1969",
        "ended": true
      },
      "aliases": [
        {
          "sort-name": "John Fred",
          "name": "John Fred",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred and His Playboyband",
          "name": "John Fred and His Playboyband",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred and His Play Boy Band",
          "name": "John Fred and His Play Boy Band",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred And His Playboy Band",
          "name": "John Fred And His Playboy Band",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred and His Playboys",
          "name": "John Fred and His Playboys",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred and the Play Boy Band",
          "name": "John Fred and the Play Boy Band",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred and the Playboys",
          "name": "John Fred and the Playboys",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred & His Playboyband",
          "name": "John Fred & His Playboyband",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred & His Playboys",
          "name": "John Fred & His Playboys",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred & Hiss Playboy Band",
          "name": "John Fred & Hiss Playboy Band",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred & Playboyband",
          "name": "John Fred & Playboyband",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred & Playboy Band",
          "name": "John Fred & Playboy Band",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred & the Play Boy Band",
          "name": "John Fred & the Play Boy Band",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred & The Playboyband",
          "name": "John Fred & The Playboyband",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred & The Playboy Band",
          "name": "John Fred & The Playboy Band",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "John Fred & The Playboys",
          "name": "John Fred & The Playboys",
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

