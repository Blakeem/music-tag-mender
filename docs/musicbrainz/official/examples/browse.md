Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

## Browse requests

Request: [http://musicbrainz.org/ws/2/release?label=47e718e1-7ee4-460c-b1cc-1192a841c6e5&offset=12&limit=2](http://musicbrainz.org/ws/2/release?label=47e718e1-7ee4-460c-b1cc-1192a841c6e5&offset=12&limit=2)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <release-list count="106" offset="12">
    <release id="6283480c-5004-4da7-993c-69fd7d2f0681">
      <title>Autumntunes</title>
      /* some properties omitted to keep this example shorter, see the release results for the full format */
    </release>
    <release id="26598ff2-4719-4d26-a3e5-5ccd77119455">
      <title>Around Past</title>
      /* some properties omitted to keep this example shorter, see the release results for the full format */
    </release>
  </release-list>
</metadata>
```

JSON Response

```
  {
    releases: [
      {
        id: "26598ff2-4719-4d26-a3e5-5ccd77119455",
        title: "Around Past"
        /* some properties omitted to keep this example shorter, see the release results for the full format */
      },
      {
        id: "6283480c-5004-4da7-993c-69fd7d2f0681",
        title: "Autumntunes"
        /* some properties omitted to keep this example shorter, see the release results for the full format */
      }
    ],
    release-offset: 12,
    release-count: 106
  }
```

