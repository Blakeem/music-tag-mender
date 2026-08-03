Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

### Genre

Request: [http://musicbrainz.org/ws/2/genre/f66d7266-eb3d-4ef3-b4d8-b7cd992f918b](http://musicbrainz.org/ws/2/genre/f66d7266-eb3d-4ef3-b4d8-b7cd992f918b)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <genre id="f66d7266-eb3d-4ef3-b4d8-b7cd992f918b">
    <name>crust punk</name>
  </genre>
</metadata>
```

JSON Response

```
  {
     id: "f66d7266-eb3d-4ef3-b4d8-b7cd992f918b"
     name: "crust punk",
     disambiguation: "",
  }
```

