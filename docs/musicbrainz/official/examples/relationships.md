Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

## Relationships

You can find standard real-life examples of relationship requests above, under the lookup examples for Event and Work.

See the example below for a classical release (one-track, for simplicity) with both performers (recording level relationships) and composer (work level relationship) using `recording-level-rels` and `work-level-rels`.

Request: [https://musicbrainz.org/ws/2/release/987f3e2d-22a6-4a4f-b840-c80c26b8b91a?inc=artist-credits+labels+recordings+recording-level-rels+work-rels+work-level-rels+artist-rels](https://musicbrainz.org/ws/2/release/987f3e2d-22a6-4a4f-b840-c80c26b8b91a?inc=artist-credits+labels+recordings+recording-level-rels+work-rels+work-level-rels+artist-rels)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
    <release id="987f3e2d-22a6-4a4f-b840-c80c26b8b91a">
        <title>Become Desert</title>
        <status id="4e304316-386d-3409-af2e-78857eec5cfe">Official</status>
        <quality>normal</quality>
        <packaging id="119eba76-b343-3e02-a292-f0f00644bb9b">None</packaging>
        <text-representation>
            <language>eng</language>
            <script>Latn</script>
        </text-representation>
        <artist-credit>
            <name-credit joinphrase="; ">
                <artist id="96681463-98e2-4032-9728-5fbb7b002427" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
                    <name>John Luther Adams</name>
                    <sort-name>Adams, John Luther</sort-name>
                </artist>
            </name-credit>
            <name-credit joinphrase=", ">
                <artist id="0b51c328-1f2b-464c-9e2c-0c2a8cce20ae" type="Orchestra" type-id="a0b36c92-3eb1-3839-a4f9-4799823f54a5">
                    <name>Seattle Symphony</name>
                    <sort-name>Seattle Symphony</sort-name>
                </artist>
            </name-credit>
            <name-credit>
                <artist id="eace8da8-8535-47c4-82da-902c792ec9f4" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
                    <name>Ludovic Morlot</name>
                    <sort-name>Morlot, Ludovic</sort-name>
                </artist>
            </name-credit>
        </artist-credit>
        <date>2019-06-14</date>
        <country>XW</country>
        <release-event-list count="1">
            <release-event>
                <date>2019-06-14</date>
                <area id="525d4e18-3d00-31b9-a58b-a146a916de8f">
                    <name>[Worldwide]</name>
                    <sort-name>[Worldwide]</sort-name>
                    <iso-3166-1-code-list>
                        <iso-3166-1-code>XW</iso-3166-1-code>
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
                <label id="3044afdb-c895-4f60-8e69-5796b3887c21" type="Original Production" type-id="7aaa37fe-2def-3476-b359-80245850062d">
                    <name>Cantaloupe Music</name>
                    <sort-name>Cantaloupe Music</sort-name>
                </label>
            </label-info>
        </label-info-list>
        <medium-list count="1">
            <medium>
                <position>1</position>
                <format id="907a28d9-b3b2-3ef6-89a8-7b18d91d4794">Digital Media</format>
                <track-list count="1" offset="0">
                    <track id="33781879-1dae-422c-a634-b26f89705e48">
                        <position>1</position>
                        <number>1</number>
                        <length>2422450</length>
                        <artist-credit>
                            <name-credit>
                                <artist id="96681463-98e2-4032-9728-5fbb7b002427" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
                                    <name>John Luther Adams</name>
                                    <sort-name>Adams, John Luther</sort-name>
                                </artist>
                            </name-credit>
                        </artist-credit>
                        <recording id="d90bc0ff-c7d9-4c09-a12b-d46f46f7281d">
                            <title>Become Desert</title>
                            <length>2422450</length>
                            <artist-credit>
                                <name-credit joinphrase=", ">
                                    <artist id="0b51c328-1f2b-464c-9e2c-0c2a8cce20ae" type="Orchestra" type-id="a0b36c92-3eb1-3839-a4f9-4799823f54a5">
                                        <name>Seattle Symphony</name>
                                        <sort-name>Seattle Symphony</sort-name>
                                    </artist>
                                </name-credit>
                                <name-credit>
                                    <artist id="eace8da8-8535-47c4-82da-902c792ec9f4" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
                                        <name>Ludovic Morlot</name>
                                        <sort-name>Morlot, Ludovic</sort-name>
                                    </artist>
                                </name-credit>
                            </artist-credit>
                            <relation-list target-type="artist">
                                <relation type="conductor" type-id="234670ce-5f22-4fd0-921b-ef1662695c5d">
                                    <target>eace8da8-8535-47c4-82da-902c792ec9f4</target>
                                    <direction>backward</direction>
                                    <begin>2018-09-25</begin>
                                    <end>2018-09-26</end>
                                    <ended>true</ended>
                                    <artist id="eace8da8-8535-47c4-82da-902c792ec9f4" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
                                        <name>Ludovic Morlot</name>
                                        <sort-name>Morlot, Ludovic</sort-name>
                                    </artist>
                                </relation>
                                <relation type="mix" type-id="3e3102e1-1896-4f50-b5b2-dd9824e46efe">
                                    <target>ac0122d6-14e8-4335-a01f-5d6ee71d308b</target>
                                    <direction>backward</direction>
                                    <artist id="ac0122d6-14e8-4335-a01f-5d6ee71d308b" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
                                        <name>Nathaniel Reichman</name>
                                        <sort-name>Reichman, Nathaniel</sort-name>
                                        <disambiguation>music producer, rerecording mixer</disambiguation>
                                    </artist>
                                </relation>
                                <relation type="performing orchestra" type-id="3b6616c5-88ba-4341-b4ee-81ce1e6d7ebb">
                                    <target>0b51c328-1f2b-464c-9e2c-0c2a8cce20ae</target>
                                    <direction>backward</direction>
                                    <begin>2018-09-25</begin>
                                    <end>2018-09-26</end>
                                    <ended>true</ended>
                                    <artist id="0b51c328-1f2b-464c-9e2c-0c2a8cce20ae" type="Orchestra" type-id="a0b36c92-3eb1-3839-a4f9-4799823f54a5">
                                        <name>Seattle Symphony</name>
                                        <sort-name>Seattle Symphony</sort-name>
                                    </artist>
                                </relation>
                                <relation type="producer" type-id="5c0ceac3-feb4-41f0-868d-dc06f6e27fc0">
                                    <target>748b47b2-604b-4487-b994-944918df19e9</target>
                                    <direction>backward</direction>
                                    <artist id="748b47b2-604b-4487-b994-944918df19e9" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
                                        <name>Dmitriy Lipay</name>
                                        <sort-name>Lipay, Dmitriy</sort-name>
                                        <disambiguation>recording engineer & producer</disambiguation>
                                    </artist>
                                </relation>
                                <relation type="producer" type-id="5c0ceac3-feb4-41f0-868d-dc06f6e27fc0">
                                    <target>ac0122d6-14e8-4335-a01f-5d6ee71d308b</target>
                                    <direction>backward</direction>
                                    <artist id="ac0122d6-14e8-4335-a01f-5d6ee71d308b" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
                                        <name>Nathaniel Reichman</name>
                                        <sort-name>Reichman, Nathaniel</sort-name>
                                        <disambiguation>music producer, rerecording mixer</disambiguation>
                                    </artist>
                                </relation>
                                <relation type="recording" type-id="a01ee869-80a8-45ef-9447-c59e91aa7926">
                                    <target>e2a6142d-83f5-4626-a48a-1211588d06db</target>
                                    <direction>backward</direction>
                                    <artist id="e2a6142d-83f5-4626-a48a-1211588d06db" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
                                        <name>Alexander Lipay</name>
                                        <sort-name>Lipay, Alexander</sort-name>
                                    </artist>
                                </relation>
                                <relation type="recording" type-id="a01ee869-80a8-45ef-9447-c59e91aa7926">
                                    <target>748b47b2-604b-4487-b994-944918df19e9</target>
                                    <direction>backward</direction>
                                    <artist id="748b47b2-604b-4487-b994-944918df19e9" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
                                        <name>Dmitriy Lipay</name>
                                        <sort-name>Lipay, Dmitriy</sort-name>
                                        <disambiguation>recording engineer & producer</disambiguation>
                                    </artist>
                                </relation>
                            </relation-list>
                            <relation-list target-type="work">
                                <relation type="performance" type-id="a3005666-a872-32c3-ad06-98af558e99b0">
                                    <target>b6cb19ec-08d3-4563-9e82-9562e5cab030</target>
                                    <direction>forward</direction>
                                    <begin>2018-09-25</begin>
                                    <end>2018-09-26</end>
                                    <ended>true</ended>
                                    <work id="b6cb19ec-08d3-4563-9e82-9562e5cab030" type="Symphony" type-id="174314aa-0aa4-30cf-96a6-50b281d8d208">
                                        <title>Become Desert</title>
                                        <language>zxx</language>
                                        <language-list>
                                            <language>zxx</language>
                                        </language-list>
                                        <relation-list target-type="artist">
                                            <relation type="commissioned" type-id="95f0213a-dbe0-4d36-8036-9782e425e98a">
                                                <target>7cdb68bc-c4f1-4a92-9bd2-739641c5eff0</target>
                                                <direction>backward</direction>
                                                <artist id="7cdb68bc-c4f1-4a92-9bd2-739641c5eff0" type="Orchestra" type-id="a0b36c92-3eb1-3839-a4f9-4799823f54a5">
                                                    <name>New York Philharmonic</name>
                                                    <sort-name>New York Philharmonic</sort-name>
                                                </artist>
                                            </relation>
                                            <relation type="commissioned" type-id="95f0213a-dbe0-4d36-8036-9782e425e98a">
                                                <target>c5958778-9c97-4970-9e63-0072ab2c4189</target>
                                                <direction>backward</direction>
                                                <artist id="c5958778-9c97-4970-9e63-0072ab2c4189" type="Orchestra" type-id="a0b36c92-3eb1-3839-a4f9-4799823f54a5">
                                                    <name>Rotterdams Philharmonisch Orkest</name>
                                                    <sort-name>Rotterdams Philharmonisch Orkest</sort-name>
                                                </artist>
                                            </relation>
                                            <relation type="commissioned" type-id="95f0213a-dbe0-4d36-8036-9782e425e98a">
                                                <target>68f7b9d5-b329-40aa-a474-5abffd6dfb44</target>
                                                <direction>backward</direction>
                                                <artist id="68f7b9d5-b329-40aa-a474-5abffd6dfb44" type="Orchestra" type-id="a0b36c92-3eb1-3839-a4f9-4799823f54a5">
                                                    <name>San Diego Symphony</name>
                                                    <sort-name>San Diego Symphony</sort-name>
                                                </artist>
                                            </relation>
                                            <relation type="commissioned" type-id="95f0213a-dbe0-4d36-8036-9782e425e98a">
                                                <target>0b51c328-1f2b-464c-9e2c-0c2a8cce20ae</target>
                                                <direction>backward</direction>
                                                <artist id="0b51c328-1f2b-464c-9e2c-0c2a8cce20ae" type="Orchestra" type-id="a0b36c92-3eb1-3839-a4f9-4799823f54a5">
                                                    <name>Seattle Symphony</name>
                                                    <sort-name>Seattle Symphony</sort-name>
                                                </artist>
                                            </relation>
                                            <relation type="composer" type-id="d59d99ea-23d4-4a80-b066-edca32ee158f">
                                                <target>96681463-98e2-4032-9728-5fbb7b002427</target>
                                                <direction>backward</direction>
                                                <artist id="96681463-98e2-4032-9728-5fbb7b002427" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
                                                    <name>John Luther Adams</name>
                                                    <sort-name>Adams, John Luther</sort-name>
                                                </artist>
                                            </relation>
                                            <relation type="premiere" type-id="5cc8cfb5-cca0-4395-a44b-b7d3c1777608">
                                                <target>0b51c328-1f2b-464c-9e2c-0c2a8cce20ae</target>
                                                <direction>backward</direction>
                                                <begin>2018-03-21</begin>
                                                <end>2018-03-21</end>
                                                <ended>true</ended>
                                                <artist id="0b51c328-1f2b-464c-9e2c-0c2a8cce20ae" type="Orchestra" type-id="a0b36c92-3eb1-3839-a4f9-4799823f54a5">
                                                    <name>Seattle Symphony</name>
                                                    <sort-name>Seattle Symphony</sort-name>
                                                </artist>
                                            </relation>
                                            <relation type="premiere" type-id="5cc8cfb5-cca0-4395-a44b-b7d3c1777608">
                                                <target>0b51c328-1f2b-464c-9e2c-0c2a8cce20ae</target>
                                                <direction>backward</direction>
                                                <begin>2018-03-29</begin>
                                                <end>2018-03-29</end>
                                                <ended>true</ended>
                                                <artist id="0b51c328-1f2b-464c-9e2c-0c2a8cce20ae" type="Orchestra" type-id="a0b36c92-3eb1-3839-a4f9-4799823f54a5">
                                                    <name>Seattle Symphony</name>
                                                    <sort-name>Seattle Symphony</sort-name>
                                                </artist>
                                            </relation>
                                        </relation-list>
                                    </work>
                                </relation>
                            </relation-list>
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
    id: "987f3e2d-22a6-4a4f-b840-c80c26b8b91a",
    title: "Become Desert",
    disambiguation: "",
    artist-credit: [
      {
        name: "John Luther Adams",
        joinphrase: "; ",
        artist: {
          id: "96681463-98e2-4032-9728-5fbb7b002427",
          name: "John Luther Adams",
          sort-name: "Adams, John Luther",
          disambiguation: ""
        }
      },
      {
        name: "Seattle Symphony",
        joinphrase: ", ",
        artist: {
          id: "0b51c328-1f2b-464c-9e2c-0c2a8cce20ae",
          name: "Seattle Symphony",
          sort-name: "Seattle Symphony",
          disambiguation: ""
        }
      },
      {
        name: "Ludovic Morlot",
        joinphrase: "",
        artist: {
          id: "eace8da8-8535-47c4-82da-902c792ec9f4",
          name: "Ludovic Morlot",
          sort-name: "Morlot, Ludovic",
          disambiguation: ""
        }
      }
    ],
    text-representation: {
      language: "eng",
      script: "Latn"
    },
    date: "2019-06-14",
    country: "XW",
    release-events: [
      {
        date: "2019-06-14",
        area: {
          id: "525d4e18-3d00-31b9-a58b-a146a916de8f",
          name: "[Worldwide]",
          sort-name: "[Worldwide]",
          disambiguation: "",
          iso-3166-1-codes: ["XW"]
        }
      }
    ],
    label-info: [
      {
        label: {
          id: "3044afdb-c895-4f60-8e69-5796b3887c21",
          sort-name: "Cantaloupe Music",
          name: "Cantaloupe Music",
          label-code: null,
          disambiguation: ""
        },
        catalog-number: null
      }
    ],
    barcode: null,
    status-id: "4e304316-386d-3409-af2e-78857eec5cfe",
    status: "Official",
    packaging-id: "119eba76-b343-3e02-a292-f0f00644bb9b",
    packaging: "None",
    quality: "normal",
    relations: [ ],
    asin: null,
    media: [
      {
        position: 1,
        format-id: "907a28d9-b3b2-3ef6-89a8-7b18d91d4794",
        format: "Digital Media",
        title: "",
        track-count: 1,
        track-offset: 0,
        tracks: [
          {
            id: "33781879-1dae-422c-a634-b26f89705e48",
            title: "Become Desert",
            length: 2422450,
            number: "1",
            position: 1,
            artist-credit: [
              {
                name: "John Luther Adams",
                joinphrase: "",
                artist: {
                  id: "96681463-98e2-4032-9728-5fbb7b002427",
                  name: "John Luther Adams",
                  sort-name: "Adams, John Luther",
                  disambiguation: ""
                }
              }
            ],
            recording: {
              id: "d90bc0ff-c7d9-4c09-a12b-d46f46f7281d",
              title: "Become Desert",
              disambiguation: "",
              length: 2422450,
              video: false,
              artist-credit: [
                {
                  name: "Seattle Symphony",
                  joinphrase: ", ",
                  artist: {
                    id: "0b51c328-1f2b-464c-9e2c-0c2a8cce20ae",
                    name: "Seattle Symphony",
                    sort-name: "Seattle Symphony",
                    disambiguation: ""
                  }
                },
                {
                  name: "Ludovic Morlot",
                  joinphrase: "",
                  artist: {
                    id: "eace8da8-8535-47c4-82da-902c792ec9f4",
                    sort-name: "Morlot, Ludovic",
                    disambiguation: "",
                    name: "Ludovic Morlot"
                  }
                }
              ],
              relations: [
                {
                  type-id: "234670ce-5f22-4fd0-921b-ef1662695c5d",
                  type: "conductor",
                  direction: "backward",
                  target-type: "artist",
                  artist: {
                    id: "eace8da8-8535-47c4-82da-902c792ec9f4",
                    name: "Ludovic Morlot",
                    sort-name: "Morlot, Ludovic",
                    disambiguation: ""
                  },
                  begin: "2018-09-25",
                  end: "2018-09-26",
                  ended: true,
                  target-credit: "",
                  source-credit: "",
                  attributes: [ ],
                  attribute-values: { },
                  attribute-ids: { }
                },
                {
                  type-id: "3b6616c5-88ba-4341-b4ee-81ce1e6d7ebb",
                  type: "performing orchestra",
                  direction: "backward",
                  target-type: "artist",
                  artist: {
                    id: "0b51c328-1f2b-464c-9e2c-0c2a8cce20ae",
                    name: "Seattle Symphony",
                    sort-name: "Seattle Symphony",
                    disambiguation: ""
                  },
                  begin: "2018-09-25",
                  end: "2018-09-26",
                  ended: true,
                  target-credit: "",
                  source-credit: "",
                  attributes: [ ],
                  attribute-values: { },
                  attribute-ids: { }
                },
                {
                  type-id: "a01ee869-80a8-45ef-9447-c59e91aa7926",
                  type: "recording",
                  direction: "backward",
                  target-type: "artist",
                  artist: {
                    id: "e2a6142d-83f5-4626-a48a-1211588d06db",
                    name: "Alexander Lipay",
                    sort-name: "Lipay, Alexander",
                    disambiguation: ""
                  },
                  begin: null,
                  end: null,
                  ended: false,
                  target-credit: "",
                  source-credit: "",
                  attributes: [ ],
                  attribute-values: { },
                  attribute-ids: { }
                },
                {
                  type-id: "a3005666-a872-32c3-ad06-98af558e99b0",
                  type: "performance",
                  direction: "forward",
                  target-type: "work",
                  work: {
                    id: "b6cb19ec-08d3-4563-9e82-9562e5cab030",
                    title: "Become Desert",
                    disambiguation: "",
                    type-id: null,
                    type: null,
                    languages: ["zxx"],
                    iswcs: [ ],
                    attributes: [ ],
                    relations: [
                      {
                        type-id: "d59d99ea-23d4-4a80-b066-edca32ee158f",
                        type: "composer",
                        direction: "backward",
                        target-type: "artist",
                        artist: {
                          id: "96681463-98e2-4032-9728-5fbb7b002427",
                          name: "John Luther Adams",
                          sort-name: "Adams, John Luther",
                          disambiguation: ""
                        },
                        begin: null,
                        end: null,
                        ended: false,
                        source-credit: "",
                        target-credit: "",
                        attributes: [ ],
                        attribute-ids: { },
                        attribute-values: { }
                      },
                      {
                        type-id: "95f0213a-dbe0-4d36-8036-9782e425e98a",
                        type: "commissioned",
                        direction: "backward",
                        target-type: "artist",
                        artist: {
                          id: "7cdb68bc-c4f1-4a92-9bd2-739641c5eff0",
                          name: "New York Philharmonic",
                          sort-name: "New York Philharmonic",
                          disambiguation: ""
                        },
                        begin: null,
                        end: null,
                        ended: false,
                        source-credit: "",
                        target-credit: "",
                        attributes: [ ],
                        attribute-ids: { },
                        attribute-values: { }
                      }
                    ]
                  },
                  begin: "2018-09-25",
                  end: "2018-09-26",
                  ended: true,
                  target-credit: "",
                  source-credit: "",
                  attributes: [ ],
                  attribute-ids: { },
                  attribute-values: { }
                }
              ]
            }
          }
        ]
      }
    ],
    cover-art-archive: {
      artwork: true,
      count: 1,
      front: true,
      back: false,
      darkened: false
    }
  }
```

  
See below for an example of a relationship with relationship attributes (in this case, the work is part of a work catalogue series, and it is assigned a specific number for that series, stored as an attribute):

Request: [https://musicbrainz.org/ws/2/work/c1b0e8a2-2461-4d48-9a89-f4e6d624d342?inc=series-rels](https://musicbrainz.org/ws/2/work/c1b0e8a2-2461-4d48-9a89-f4e6d624d342?inc=series-rels)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <work id="c1b0e8a2-2461-4d48-9a89-f4e6d624d342" type="Symphony" type-id="174314aa-0aa4-30cf-96a6-50b281d8d208">
    <title>Sinfonie Nr. 1 c-Moll, op. 68</title>
    <language>zxx</language>
    <language-list>
      <language>zxx</language>
    </language-list>
    <attribute-list>
      <attribute value-id="d3d0b554-d3f4-3995-b995-5113f3771295" type="Key" type-id="7526c19d-3be4-3420-b6cc-9fb6e49fa1a9">C minor</attribute>
    </attribute-list>
    <relation-list target-type="series">
      <relation type="part of" type-id="b0d44366-cdf0-3acb-bee6-0f65a77a6ef0">
        <target>03531d5b-11e8-40c9-9fc3-d814e9886590</target>
        <ordering-key>114</ordering-key>
        <direction>backward</direction>
        <attribute-list>
          <attribute type-id="a59c5830-5ec7-38fe-9a21-c7ea54f6650a" value="op. 68">number</attribute>
        </attribute-list>
        <series id="03531d5b-11e8-40c9-9fc3-d814e9886590" type="Catalogue" type-id="49482ff0-fc9e-3b8c-a2d0-30e84d9df002">
          <name>Johannes Brahms. Thematisch-Bibliographisches Werkverzeichnis</name>
        </series>
      </relation>
    </relation-list>
  </work>
</metadata>
```

JSON Response

```
{
  id: "c1b0e8a2-2461-4d48-9a89-f4e6d624d342",
  title: "Sinfonie Nr. 1 c-Moll, op. 68"
  type-id: "174314aa-0aa4-30cf-96a6-50b281d8d208",
  type: "Symphony",
  disambiguation: "",
  language: "zxx",
  languages: ["zxx"],
  iswcs: [],
  attributes: [
    {
      value-id: "d3d0b554-d3f4-3995-b995-5113f3771295",
      type-id: "7526c19d-3be4-3420-b6cc-9fb6e49fa1a9",
      type: "Key",
      value: "C minor",
    }
  ],
  relations: [
    {
      direction: "backward",
      target-credit: "",
      type-id: "b0d44366-cdf0-3acb-bee6-0f65a77a6ef0",
      source-credit: "",
      begin: null,
      target-type: "series",
      ended: false,
      series: {
        type: "Catalogue",
        type-id: "49482ff0-fc9e-3b8c-a2d0-30e84d9df002",
        disambiguation: "",
        id: "03531d5b-11e8-40c9-9fc3-d814e9886590",
        name: "Johannes Brahms. Thematisch-Bibliographisches Werkverzeichnis"
      },
      attribute-ids: {
        number: "a59c5830-5ec7-38fe-9a21-c7ea54f6650a"
      },
      attributes: [
        "number"
      ],
      ordering-key: 114,
      type: "part of",
      attribute-values: {
        number: "op. 68"
      },
      end: null
    }
  ],
}
```

