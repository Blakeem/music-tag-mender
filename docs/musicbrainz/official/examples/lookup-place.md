Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

### Place

Request: [https://musicbrainz.org/ws/2/place/478558f9-a951-4067-ad91-e83f6ba63e74?inc=aliases](https://musicbrainz.org/ws/2/place/478558f9-a951-4067-ad91-e83f6ba63e74?inc=aliases)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <place id="478558f9-a951-4067-ad91-e83f6ba63e74" type="Indoor arena" type-id="a77c11f6-82fa-3cc0-9041-ac60e5f6e024">
    <name>Arēna Rīga</name>
    <address>Skanstes iela 21, Rīga, Latvia</address>
    <coordinates>
      <latitude>56.967989</latitude>
      <longitude>24.121403</longitude>
    </coordinates>
    <area id="9c612199-d66f-4109-aedc-67ab26e0a43b">
      <name>Rīga</name>
      <sort-name>Rīga</sort-name>
      <iso-3166-2-code-list>
        <iso-3166-2-code>LV-RIX</iso-3166-2-code>
      </iso-3166-2-code-list>
    </area>
    <life-span>
      <begin>2006-02-15</begin>
    </life-span>
    <alias-list count="2">
      <alias locale="en" sort-name="Arena Riga" type="Place name" type-id="fb68f9a2-622c-319b-83b0-bbff4127cdc5" primary="primary">Arena Riga</alias>
      <alias locale="lv" sort-name="Arēna Rīga" type="Place name" type-id="fb68f9a2-622c-319b-83b0-bbff4127cdc5" primary="primary">Arēna Rīga</alias>
    </alias-list>
  </place>
</metadata>

```

JSON Response

```
  {
    id: "478558f9-a951-4067-ad91-e83f6ba63e74",
    name: "Arēna Rīga",
    disambiguation: "",
    type-id: "a77c11f6-82fa-3cc0-9041-ac60e5f6e024",
    type: "Indoor arena",
    address: "Skanstes iela 21, Rīga, Latvia",
    life-span: {
      begin: "2006-02-15",
      end: null,
      ended: false
    },
    coordinates: {
      longitude: 24.121403,
      latitude: 56.967989
    },
    aliases: [
      {
        name: "Arena Riga",
        sort-name: "Arena Riga",
        type-id: "fb68f9a2-622c-319b-83b0-bbff4127cdc5",
        type: "Place name",
        locale: "en",
        primary: true,
        begin: null,
        end: null,
        ended: false
      }
    ],
    area: {
      id: "9c612199-d66f-4109-aedc-67ab26e0a43b",
      name: "Rīga",
      sort-name: "Rīga",
      disambiguation: "",
      iso-3166-2-codes: ["LV-RIX"]
    },
  }
```

