Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

### Artist

Request: [http://musicbrainz.org/ws/2/artist/5b11f4ce-a62d-471e-81fc-a69a8278c7da?inc=aliases](http://musicbrainz.org/ws/2/artist/5b11f4ce-a62d-471e-81fc-a69a8278c7da?inc=aliases)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <artist id="5b11f4ce-a62d-471e-81fc-a69a8278c7da" type="Group" type-id="e431f5f6-b5d2-343d-8b36-72607fffb74b">
    <name>Nirvana</name>
    <sort-name>Nirvana</sort-name>
    <disambiguation>90s US grunge band</disambiguation>
    <isni-list>
      <isni>0000000123486830</isni>
      <isni>0000000123487390</isni>
    </isni-list>
    <country>US</country>
    <area id="489ce91b-6658-3307-9877-795b68554c98">
      <name>United States</name>
      <sort-name>United States</sort-name>
      <iso-3166-1-code-list>
        <iso-3166-1-code>US</iso-3166-1-code>
      </iso-3166-1-code-list>
    </area>
    <begin-area id="a640b45c-c173-49b1-8030-973603e895b5">
      <name>Aberdeen</name>
      <sort-name>Aberdeen</sort-name>
    </begin-area>
    <life-span>
      <begin>1988-01</begin>
      <end>1994-04-05</end>
      <ended>true</ended>
    </life-span>
    <alias-list count="2">
      <alias sort-name="Nirvana US">Nirvana US</alias>
      <alias locale="ja" sort-name="ニルヴァーナ" type="Artist name" type-id="894afba6-2816-3c24-8072-eadb66bd04bc" primary="primary">ニルヴァーナ</alias>
    </alias-list>
  </artist>
</metadata>
```

JSON Response

```
  {
    id: "5b11f4ce-a62d-471e-81fc-a69a8278c7da",
    name: "Nirvana",
    sort-name: "Nirvana",
    type-id: "e431f5f6-b5d2-343d-8b36-72607fffb74b",
    type: "Group",
    disambiguation: "90s US grunge band",
    gender: null,
    gender-id: null,
    country: "US",
    area: {
      disambiguation: "",
      id: "489ce91b-6658-3307-9877-795b68554c98",
      sort-name: "United States",
      name: "United States",
      iso-3166-1-codes: ["US"]
    },
    begin-area: {
      id: "a640b45c-c173-49b1-8030-973603e895b5",
      disambiguation: "",
      name: "Aberdeen",
      sort-name: "Aberdeen"
    },
    end-area: null,
    life-span: {
      ended: true,
      begin: "1988-01",
      end: "1994-04-05"
    },
    isnis: ["0000000123486830", "0000000123487390"],
    ipis: [ ],
    aliases: [
      {
        end: null,
        begin: null,
        sort-name: "Nirvana US",
        name: "Nirvana US",
        type-id: null,
        primary: null,
        locale: null,
        type: null,
        ended: false
      },
      {
        primary: true,
        type-id: "894afba6-2816-3c24-8072-eadb66bd04bc",
        name: "ニルヴァーナ",
        sort-name: "ニルヴァーナ",
        begin: null,
        end: null,
        ended: false,
        type: "Artist name",
        locale: "ja"
      }
    ]
  }
```

