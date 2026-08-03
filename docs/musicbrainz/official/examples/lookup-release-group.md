Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

### Release Group

Request: [http://musicbrainz.org/ws/2/release-group/c9fdb94c-4975-4ed6-a96f-ef6d80bb7738?inc=artist-credits+releases](http://musicbrainz.org/ws/2/release-group/c9fdb94c-4975-4ed6-a96f-ef6d80bb7738?inc=artist-credits+releases)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <release-group id="c9fdb94c-4975-4ed6-a96f-ef6d80bb7738" type="Album" type-id="f529b476-6e62-324f-b0aa-1f3e33d313fc">
    <title>The Lost Tape</title>
    <first-release-date>2012-05-22</first-release-date>
    <primary-type id="f529b476-6e62-324f-b0aa-1f3e33d313fc">Album</primary-type>
    <secondary-type-list>
      <secondary-type id="15c1b1f5-d893-3375-a1db-e180c5ae15ed">Mixtape/Street</secondary-type>
    </secondary-type-list>
    <artist-credit>
      <name-credit>
        <artist id="8e68819d-71be-4e7d-b41d-f1df81b01d3f" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
          <name>50 Cent</name>
          <sort-name>50 Cent</sort-name>
        </artist>
      </name-credit>
    </artist-credit>
    <release-list count="1">
      <release id="2ec84eb6-ab92-4ac3-9720-32ad84c34f11">
        <title>The Lost Tape</title>
        /* some properties omitted to keep this example shorter, see the release results for the full format */
      </release>
    </release-list>
  </release-group>
</metadata>
```

JSON Response

```
 {
     id: "c9fdb94c-4975-4ed6-a96f-ef6d80bb7738",
     title: "The Lost Tape",
     first-release-date: "2012-05-22",
     artist-credit: [
         "name": "50 Cent",
         "joinphrase": "",
         "artist": {
             "id": "8e68819d-71be-4e7d-b41d-f1df81b01d3f",
             "name": "50 Cent",
             "sort-name": "50 Cent",
             "disambiguation": ""
         }
     ],
     disambiguation: null,
     primary-type-id: "f529b476-6e62-324f-b0aa-1f3e33d313fc",
     primary-type: "Album",
     secondary-type-ids: ["15c1b1f5-d893-3375-a1db-e180c5ae15ed"]
     secondary-types: [ "Mixtape/Street" ],
     releases: [
         {
             "id": "2ec84eb6-ab92-4ac3-9720-32ad84c34f11",
             "title": "The Lost Tape",
             /* some properties omitted to keep this example shorter, see the release results for the full format */
         }
     ]
  }

```

