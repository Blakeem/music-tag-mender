Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

### Release

Request: [http://musicbrainz.org/ws/2/release/59211ea4-ffd2-4ad9-9a4e-941d3148024a?inc=artist-credits+labels+discids+recordings](http://musicbrainz.org/ws/2/release/59211ea4-ffd2-4ad9-9a4e-941d3148024a?inc=artist-credits+labels+discids+recordings)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
    <release id="59211ea4-ffd2-4ad9-9a4e-941d3148024a">
        <title>æ³o & h³æ</title>
        <status id="4e304316-386d-3409-af2e-78857eec5cfe">Official</status>
        <quality>normal</quality>
        <text-representation>
            <language>eng</language>
            <script>Latn</script>
        </text-representation>
        <artist-credit>
            <name-credit joinphrase=" & ">
                <artist id="410c9baf-5469-44f6-9852-826524b80c61" type="Group" type-id="e431f5f6-b5d2-343d-8b36-72607fffb74b">
                    <name>Autechre</name>
                    <sort-name>Autechre</sort-name>
                    <disambiguation>English electronic music duo Rob Brown & Sean Booth</disambiguation>
                </artist>
            </name-credit>
            <name-credit>
                <artist id="146c01d0-d3a2-44c3-acb5-9208bce75e14" type="Group" type-id="e431f5f6-b5d2-343d-8b36-72607fffb74b">
                    <name>The Hafler Trio</name>
                    <sort-name>Hafler Trio, The</sort-name>
                </artist>
            </name-credit>
        </artist-credit>
        <date>2003-12-04</date>
        <country>GB</country>
        <release-event-list count="1">
            <release-event>
                <date>2003-12-04</date>
                <area id="8a754a16-0027-3a29-b6d7-2b40ea0481ed">
                    <name>United Kingdom</name>
                    <sort-name>United Kingdom</sort-name>
                    <iso-3166-1-code-list>
                        <iso-3166-1-code>GB</iso-3166-1-code>
                    </iso-3166-1-code-list>
                </area>
            </release-event>
        </release-event-list>
        <cover-art-archive>
            <artwork>true</artwork>
            <count>1</count>
            <front>true</front>
            <back>false</back>
        </cover-art-archive>
        <label-info-list count="1">
            <label-info>
                <catalog-number>pgram002</catalog-number>
                <label id="a0759efa-f583-49ea-9a8d-d5bbce55541c" type="Original Production" type-id="7aaa37fe-2def-3476-b359-80245850062d">
                    <name>Phonometrography</name>
                    <sort-name>Phonometrography</sort-name>
                </label>
            </label-info>
        </label-info-list>
        <medium-list count="2">
            <medium>
                <title>æ³o</title>
                <position>1</position>
                <format id="9712d52a-4509-3d4b-a1a2-67c88c643e31">CD</format>
                <disc-list count="1">
                    <disc id="nN2g3a0ZSjovyIgK3bJl6_.j8C4-">
                        <sectors>73241</sectors>
                        <offset-list count="1">
                            <offset position="1">150</offset>
                        </offset-list>
                    </disc>
                </disc-list>
                <track-list count="1" offset="0">
                    <track id="61af3e5a-14e0-350d-9826-a884c6e586b1">
                        <position>1</position>
                        <number>1</number>
                        <length>974546</length>
                        <recording id="af87f070-238b-46c1-aa3e-f831ab91fa20">
                            <title>æ³o</title>
                            <length>974546</length>
                            <artist-credit>
                                <name-credit joinphrase=" & ">
                                    <artist id="410c9baf-5469-44f6-9852-826524b80c61" type="Group" type-id="e431f5f6-b5d2-343d-8b36-72607fffb74b">
                                        <name>Autechre</name>
                                        <sort-name>Autechre</sort-name>
                                        <disambiguation>English electronic music duo Rob Brown & Sean Booth</disambiguation>
                                    </artist>
                                </name-credit>
                                <name-credit>
                                    <artist id="146c01d0-d3a2-44c3-acb5-9208bce75e14" type="Group" type-id="e431f5f6-b5d2-343d-8b36-72607fffb74b">
                                        <name>The Hafler Trio</name>
                                        <sort-name>Hafler Trio, The</sort-name>
                                    </artist>
                                </name-credit>
                            </artist-credit>
                        </recording>
                    </track>
                </track-list>
            </medium>
            <medium>
                <title>h³æ</title>
                <position>2</position>
                <format id="9712d52a-4509-3d4b-a1a2-67c88c643e31">CD</format>
                <disc-list count="1">
                    <disc id="aSHvkMnq2jZVFEK.DmSPbvN_f54-">
                        <sectors>69341</sectors>
                        <offset-list count="1">
                            <offset position="1">150</offset>
                        </offset-list>
                    </disc>
                </disc-list>
                <track-list count="1" offset="0">
                    <track id="5f2031a2-c67d-3bec-8ae5-8d22847ab0a5">
                        <position>1</position>
                        <number>1</number>
                        <length>922546</length>
                        <recording id="5aff6309-2e02-4a47-9233-32d7dcc9a960">
                            <title>h³æ</title>
                            <length>922546</length>
                            <artist-credit>
                                <name-credit joinphrase=" & ">
                                    <artist id="410c9baf-5469-44f6-9852-826524b80c61" type="Group" type-id="e431f5f6-b5d2-343d-8b36-72607fffb74b">
                                        <name>Autechre</name>
                                        <sort-name>Autechre</sort-name>
                                        <disambiguation>English electronic music duo Rob Brown & Sean Booth</disambiguation>
                                    </artist>
                                </name-credit>
                                <name-credit>
                                    <artist id="146c01d0-d3a2-44c3-acb5-9208bce75e14" type="Group" type-id="e431f5f6-b5d2-343d-8b36-72607fffb74b">
                                        <name>The Hafler Trio</name>
                                        <sort-name>Hafler Trio, The</sort-name>
                                    </artist>
                                </name-credit>
                            </artist-credit>
                        </recording>
                    </track>
                </track-list>
            </medium>
        </medium-list>
    </release>
</metadata>
```

JSON Response

```
  {
    id: "59211ea4-ffd2-4ad9-9a4e-941d3148024a",
    title: "æ³o & h³æ",
    disambiguation: "",
    artist-credit: [
      {
        name: "Autechre",
        joinphrase: " & ",
        artist: {
          id: "410c9baf-5469-44f6-9852-826524b80c61",
          name: "Autechre",
          sort-name: "Autechre",
          disambiguation: "English electronic music duo Rob Brown & Sean Booth"
        }
      },
      {
        name: "The Hafler Trio",
        joinphrase: "",
        artist: {
          id: "146c01d0-d3a2-44c3-acb5-9208bce75e14",
          name: "The Hafler Trio",
          sort-name: "Hafler Trio, The",
          disambiguation: ""
        }
      }
    ],
    date: "2003-12-04",
    country: "GB",
    release-events: [
      {
        date: "2003-12-04",
        area: {
          id: "8a754a16-0027-3a29-b6d7-2b40ea0481ed",
          name: "United Kingdom",
          sort-name: "United Kingdom",
          iso-3166-1-codes: ["GB"],
          disambiguation: ""
        }
      }
    ],
    label-info: [
      {
        catalog-number: "pgram002",
        label: {
          id: "a0759efa-f583-49ea-9a8d-d5bbce55541c",
          name: "Phonometrography",
          disambiguation: "",
          label-code: null
        }
      }
    ],
    barcode: null,
    packaging-id: null,
    packaging: null,
    status-id: "4e304316-386d-3409-af2e-78857eec5cfe",
    status: "Official",
    quality: "normal",
    text-representation: {
      language: "eng",
      script: "Latn"
    },
    asin: null,
    media: [
      {
        discs: [
          {
            id: "nN2g3a0ZSjovyIgK3bJl6_.j8C4-",
            sectors: 73241,
            offsets: [150],
            offset-count: 1
          }
        ],
        position: 1,
        title: "æ³o",
        format-id: "9712d52a-4509-3d4b-a1a2-67c88c643e31",
        format: "CD",
        track-count: 1,
        track-offset: 0,
        tracks: [
          {
            id: "61af3e5a-14e0-350d-9826-a884c6e586b1",
            title: "æ³o",
            length: 974546,
            number: "1",
            position: 1,
            artist-credit: [
              {
                name: "Autechre",
                joinphrase: " & ",
                artist: {
                  id: "410c9baf-5469-44f6-9852-826524b80c61",
                  name: "Autechre",
                  sort-name: "Autechre",
                  disambiguation: "English electronic music duo Rob Brown & Sean Booth"
                }
              },
              {
                name: "The Hafler Trio",
                joinphrase: "",
                artist: {
                  id: "146c01d0-d3a2-44c3-acb5-9208bce75e14",
                  name: "The Hafler Trio",
                  sort-name: "Hafler Trio, The",
                  disambiguation: ""
                }
              }
            ],
            recording: {
              id: "af87f070-238b-46c1-aa3e-f831ab91fa20",
              title: "æ³o",
              disambiguation: "",
              length: 974546,
              video: false,
              artist-credit: [
                {
                  name: "Autechre",
                  joinphrase: " & ",
                  artist: {
                    id: "410c9baf-5469-44f6-9852-826524b80c61",
                    name: "Autechre",
                    sort-name: "Autechre",
                    disambiguation: "English electronic music duo Rob Brown & Sean Booth"
                  }
                },
                {
                  name: "The Hafler Trio",
                  joinphrase: "",
                  artist: {
                    id: "146c01d0-d3a2-44c3-acb5-9208bce75e14",
                    name: "The Hafler Trio",
                    sort-name: "Hafler Trio, The",
                    disambiguation: ""
                  }
                }
              ]
            }
          }
        ]
      },
      {
        position: 2,
        title: "h³æ",
        format-id: "9712d52a-4509-3d4b-a1a2-67c88c643e31",
        format: "CD",
        track-count: 1,
        track-offset: 0,
        discs: [
          {
            id: "aSHvkMnq2jZVFEK.DmSPbvN_f54-",
            sectors: 69341,
            offsets: [150],
            offset-count: 1
          }
        ],
        tracks: [
          {
            id: "5f2031a2-c67d-3bec-8ae5-8d22847ab0a5",
            title: "h³æ",
            length: 922546,
            number: "1",
            position: 1,
            artist-credit: [
              {
                name: "Autechre",
                joinphrase: " & ",
                artist: {
                  id: "410c9baf-5469-44f6-9852-826524b80c61",
                  name: "Autechre",
                  sort-name: "Autechre",
                  disambiguation: "English electronic music duo Rob Brown & Sean Booth"
                }
              },
              {
                name: "The Hafler Trio",
                joinphrase: "",
                artist: {
                  id: "146c01d0-d3a2-44c3-acb5-9208bce75e14",
                  name: "The Hafler Trio",
                  sort-name: "Hafler Trio, The",
                  disambiguation: ""
                }
              }
            ],
            recording: {
              id: "5aff6309-2e02-4a47-9233-32d7dcc9a960",
              title: "h³æ",
              disambiguation: "",
              length: 922546,
              video: false,
              artist-credit: [
                {
                  name: "Autechre",
                  joinphrase: " & ",
                  artist: {
                    id: "410c9baf-5469-44f6-9852-826524b80c61",
                    name: "Autechre",
                    sort-name: "Autechre",
                    disambiguation: "English electronic music duo Rob Brown & Sean Booth"
                  }
                },
                {
                  name: "The Hafler Trio",
                  joinphrase: "",
                  artist: {
                    id: "146c01d0-d3a2-44c3-acb5-9208bce75e14",
                    name: "The Hafler Trio",
                    sort-name: "Hafler Trio, The",
                    disambiguation: ""
                  }
                }
              ]
            }
          }
        ]
      }
    ],
    cover-art-archive: {
      count: 1,
      artwork: true,
      front: true,
      back: false,
      darkened: false
    }
  }
```

