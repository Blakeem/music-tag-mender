Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

### Label

Request: [http://musicbrainz.org/ws/2/label/46f0f4cd-8aab-4b33-b698-f459faf64190?inc=aliases](http://musicbrainz.org/ws/2/label/46f0f4cd-8aab-4b33-b698-f459faf64190?inc=aliases)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <label id="46f0f4cd-8aab-4b33-b698-f459faf64190" type="Original Production" type-id="7aaa37fe-2def-3476-b359-80245850062d">
    <name>Warp</name>
    <sort-name>Warp</sort-name>
    <disambiguation>UK independent label</disambiguation>
    <label-code>2070</label-code>
    <isni-list>
      <isni>0000000107280584</isni>
    </isni-list>
    <country>GB</country>
    <area id="8a754a16-0027-3a29-b6d7-2b40ea0481ed">
      <name>United Kingdom</name>
      <sort-name>United Kingdom</sort-name>
      <iso-3166-1-code-list>
        <iso-3166-1-code>GB</iso-3166-1-code>
      </iso-3166-1-code-list>
    </area>
    <life-span>
      <begin>1989</begin>
    </life-span>
    <alias-list count="1">
      <alias locale="en_GB" sort-name="Warp Records" type="Label name" type-id="3a1a0c48-d885-3b89-87b2-9e8a483c5675">Warp Records</alias>
    </alias-list>
  </label>
</metadata>
```

JSON Response

```
  {
    id: "46f0f4cd-8aab-4b33-b698-f459faf64190",
    name: "Warp",
    disambiguation: "UK independent label",
    type-id: "7aaa37fe-2def-3476-b359-80245850062d",
    type: "Original Production",
    label-code: 2070,
    life-span: {
      begin: "1989",
      end: null,
      ended: false
    },
    aliases: [
      {
        name: "Warp Records",
        sort-name: "Warp Records",
        type-id: "3a1a0c48-d885-3b89-87b2-9e8a483c5675",
        type: "Label name",
        locale: "en_GB",
        primary: false,
        begin: null,
        end: null,
        ended: false
      }
    ],
    country: "GB",
    area: {
      id: "8a754a16-0027-3a29-b6d7-2b40ea0481ed",
      name: "United Kingdom",
      sort-name: "United Kingdom",
      disambiguation: ""
      iso-3166-1-codes: ["GB"],
    },
    ipis: [ ],
    isnis: ["0000000107280584"]
  }
```

