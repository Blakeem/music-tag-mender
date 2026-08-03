Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## Recording

#### Example

[http://musicbrainz.org/ws/2/recording/?query=isrc:GBAHT1600302](http://musicbrainz.org/ws/2/recording/?query=isrc:GBAHT1600302)

#### Search Fields

The [Recording](https://musicbrainz.org/doc/Recording) index contains the following fields you can search

| Field | Description |
| --- | --- |
| alias | (part of) any [alias](https://musicbrainz.org/doc/Aliases) attached to the recording (diacritics are ignored) |
| arid | the MBID of any of the recording artists |
| artist | (part of) the combined credited artist name for the recording, including join phrases (e.g. "Artist X feat.") |
| artistname | (part of) the name of any of the recording artists |
| comment | (part of) the recording's disambiguation comment |
| country | the 2-letter code (ISO 3166-1 alpha-2) for the country any release of this recording was released in |
| creditname | (part of) the credited name of any of the recording artists on this particular recording |
| date | the release date of any release including this recording (e.g. "1980-01-22") |
| dur | the recording duration in milliseconds |
| firstreleasedate | the release date of the earliest release including this recording (e.g. "1980-01-22") |
| format | the [format](https://musicbrainz.org/doc/Release/Format) of any medium including this recording (insensitive to case, spaces, and separators) |
| isrc | any [ISRC](https://musicbrainz.org/doc/ISRC) associated to the recording |
| number | the free-text number of the track on any medium including this recording (e.g. "A4") |
| position | the position inside its release of any medium including this recording (starts at 1) |
| primarytype | the [primary type](https://musicbrainz.org/doc/Release_Group/Type#Primary_types) of any release group including this recording |
| qdur | the recording duration, quantized (duration in milliseconds / 2000) |
| recording | (part of) the recording's name, or the name of a track connected to this recording (diacritics are ignored) |
| recordingaccent | (part of) the recordings's name, or the name of a track connected to this recording (with the specified diacritics) |
| reid | the MBID of any release including this recording |
| release | (part of) the name of any release including this recording |
| rgid | the MBID of any release group including this recording |
| rid | the recording's MBID |
| secondarytype | any of the [secondary types](https://musicbrainz.org/doc/Release_Group/Type#Secondary_types) of any release group including this recording |
| status | the [status](https://musicbrainz.org/doc/Release#Status) of any release including this recording |
| tag | (part of) a tag attached to the recording |
| tid | the MBID of a track connected to this recording |
| tnum | the position of the track on any medium including this recording (starts at 1, pre-gaps at 0) |
| tracks | the number of tracks on any medium including this recording |
| tracksrelease | the number of tracks on any release (as a whole) including this recording |
| type | legacy [release group type](https://musicbrainz.org/doc/Release_Group/Type) field that predates the ability to set multiple types ([see calculation code](https://github.com/metabrainz/musicbrainz-server/blob/f8bd7e9366eb6e836dfcee9b626e1ca969db1c4f/lib/MusicBrainz/Server/WebService/XMLSerializer.pm#L305-L326)) |
| video | a boolean flag (true/false) indicating whether or not the recording is a video recording |

If you don't specify a field, the terms will be searched for in the _recording_ field.

#### Xml

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<metadata created="2017-02-23T14:31:36.889Z" xmlns="http://musicbrainz.org/ns/mmd-2.0#" xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">
  <recording-list count="1" offset="0">
    <recording id="026fa041-3917-4c73-9079-ed16e36f20f8" ext:score="100">
      <title>Blow Your Mind (Mwah)</title>
      <length>178000</length>
      <artist-credit id="4af83e24-f886-35c8-831f-5dde0163a6a0">
        <name-credit>
          <artist id="6f1a58bf-9b1b-49cf-a44a-6cefad7ae04f">
            <name>Dua Lipa</name>
            <sort-name>Lipa, Dua</sort-name>
          </artist>
        </name-credit>
      </artist-credit>
      <first-release-date>2016-08-26</first-release-date>
      <release-list>
        <release id="383be31c-37a0-4e08-8cda-cbcbbc587ae5">
          <title>Blow Your Mind (Mwah)</title>
          <status id="4e304316-386d-3409-af2e-78857eec5cfe">Official</status>
          <release-group id="4a45bfa5-eb1e-49eb-a20c-1021389b2121" type="Single">
            <primary-type>Single</primary-type>
          </release-group>
          <date>2016-08-26</date>
          <country>XW</country>
          <release-event-list>
            <release-event>
              <date>2016-08-26</date>
              <area id="525d4e18-3d00-31b9-a58b-a146a916de8f">
                <name>[Worldwide]</name>
                <sort-name>[Worldwide]</sort-name>
                <iso-3166-1-code-list>
                  <iso-3166-1-code>XW</iso-3166-1-code>
                </iso-3166-1-code-list>
              </area>
            </release-event>
          </release-event-list>
          <medium-list>
            <track-count>1</track-count>
            <medium>
              <position>1</position>
              <format>Digital Media</format>
              <track-list count="1" offset="0">
                <track id="0ef6e647-4aeb-438e-8c8a-50c22c511203">
                  <number>1</number>
                  <title>Blow Your Mind (Mwah)</title>
                  <length>179000</length>
                </track>
              </track-list>
            </medium>
          </medium-list>
        </release>
        <release id="8bd42e63-46cb-43e3-8294-c9c3b9793581">
          <title>Now That’s What I Call Music! 95</title>
          <status id="4e304316-386d-3409-af2e-78857eec5cfe">Official</status>
          <artist-credit id="949a7fd5-fe73-3e8f-922e-01ff4ca958f7">
            <name-credit>
              <artist id="89ad4ac3-39f7-470e-963a-56509c546377">
                <name>Various Artists</name>
                <sort-name>Various Artists</sort-name>
              </artist>
            </name-credit>
          </artist-credit>
          <release-group id="071a007b-b9cf-4e22-8646-03c30fc8dd87" type="Compilation">
            <primary-type>Album</primary-type>
            <secondary-type-list>
              <secondary-type>Compilation</secondary-type>
            </secondary-type-list>
          </release-group>
          <date>2016-11-18</date>
          <country>GB</country>
          <release-event-list>
            <release-event>
              <date>2016-11-18</date>
              <area id="8a754a16-0027-3a29-b6d7-2b40ea0481ed">
                <name>United Kingdom</name>
                <sort-name>United Kingdom</sort-name>
                <iso-3166-1-code-list>
                  <iso-3166-1-code>GB</iso-3166-1-code>
                </iso-3166-1-code-list>
              </area>
            </release-event>
          </release-event-list>
          <medium-list>
            <track-count>45</track-count>
            <medium>
              <position>1</position>
              <format>Digital Media</format>
              <track-list count="45" offset="35">
                <track id="e43cfb2f-fdd3-40a7-9396-25c9435ca8cb">
                  <number>36</number>
                  <title>Blow Your Mind (Mwah)</title>
                  <length>178000</length>
                </track>
              </track-list>
            </medium>
          </medium-list>
        </release>
        <release id="72a240b0-ca08-4d35-a15d-c1eded21c0ce">
          <title>Now That’s What I Call Music! 95</title>
          <status id="4e304316-386d-3409-af2e-78857eec5cfe">Official</status>
          <artist-credit id="949a7fd5-fe73-3e8f-922e-01ff4ca958f7">
            <name-credit>
              <artist id="89ad4ac3-39f7-470e-963a-56509c546377">
                <name>Various Artists</name>
                <sort-name>Various Artists</sort-name>
              </artist>
            </name-credit>
          </artist-credit>
          <release-group id="071a007b-b9cf-4e22-8646-03c30fc8dd87" type="Compilation">
            <primary-type>Album</primary-type>
            <secondary-type-list>
              <secondary-type>Compilation</secondary-type>
            </secondary-type-list>
          </release-group>
          <date>2016-11-18</date>
          <country>GB</country>
          <release-event-list>
            <release-event>
              <date>2016-11-18</date>
              <area id="8a754a16-0027-3a29-b6d7-2b40ea0481ed">
                <name>United Kingdom</name>
                <sort-name>United Kingdom</sort-name>
                <iso-3166-1-code-list>
                  <iso-3166-1-code>GB</iso-3166-1-code>
                </iso-3166-1-code-list>
              </area>
            </release-event>
          </release-event-list>
          <medium-list>
            <track-count>45</track-count>
            <medium>
              <position>2</position>
              <format>CD</format>
              <track-list count="24" offset="14">
                <track id="8c193afa-a867-46fb-8745-5168a8e16a75">
                  <number>15</number>
                  <title>Blow Your Mind (Mwah)</title>
                  <length>178213</length>
                </track>
              </track-list>
            </medium>
          </medium-list>
        </release>
        <release id="6313b666-6042-486b-b2aa-614e7542ff68">
          <title>Life Is Music 2016.2</title>
          <status id="4e304316-386d-3409-af2e-78857eec5cfe">Official</status>
          <artist-credit id="949a7fd5-fe73-3e8f-922e-01ff4ca958f7">
            <name-credit>
              <artist id="89ad4ac3-39f7-470e-963a-56509c546377">
                <name>Various Artists</name>
                <sort-name>Various Artists</sort-name>
              </artist>
            </name-credit>
          </artist-credit>
          <release-group id="406d765d-c5ca-43e9-b268-a33878927ff5" type="Compilation">
            <primary-type>Album</primary-type>
            <secondary-type-list>
              <secondary-type>Compilation</secondary-type>
            </secondary-type-list>
          </release-group>
          <date>2016-12-02</date>
          <country>BE</country>
          <release-event-list>
            <release-event>
              <date>2016-12-02</date>
              <area id="5b8a5ee5-0bb3-34cf-9a75-c27c44e341fc">
                <name>Belgium</name>
                <sort-name>Belgium</sort-name>
                <iso-3166-1-code-list>
                  <iso-3166-1-code>BE</iso-3166-1-code>
                </iso-3166-1-code-list>
              </area>
            </release-event>
          </release-event-list>
          <medium-list>
            <track-count>39</track-count>
            <medium>
              <position>1</position>
              <format>CD</format>
              <track-list count="20" offset="7">
                <track id="7e93e73c-e8f2-4218-af5e-baae4ce98882">
                  <number>8</number>
                  <title>Blow Your Mind (Mwah)</title>
                  <length>177266</length>
                </track>
              </track-list>
            </medium>
          </medium-list>
        </release>
        <release id="b3a2bca0-faec-4e67-8cf3-fd3d01755a2d">
          <title>Hits 2016</title>
          <status id="4e304316-386d-3409-af2e-78857eec5cfe">Official</status>
          <artist-credit id="949a7fd5-fe73-3e8f-922e-01ff4ca958f7">
            <name-credit>
              <artist id="89ad4ac3-39f7-470e-963a-56509c546377">
                <name>Various Artists</name>
                <sort-name>Various Artists</sort-name>
              </artist>
            </name-credit>
          </artist-credit>
          <release-group id="7678ff0a-9446-4d5f-b46e-56c84fc68654" type="Compilation">
            <primary-type>Album</primary-type>
            <secondary-type-list>
              <secondary-type>Compilation</secondary-type>
            </secondary-type-list>
          </release-group>
          <date>2016-12-23</date>
          <country>MX</country>
          <release-event-list>
            <release-event>
              <date>2016-12-23</date>
              <area id="3e08b2cd-69f3-317c-b1e4-e71be581839e">
                <name>Mexico</name>
                <sort-name>Mexico</sort-name>
                <iso-3166-1-code-list>
                  <iso-3166-1-code>MX</iso-3166-1-code>
                </iso-3166-1-code-list>
              </area>
            </release-event>
          </release-event-list>
          <medium-list>
            <track-count>23</track-count>
            <medium>
              <position>1</position>
              <format>Digital Media</format>
              <track-list count="23" offset="22">
                <track id="3b466997-8bda-410f-82aa-02b703c931c5">
                  <number>23</number>
                  <title>Blow Your Mind (Mwah)</title>
                  <length>178583</length>
                </track>
              </track-list>
            </medium>
          </medium-list>
        </release>
        <release id="d02cce98-98fe-430c-b3c9-951c94f4fb1d">
          <title>Now That's What I Call Music! 95</title>
          <status id="4e304316-386d-3409-af2e-78857eec5cfe">Official</status>
          <artist-credit id="949a7fd5-fe73-3e8f-922e-01ff4ca958f7">
            <name-credit>
              <artist id="89ad4ac3-39f7-470e-963a-56509c546377">
                <name>Various Artists</name>
                <sort-name>Various Artists</sort-name>
              </artist>
            </name-credit>
          </artist-credit>
          <release-group id="071a007b-b9cf-4e22-8646-03c30fc8dd87" type="Compilation">
            <primary-type>Album</primary-type>
            <secondary-type-list>
              <secondary-type>Compilation</secondary-type>
            </secondary-type-list>
          </release-group>
          <date>2016-11-18</date>
          <country>GB</country>
          <release-event-list>
            <release-event>
              <date>2016-11-18</date>
              <area id="8a754a16-0027-3a29-b6d7-2b40ea0481ed">
                <name>United Kingdom</name>
                <sort-name>United Kingdom</sort-name>
                <iso-3166-1-code-list>
                  <iso-3166-1-code>GB</iso-3166-1-code>
                </iso-3166-1-code-list>
              </area>
            </release-event>
          </release-event-list>
          <medium-list>
            <track-count>45</track-count>
            <medium>
              <position>2</position>
              <format>CD</format>
              <track-list count="24" offset="14">
                <track id="4ae11ffe-5ac8-42ae-a321-1535136ff05c">
                  <number>15</number>
                  <title>Blow Your Mind (Mwah)</title>
                  <length>178213</length>
                </track>
              </track-list>
            </medium>
          </medium-list>
        </release>
        <release id="5496dc5f-a282-422e-b7c6-78575e5f7944">
          <title>Dua Lipa (Deluxe)</title>
          <status id="518ffc83-5cde-34df-8627-81bff5093d92">Promotion</status>
          <release-group id="abbc4905-c25f-4c67-8e2d-19329ec48b1f" type="Album">
            <primary-type>Album</primary-type>
          </release-group>
          <date>2017-06-02</date>
          <country>XW</country>
          <release-event-list>
            <release-event>
              <date>2017-06-02</date>
              <area id="525d4e18-3d00-31b9-a58b-a146a916de8f">
                <name>[Worldwide]</name>
                <sort-name>[Worldwide]</sort-name>
                <iso-3166-1-code-list>
                  <iso-3166-1-code>XW</iso-3166-1-code>
                </iso-3166-1-code-list>
              </area>
            </release-event>
          </release-event-list>
          <medium-list>
            <track-count>17</track-count>
            <medium>
              <position>1</position>
              <format>Digital Media</format>
              <track-list count="17" offset="5">
                <track id="a2fcb379-7638-4f27-b2e4-5242a69ed09c">
                  <number>6</number>
                  <title>Blow Your Mind (Mwah)</title>
                  <length>178000</length>
                </track>
              </track-list>
            </medium>
          </medium-list>
        </release>
        <release id="eb66eaa0-acb6-4317-91ea-ff65b7813ec0">
          <title>Dua Lipa</title>
          <status id="518ffc83-5cde-34df-8627-81bff5093d92">Promotion</status>
          <release-group id="abbc4905-c25f-4c67-8e2d-19329ec48b1f" type="Album">
            <primary-type>Album</primary-type>
          </release-group>
          <date>2017-06-02</date>
          <country>XW</country>
          <release-event-list>
            <release-event>
              <date>2017-06-02</date>
              <area id="525d4e18-3d00-31b9-a58b-a146a916de8f">
                <name>[Worldwide]</name>
                <sort-name>[Worldwide]</sort-name>
                <iso-3166-1-code-list>
                  <iso-3166-1-code>XW</iso-3166-1-code>
                </iso-3166-1-code-list>
              </area>
            </release-event>
          </release-event-list>
          <medium-list>
            <track-count>12</track-count>
            <medium>
              <position>1</position>
              <format>Digital Media</format>
              <track-list count="12" offset="5">
                <track id="b74b2a35-900a-446c-8a63-7a5b01c82a5e">
                  <number>6</number>
                  <title>Blow Your Mind (Mwah)</title>
                  <length>178000</length>
                </track>
              </track-list>
            </medium>
          </medium-list>
        </release>
      </release-list>
      <isrc-list>
        <isrc id="GBAHT1600302"/>
      </isrc-list>
      <tag-list>
        <tag count="1">
          <name>electropop</name>
        </tag>
        <tag count="1">
          <name>dance-pop</name>
        </tag>
        <tag count="1">
          <name>contemporary r&b</name>
        </tag>
      </tag-list>
    </recording>
  </recording-list>
</metadata>
```

Note that if the recording belongs to a release that is a multiple-artist compilation then the release data includes an artist credit.

#### Json

```
{
  "created": "2017-02-23T14:31:36.889Z",
  "count": 1,
  "offset": 0,
  "recordings": [
    {
      "id": "026fa041-3917-4c73-9079-ed16e36f20f8",
      "score": "100",
      "title": "Blow Your Mind (Mwah)",
      "length": 178000,
      "video": null,
      "artist-credit": [
        {
          "artist": {
            "id": "6f1a58bf-9b1b-49cf-a44a-6cefad7ae04f",
            "name": "Dua Lipa",
            "sort-name": "Lipa, Dua"
          }
        }
      ],
      "artist-credit-id": "4af83e24-f886-35c8-831f-5dde0163a6a0",
      "first-release-date": "2016-08-26",
      "releases": [
        {
          "id": "383be31c-37a0-4e08-8cda-cbcbbc587ae5",
          "title": "Blow Your Mind (Mwah)",
          "status-id": "4e304316-386d-3409-af2e-78857eec5cfe",
          "status": "Official",
          "release-group": {
            "id": "4a45bfa5-eb1e-49eb-a20c-1021389b2121",
            "primary-type": "Single"
          },
          "date": "2016-08-26",
          "country": "XW",
          "release-events": [
            {
              "date": "2016-08-26",
              "area": {
                "id": "525d4e18-3d00-31b9-a58b-a146a916de8f",
                "name": "[Worldwide]",
                "sort-name": "[Worldwide]",
                "iso-3166-1-codes": [
                  "XW"
                ]
              }
            }
          ],
          "track-count": 1,
          "media": [
            {
              "position": 1,
              "format": "Digital Media",
              "track": [
                {
                  "id": "0ef6e647-4aeb-438e-8c8a-50c22c511203",
                  "number": "1",
                  "title": "Blow Your Mind (Mwah)",
                  "length": 179000
                }
              ],
              "track-count": 1,
              "track-offset": 0
            }
          ]
        },
        {
          "id": "8bd42e63-46cb-43e3-8294-c9c3b9793581",
          "title": "Now That’s What I Call Music! 95",
          "status-id": "4e304316-386d-3409-af2e-78857eec5cfe",
          "status": "Official",
          "artist-credit": [
            {
              "artist": {
                "id": "89ad4ac3-39f7-470e-963a-56509c546377",
                "name": "Various Artists",
                "sort-name": "Various Artists"
              }
            }
          ],
          "artist-credit-id": "949a7fd5-fe73-3e8f-922e-01ff4ca958f7",
          "release-group": {
            "id": "071a007b-b9cf-4e22-8646-03c30fc8dd87",
            "primary-type": "Album",
            "secondary-types": [
              "Compilation"
            ]
          },
          "date": "2016-11-18",
          "country": "GB",
          "release-events": [
            {
              "date": "2016-11-18",
              "area": {
                "id": "8a754a16-0027-3a29-b6d7-2b40ea0481ed",
                "name": "United Kingdom",
                "sort-name": "United Kingdom",
                "iso-3166-1-codes": [
                  "GB"
                ]
              }
            }
          ],
          "track-count": 45,
          "media": [
            {
              "position": 1,
              "format": "Digital Media",
              "track": [
                {
                  "id": "e43cfb2f-fdd3-40a7-9396-25c9435ca8cb",
                  "number": "36",
                  "title": "Blow Your Mind (Mwah)",
                  "length": 178000
                }
              ],
              "track-count": 45,
              "track-offset": 35
            }
          ]
        },
        {
          "id": "72a240b0-ca08-4d35-a15d-c1eded21c0ce",
          "title": "Now That’s What I Call Music! 95",
          "status-id": "4e304316-386d-3409-af2e-78857eec5cfe",
          "status": "Official",
          "artist-credit": [
            {
              "artist": {
                "id": "89ad4ac3-39f7-470e-963a-56509c546377",
                "name": "Various Artists",
                "sort-name": "Various Artists"
              }
            }
          ],
          "artist-credit-id": "949a7fd5-fe73-3e8f-922e-01ff4ca958f7",
          "release-group": {
            "id": "071a007b-b9cf-4e22-8646-03c30fc8dd87",
            "primary-type": "Album",
            "secondary-types": [
              "Compilation"
            ]
          },
          "date": "2016-11-18",
          "country": "GB",
          "release-events": [
            {
              "date": "2016-11-18",
              "area": {
                "id": "8a754a16-0027-3a29-b6d7-2b40ea0481ed",
                "name": "United Kingdom",
                "sort-name": "United Kingdom",
                "iso-3166-1-codes": [
                  "GB"
                ]
              }
            }
          ],
          "track-count": 45,
          "media": [
            {
              "position": 2,
              "format": "CD",
              "track": [
                {
                  "id": "8c193afa-a867-46fb-8745-5168a8e16a75",
                  "number": "15",
                  "title": "Blow Your Mind (Mwah)",
                  "length": 178213
                }
              ],
              "track-count": 24,
              "track-offset": 14
            }
          ]
        },
        {
          "id": "6313b666-6042-486b-b2aa-614e7542ff68",
          "title": "Life Is Music 2016.2",
          "status-id": "4e304316-386d-3409-af2e-78857eec5cfe",
          "status": "Official",
          "artist-credit": [
            {
              "artist": {
                "id": "89ad4ac3-39f7-470e-963a-56509c546377",
                "name": "Various Artists",
                "sort-name": "Various Artists"
              }
            }
          ],
          "artist-credit-id": "949a7fd5-fe73-3e8f-922e-01ff4ca958f7",
          "release-group": {
            "id": "406d765d-c5ca-43e9-b268-a33878927ff5",
            "primary-type": "Album",
            "secondary-types": [
              "Compilation"
            ]
          },
          "date": "2016-12-02",
          "country": "BE",
          "release-events": [
            {
              "date": "2016-12-02",
              "area": {
                "id": "5b8a5ee5-0bb3-34cf-9a75-c27c44e341fc",
                "name": "Belgium",
                "sort-name": "Belgium",
                "iso-3166-1-codes": [
                  "BE"
                ]
              }
            }
          ],
          "track-count": 39,
          "media": [
            {
              "position": 1,
              "format": "CD",
              "track": [
                {
                  "id": "7e93e73c-e8f2-4218-af5e-baae4ce98882",
                  "number": "8",
                  "title": "Blow Your Mind (Mwah)",
                  "length": 177266
                }
              ],
              "track-count": 20,
              "track-offset": 7
            }
          ]
        },
        {
          "id": "b3a2bca0-faec-4e67-8cf3-fd3d01755a2d",
          "title": "Hits 2016",
          "status-id": "4e304316-386d-3409-af2e-78857eec5cfe",
          "status": "Official",
          "artist-credit": [
            {
              "artist": {
                "id": "89ad4ac3-39f7-470e-963a-56509c546377",
                "name": "Various Artists",
                "sort-name": "Various Artists"
              }
            }
          ],
          "artist-credit-id": "949a7fd5-fe73-3e8f-922e-01ff4ca958f7",
          "release-group": {
            "id": "7678ff0a-9446-4d5f-b46e-56c84fc68654",
            "primary-type": "Album",
            "secondary-types": [
              "Compilation"
            ]
          },
          "date": "2016-12-23",
          "country": "MX",
          "release-events": [
            {
              "date": "2016-12-23",
              "area": {
                "id": "3e08b2cd-69f3-317c-b1e4-e71be581839e",
                "name": "Mexico",
                "sort-name": "Mexico",
                "iso-3166-1-codes": [
                  "MX"
                ]
              }
            }
          ],
          "track-count": 23,
          "media": [
            {
              "position": 1,
              "format": "Digital Media",
              "track": [
                {
                  "id": "3b466997-8bda-410f-82aa-02b703c931c5",
                  "number": "23",
                  "title": "Blow Your Mind (Mwah)",
                  "length": 178583
                }
              ],
              "track-count": 23,
              "track-offset": 22
            }
          ]
        },
        {
          "id": "d02cce98-98fe-430c-b3c9-951c94f4fb1d",
          "title": "Now That's What I Call Music! 95",
          "status-id": "4e304316-386d-3409-af2e-78857eec5cfe",
          "status": "Official",
          "artist-credit": [
            {
              "artist": {
                "id": "89ad4ac3-39f7-470e-963a-56509c546377",
                "name": "Various Artists",
                "sort-name": "Various Artists"
              }
            }
          ],
          "artist-credit-id": "949a7fd5-fe73-3e8f-922e-01ff4ca958f7",
          "release-group": {
            "id": "071a007b-b9cf-4e22-8646-03c30fc8dd87",
            "primary-type": "Album",
            "secondary-types": [
              "Compilation"
            ]
          },
          "date": "2016-11-18",
          "country": "GB",
          "release-events": [
            {
              "date": "2016-11-18",
              "area": {
                "id": "8a754a16-0027-3a29-b6d7-2b40ea0481ed",
                "name": "United Kingdom",
                "sort-name": "United Kingdom",
                "iso-3166-1-codes": [
                  "GB"
                ]
              }
            }
          ],
          "track-count": 45,
          "media": [
            {
              "position": 2,
              "format": "CD",
              "track": [
                {
                  "id": "4ae11ffe-5ac8-42ae-a321-1535136ff05c",
                  "number": "15",
                  "title": "Blow Your Mind (Mwah)",
                  "length": 178213
                }
              ],
              "track-count": 24,
              "track-offset": 14
            }
          ]
        },
        {
          "id": "5496dc5f-a282-422e-b7c6-78575e5f7944",
          "title": "Dua Lipa (Deluxe)",
          "status-id": "518ffc83-5cde-34df-8627-81bff5093d92",
          "status": "Promotion",
          "release-group": {
            "id": "abbc4905-c25f-4c67-8e2d-19329ec48b1f",
            "primary-type": "Album"
          },
          "date": "2017-06-02",
          "country": "XW",
          "release-events": [
            {
              "date": "2017-06-02",
              "area": {
                "id": "525d4e18-3d00-31b9-a58b-a146a916de8f",
                "name": "[Worldwide]",
                "sort-name": "[Worldwide]",
                "iso-3166-1-codes": [
                  "XW"
                ]
              }
            }
          ],
          "track-count": 17,
          "media": [
            {
              "position": 1,
              "format": "Digital Media",
              "track": [
                {
                  "id": "a2fcb379-7638-4f27-b2e4-5242a69ed09c",
                  "number": "6",
                  "title": "Blow Your Mind (Mwah)",
                  "length": 178000
                }
              ],
              "track-count": 17,
              "track-offset": 5
            }
          ]
        },
        {
          "id": "eb66eaa0-acb6-4317-91ea-ff65b7813ec0",
          "title": "Dua Lipa",
          "status-id": "518ffc83-5cde-34df-8627-81bff5093d92",
          "status": "Promotion",
          "release-group": {
            "id": "abbc4905-c25f-4c67-8e2d-19329ec48b1f",
            "primary-type": "Album"
          },
          "date": "2017-06-02",
          "country": "XW",
          "release-events": [
            {
              "date": "2017-06-02",
              "area": {
                "id": "525d4e18-3d00-31b9-a58b-a146a916de8f",
                "name": "[Worldwide]",
                "sort-name": "[Worldwide]",
                "iso-3166-1-codes": [
                  "XW"
                ]
              }
            }
          ],
          "track-count": 12,
          "media": [
            {
              "position": 1,
              "format": "Digital Media",
              "track": [
                {
                  "id": "b74b2a35-900a-446c-8a63-7a5b01c82a5e",
                  "number": "6",
                  "title": "Blow Your Mind (Mwah)",
                  "length": 178000
                }
              ],
              "track-count": 12,
              "track-offset": 5
            }
          ]
        }
      ],
      "isrcs": [
        {
          "id": "GBAHT1600302"
        },
        {
          "id": "GBAHT1600331"
        }
      ],
      "tags": [
        {
          "count": 1,
          "name": "electropop"
        },
        {
          "count": 1,
          "name": "dance-pop"
        },
        {
          "count": 1,
          "name": "contemporary r&b"
        }
      ]
    }
  ]
}
```

