Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

### Recording

Request: [https://musicbrainz.org/ws/2/recording/b9ad642e-b012-41c7-b72a-42cf4911f9ff?inc=artist-credits+isrcs+releases](https://musicbrainz.org/ws/2/recording/b9ad642e-b012-41c7-b72a-42cf4911f9ff?inc=artist-credits+isrcs+releases)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <recording id="b9ad642e-b012-41c7-b72a-42cf4911f9ff">
    <title>LAST ANGEL</title>
    <length>230000</length>
    <artist-credit>
      <name-credit joinphrase=" feat. ">
        <artist id="455641ea-fff4-49f6-8fb4-49f961d8f1ac" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
          <name>倖田來未</name>
          <sort-name>Koda, Kumi</sort-name>
        </artist>
      </name-credit>
      <name-credit>
        <artist id="05cbaf37-6dc2-4f71-a0ce-d633447d90c3" type="Group" type-id="e431f5f6-b5d2-343d-8b36-72607fffb74b">
          <name>東方神起</name>
          <sort-name>Tohoshinki</sort-name>
        </artist>
      </name-credit>
    </artist-credit>
    <release-list count="13">
      <release id="c33dee6a-e053-4272-84ad-dfeef3f48c8a">
        <title>LAST ANGEL</title>
        /* some properties omitted to keep this example shorter, see the release results for the full format */
      </release>
      <release id="601a4558-e416-410b-a64c-857fc133b75c">
        <title>Kingdom</title>
        /* some properties omitted to keep this example shorter, see the release results for the full format */
      </release>
    </release-list>
    <isrc-list count="1">
      <isrc id="JPB600760301"/>
    </isrc-list>
  </recording>
</metadata>
```

JSON Response

```
  {
    id: "b9ad642e-b012-41c7-b72a-42cf4911f9ff",
    title: "LAST ANGEL",
    artist-credit: [
      {
        name: "倖田來未",
        joinphrase: " feat. ",
        artist: {
          id: "455641ea-fff4-49f6-8fb4-49f961d8f1ac",
          name: "倖田來未",
          disambiguation: "",
          sort-name: "Koda, Kumi"
        }
      },
      {
        name: "東方神起",
        joinphrase: "",
        artist: {
          id: "05cbaf37-6dc2-4f71-a0ce-d633447d90c3",
          name: "東方神起",
          disambiguation: "",
          sort-name: "Tohoshinki"
        }
      }
    ],
    disambiguation: "",
    length: 230000,
    video: false,
    isrcs: ["JPB600760301"],
    releases: [
      {
        id: "c33dee6a-e053-4272-84ad-dfeef3f48c8a",
        title: "LAST ANGEL",
        /* some properties omitted to keep this example shorter, see the release results for the full format */
      },
      {
        id: "601a4558-e416-410b-a64c-857fc133b75c",
        title: "Kingdom",
        /* some properties omitted to keep this example shorter, see the release results for the full format */
      }
    ]
  }
```

