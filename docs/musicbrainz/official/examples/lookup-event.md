Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

### Event

For events, you'll generally want to include at least artist-rels (for performers) and place-rels (for location), as in the example below.

Request: [https://musicbrainz.org/ws/2/event/fe39727a-3d21-4066-9345-3970cbd6cca4?inc=aliases+artist-rels+place-rels](https://musicbrainz.org/ws/2/event/fe39727a-3d21-4066-9345-3970cbd6cca4?inc=aliases+artist-rels+place-rels)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <event id="fe39727a-3d21-4066-9345-3970cbd6cca4" type="Concert" type-id="ef55e8d7-3d00-394a-8012-f5506a29ff0b">
    <name>Nine Inch Nails at Arena Riga</name>
    <life-span>
      <begin>2014-05-06</begin>
      <end>2014-05-06</end>
    </life-span>
    <time>19:00</time>
    <setlist>* Copy of A * 1,000,000 * Terrible Lie * March of the Pigs * Piggy * The Frail * The Wretched * The Becoming * Gave Up * Closer * Find My Way * Me, I'm Not * Came Back Haunted * The Great Destroyer * Eraser * Wish * Burn * The Hand That Feeds * Head Like a Hole # Encore * Hurt</setlist>
    <relation-list target-type="artist">
      <relation type="main performer" type-id="936c7c95-3156-3889-a062-8a0cd57f8946">
        <target>b7ffd2af-418f-4be2-bdd1-22f8b48613da</target>
        <direction>backward</direction>
        <artist id="b7ffd2af-418f-4be2-bdd1-22f8b48613da" type="Group" type-id="e431f5f6-b5d2-343d-8b36-72607fffb74b">
          <name>Nine Inch Nails</name>
          <sort-name>Nine Inch Nails</sort-name>
        </artist>
      </relation>
      <relation type="support act" type-id="492a850e-97eb-306a-a85e-4b6d98527796">
        <target>f7f32d93-0801-45cb-9f5a-e68f640649f4</target>
        <direction>backward</direction>
        <artist id="f7f32d93-0801-45cb-9f5a-e68f640649f4" type="Group" type-id="e431f5f6-b5d2-343d-8b36-72607fffb74b">
          <name>Cold Cave</name>
          <sort-name>Cold Cave</sort-name>
        </artist>
      </relation>
    </relation-list>
    <relation-list target-type="place">
      <relation type="held at" type-id="e2c6f697-07dc-38b1-be0b-83d740165532">
        <target>478558f9-a951-4067-ad91-e83f6ba63e74</target>
        <direction>forward</direction>
        <place id="478558f9-a951-4067-ad91-e83f6ba63e74" type="Indoor arena" type-id="a77c11f6-82fa-3cc0-9041-ac60e5f6e024">
          <name>Arēna Rīga</name>
          <address>Skanstes iela 21, Rīga, Latvia</address>
          <coordinates>
            <latitude>56.967989</latitude>
            <longitude>24.121403</longitude>
          </coordinates>
        </place>
      </relation>
    </relation-list>
  </event>
</metadata>

```

JSON Response

```
  {
    id: "fe39727a-3d21-4066-9345-3970cbd6cca4",
    name: "Nine Inch Nails at Arena Riga",
    disambiguation: "",
    type-id: "ef55e8d7-3d00-394a-8012-f5506a29ff0b",
    type: "Concert",
    life-span: {
      end: "2014-05-06",
      ended: true,
      begin: "2014-05-06"
    },
    time: "19: 00",
    cancelled: false,
    setlist: "* Copy of A * 1,000,000 * Terrible Lie * March of the Pigs * Piggy * The Frail * The Wretched * The Becoming * Gave Up * Closer * Find My Way * Me, I'm Not * Came Back Haunted * The Great Destroyer * Eraser * Wish * Burn * The Hand That Feeds * Head Like a Hole # Encore * Hurt",
    relations: [
      {
        type-id: "936c7c95-3156-3889-a062-8a0cd57f8946",
        type: "main performer",
        direction: "backward",
        target-type: "artist",
        artist: {
          id: "b7ffd2af-418f-4be2-bdd1-22f8b48613da",
          name: "Nine Inch Nails",
          sort-name: "Nine Inch Nails",
          disambiguation: ""
        },
        begin: null,
        end: null,
        ended: false,
        target-credit: "",
        source-credit: "",
        attributes: [ ],
        attribute-ids: { },
        attribute-values: { }
      },
      {
        type-id: "492a850e-97eb-306a-a85e-4b6d98527796",
        type: "support act",
        direction: "backward",
        target-type: "artist",
        artist: {
          id: "f7f32d93-0801-45cb-9f5a-e68f640649f4",
          name: "Cold Cave",
          sort-name: "Cold Cave",
          disambiguation: ""
        },
        begin: null,
        end: null,
        ended: false,
        target-credit: "",
        source-credit: "",
        attributes: [ ],
        attribute-ids: { },
        attribute-values: { }
      },
      {
        type-id: "e2c6f697-07dc-38b1-be0b-83d740165532",
        type: "held at",
        direction: "forward",
        target-type: "place",
        place: {
          id: "478558f9-a951-4067-ad91-e83f6ba63e74",
          name: "Arēna Rīga",
          address: "Skanstes iela 21, Rīga, Latvia",
          coordinates: {
            longitude: 24.121403,
            latitude: 56.967989
          },
          disambiguation: "",
          type-id: "a77c11f6-82fa-3cc0-9041-ac60e5f6e024",
          type: "Indoor arena",
          area: {
            id: "9c612199-d66f-4109-aedc-67ab26e0a43b",
            name: "Rīga",
            sort-name: "Rīga",
            disambiguation: "",
            iso-3166-2-codes: ["LV-RIX"]
          }
        },
        begin: null,
        end: null,
        ended: false,
        target-credit: "",
        source-credit: "",
        attributes: [ ],
        attribute-ids: { },
        attribute-values: { }
      }
    ],
    aliases: [ ]
  }
```

