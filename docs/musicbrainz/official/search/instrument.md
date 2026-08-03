Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## Instrument

#### Example

[https://musicbrainz.org/ws/2/instrument/?query=Nose](https://musicbrainz.org/ws/2/instrument/?query=Nose)

#### Search Fields

The [instrument](https://musicbrainz.org/doc/Instrument) index contains these fields you can search:

| Field | Description |
| --- | --- |
| alias | (part of) any [alias](https://musicbrainz.org/doc/Aliases) attached to the instrument (diacritics are ignored) |
| comment | (part of) the instrument's disambiguation comment |
| description | (part of) the description of the instrument (in English) |
| iid | the MBID of the instrument |
| instrument | (part of) the instrument's name (diacritics are ignored) |
| instrumentaccent | (part of) the instrument's name (with the specified diacritics) |
| tag | (part of) a tag attached to the instrument |
| type | the instrument's [type](https://musicbrainz.org/doc/Instrument#Type) |

If you don't specify a field, the terms will be searched for in the _alias_, _description_ and _instrument_ fields.

### Xml

```
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<metadata created="2017-03-12T18:06:29.271Z" xmlns="http://musicbrainz.org/ns/mmd-2.0#" xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">
  <instrument-list count="2" offset="0">
    <instrument id="e2e7de25-20d5-4c3f-8a23-2b99d3e44730" type="Wind instrument" ext:score="100">
      <name>nose flute</name>
      <description>The nose flute is a flute played by the nose commonly found in countries in and around the Pacific.</description>
      <alias-list>
        <alias locale="es" sort-name="flauta nasal" type="Instrument name" primary="primary">flauta nasal</alias>
        <alias locale="fr" sort-name="flûte à nez" type="Instrument name" primary="primary">flûte à nez</alias>
        <alias locale="de" sort-name="Nasenflöte (traditionell)" type="Instrument name" primary="primary">Nasenflöte (traditionell)</alias>
        <alias locale="fi" sort-name="nenähuilu" type="Instrument name" primary="primary">nenähuilu</alias>
        <alias locale="nl" sort-name="neusfluit" type="Instrument name" primary="primary">neusfluit</alias>
        <alias locale="et" sort-name="ninaflööt" type="Instrument name" primary="primary">ninaflööt</alias>
        <alias locale="en" sort-name="nose flute" type="Instrument name" primary="primary">nose flute</alias>
        <alias locale="ja" sort-name="鼻笛" type="Instrument name" primary="primary">鼻笛</alias>
      </alias-list>
    </instrument>
    <instrument id="3d082a7d-e8d9-4c7b-b8d0-513883a7d586" type="Wind instrument" ext:score="100">
      <name>nose whistle</name>
      <description>The nose whistle (also known as the Humanatone) is a simple instrument played with the nose. The stream of air is directed over an edge in the instrument and the frequency of the notes produced is controlled by the volume of air.</description>
      <alias-list>
        <alias sort-name="Humanatone">Humanatone</alias>
        <alias locale="de" sort-name="Nasenflöte" type="Instrument name" primary="primary">Nasenflöte</alias>
        <alias locale="nl" sort-name="neusfluitje" type="Instrument name" primary="primary">neusfluitje</alias>
        <alias locale="et" sort-name="ninavile" type="Instrument name" primary="primary">ninavile</alias>
        <alias locale="en" sort-name="nose whistle" type="Instrument name" primary="primary">nose whistle</alias>
        <alias locale="fr" sort-name="sifflet à nez" type="Instrument name" primary="primary">sifflet à nez</alias>
        <alias locale="ja" sort-name="鼻ホイッスル" type="Instrument name" primary="primary">鼻ホイッスル</alias>
      </alias-list>
    </instrument>
  </instrument-list>
</metadata>
```

### Json

```
{
  "created": "2017-03-12T18:06:29.271Z",
  "count": 2,
  "offset": 0,
  "instruments": [
    {
      "id": "e2e7de25-20d5-4c3f-8a23-2b99d3e44730",
      "type": "Wind instrument",
      "score": "100",
      "name": "nose flute",
      "description": "The nose flute is a flute played by the nose commonly found in countries in and around the Pacific.",
      "aliases": [
        {
          "sort-name": "flauta nasal",
          "name": "flauta nasal",
          "locale": "es",
          "type": "Instrument name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "flûte à nez",
          "name": "flûte à nez",
          "locale": "fr",
          "type": "Instrument name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Nasenflöte (traditionell)",
          "name": "Nasenflöte (traditionell)",
          "locale": "de",
          "type": "Instrument name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "nenähuilu",
          "name": "nenähuilu",
          "locale": "fi",
          "type": "Instrument name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "neusfluit",
          "name": "neusfluit",
          "locale": "nl",
          "type": "Instrument name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "ninaflööt",
          "name": "ninaflööt",
          "locale": "et",
          "type": "Instrument name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "nose flute",
          "name": "nose flute",
          "locale": "en",
          "type": "Instrument name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "鼻笛",
          "name": "鼻笛",
          "locale": "ja",
          "type": "Instrument name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        }
      ]
    },
    {
      "id": "3d082a7d-e8d9-4c7b-b8d0-513883a7d586",
      "type": "Wind instrument",
      "score": "100",
      "name": "nose whistle",
      "description": "The nose whistle (also known as the Humanatone) is a simple instrument played with the nose. The stream of air is directed over an edge in the instrument and the frequency of the notes produced is controlled by the volume of air.",
      "aliases": [
        {
          "sort-name": "Humanatone",
          "name": "Humanatone",
          "locale": null,
          "type": null,
          "primary": null,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "Nasenflöte",
          "name": "Nasenflöte",
          "locale": "de",
          "type": "Instrument name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "neusfluitje",
          "name": "neusfluitje",
          "locale": "nl",
          "type": "Instrument name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "ninavile",
          "name": "ninavile",
          "locale": "et",
          "type": "Instrument name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "nose whistle",
          "name": "nose whistle",
          "locale": "en",
          "type": "Instrument name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "sifflet à nez",
          "name": "sifflet à nez",
          "locale": "fr",
          "type": "Instrument name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        },
        {
          "sort-name": "鼻ホイッスル",
          "name": "鼻ホイッスル",
          "locale": "ja",
          "type": "Instrument name",
          "primary": true,
          "begin-date": null,
          "end-date": null
        }
      ]
    }
  ]
}
```

