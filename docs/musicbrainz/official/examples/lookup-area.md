Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

### Area

Request: [https://musicbrainz.org/ws/2/area/45f07934-675a-46d6-a577-6f8637a411b1?inc=aliases](https://musicbrainz.org/ws/2/area/45f07934-675a-46d6-a577-6f8637a411b1?inc=aliases)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <area id="45f07934-675a-46d6-a577-6f8637a411b1" type="City" type-id="6fd8f29a-3d0a-32fc-980d-ea697b69da78">
    <name>Pärnu</name>
    <sort-name>Pärnu</sort-name>
    <alias-list count="2">
      <alias locale="en" sort-name="Pärnu" type="Area name" type-id="0b5b3497-d5d9-34e7-a61b-9a6c18aa7b29" primary="primary">Pärnu</alias>
      <alias locale="et" sort-name="Pärnu" type="Area name" type-id="0b5b3497-d5d9-34e7-a61b-9a6c18aa7b29" primary="primary">Pärnu</alias>
    </alias-list>
  </area>
</metadata>
```

JSON Response

```
  {
    id: "45f07934-675a-46d6-a577-6f8637a411b1",
    name: "Pärnu",
    type-id: "6fd8f29a-3d0a-32fc-980d-ea697b69da78",
    type: "City",
    disambiguation: "",
    life-span: {
      begin: null,
      end: null,
      ended: false
    },
    aliases: [
      {
        name: "Pärnu",
        sort-name: "Pärnu",
        type-id: "0b5b3497-d5d9-34e7-a61b-9a6c18aa7b29",
        type: "Area name",
        locale: "en",
        primary: true,
        begin: null,
        end: null,
        ended: false
      },
      {
        name: "Pärnu",
        sort-name: "Pärnu",
        type-id: "0b5b3497-d5d9-34e7-a61b-9a6c18aa7b29",
        type: "Area name",
        locale: "et",
        primary: true,
        begin: null,
        end: null,
        ended: false
      }
    ]
  }
```

