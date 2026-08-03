Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

### Work

For works, you'll generally want to include at least artist-rels (for writers), as in the example below.

Request: [http://musicbrainz.org/ws/2/work/b1df2cf3-69a9-3bc0-be44-f71e79b27a22?inc=artist-rels](http://musicbrainz.org/ws/2/work/b1df2cf3-69a9-3bc0-be44-f71e79b27a22?inc=artist-rels)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <work id="b1df2cf3-69a9-3bc0-be44-f71e79b27a22" type="Song" type-id="f061270a-2fd6-32f1-a641-f0f8676d14e6">
    <title>HELLO! また会おうね</title>
    <language>jpn</language>
    <language-list>
      <language>jpn</language>
    </language-list>
    <iswc>T-101.690.320-9</iswc>
    <iswc-list>
      <iswc>T-101.690.320-9</iswc>
    </iswc-list>
    <attribute-list>
      <attribute type="JASRAC ID" type-id="31048fcc-3dbb-3979-8f85-805afb933e0c">089-5005-9</attribute>
    </attribute-list>
    <relation-list target-type="artist">
      <relation type="composer" type-id="d59d99ea-23d4-4a80-b066-edca32ee158f">
        <target>d997d399-355e-4c49-9c7b-75a93d76bc0e</target>
        <direction>backward</direction>
        <artist id="d997d399-355e-4c49-9c7b-75a93d76bc0e" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
          <name>つんく♂</name>
          <sort-name>Tsunku</sort-name>
        </artist>
      </relation>
      <relation type="lyricist" type-id="3e48faba-ec01-47fd-8e89-30e81161661c">
        <target>d997d399-355e-4c49-9c7b-75a93d76bc0e</target>
        <direction>backward</direction>
        <artist id="d997d399-355e-4c49-9c7b-75a93d76bc0e" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
          <name>つんく♂</name>
          <sort-name>Tsunku</sort-name>
        </artist>
      </relation>
    </relation-list>
  </work>
</metadata>

```

JSON Response

```
  {
    id: "b1df2cf3-69a9-3bc0-be44-f71e79b27a22",
    title: "HELLO! また会おうね",
    type-id: "f061270a-2fd6-32f1-a641-f0f8676d14e6"
    type: "Song",
    disambiguation: "",
    iswcs: ["T-101.690.320-9"],
    languages: ["jpn"],
    attributes: [
      {
        type-id: "31048fcc-3dbb-3979-8f85-805afb933e0c",
        type: "JASRAC ID",
        value: "089-5005-9"
      }
    ],
    relations: [
      {
        type-id: "d59d99ea-23d4-4a80-b066-edca32ee158f",
        type: "composer",
        direction: "backward",
        target-type: "artist",
        artist: {
          id: "d997d399-355e-4c49-9c7b-75a93d76bc0e",
          name: "つんく♂",
          sort-name: "Tsunku",
          disambiguation: ""
        },
        source-credit: "",
        target-credit: "",
        attributes: [ ],
        attribute-ids: { },
        attribute-values: { },
        begin: null,
        end: null,
        ended: false
      },
        type-id: "3e48faba-ec01-47fd-8e89-30e81161661c",
        type: "lyricist",
        direction: "backward",
        target-type: "artist",
        artist: {
          id: "d997d399-355e-4c49-9c7b-75a93d76bc0e",
          name: "つんく♂",
          sort-name: "Tsunku",
          disambiguation: ""
        },
        source-credit: "",
        target-credit: "",
        attributes: [ ],
        attribute-ids: { },
        attribute-values: { },
        begin: null,
        end: null,
        ended: false
      }
    ]
  }
```

