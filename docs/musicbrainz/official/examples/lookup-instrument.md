Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

### Instrument

Request: [https://musicbrainz.org/ws/2/instrument/dd430e7f-36ba-49a5-825b-80a525e69190?inc=aliases](https://musicbrainz.org/ws/2/instrument/dd430e7f-36ba-49a5-825b-80a525e69190?inc=aliases)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <instrument id="dd430e7f-36ba-49a5-825b-80a525e69190" type="Percussion instrument" type-id="68fa2525-4c23-3386-bb81-e84994342e9a">
    <name>kemanak</name>
    <disambiguation>Banana shaped bronze slit-drum used in javanese gamelan</disambiguation>
    <description>Bent into a ladle or banana like shape, it is a pair of bronze slit-drums hit with tabuh beaters.</description>
    <alias-list count="12">
      <alias locale="de" sort-name="Kemanak" type="Instrument name" type-id="2322fc94-fbf3-3c09-b23c-aa5ec8d14fcd" primary="primary">Kemanak</alias>
      <alias sort-name="gumanak" type="Instrument name" type-id="2322fc94-fbf3-3c09-b23c-aa5ec8d14fcd">gumanak</alias>
      <alias sort-name="kenawak" type="Instrument name" type-id="2322fc94-fbf3-3c09-b23c-aa5ec8d14fcd">kenawak</alias>
    </alias-list>
  </instrument>
</metadata>
```

JSON Response

```
  {
    id: "dd430e7f-36ba-49a5-825b-80a525e69190",
    name: "kemanak",
    disambiguation: "Banana shaped bronze slit-drum used in javanese gamelan",
    description: "Bent into a ladle or banana like shape, it is a pair of bronze slit-drums hit with tabuh beaters.",
    type-id: "68fa2525-4c23-3386-bb81-e84994342e9a",
    type: "Percussion instrument",
    aliases: [
      {
        name: "gumanak",
        sort-name: "gumanak",
        type-id: "2322fc94-fbf3-3c09-b23c-aa5ec8d14fcd",
        type: "Instrument name",
        locale: null,
        primary: null,
        begin: null,
        end: null,
        ended: false
      },
      {
        name: "kenawak",
        sort-name: "kenawak",
        type-id: "2322fc94-fbf3-3c09-b23c-aa5ec8d14fcd",
        type: "Instrument name",
        locale: null,
        primary: null,
        begin: null,
        end: null,
        ended: false
      }
    ]
  }
```

