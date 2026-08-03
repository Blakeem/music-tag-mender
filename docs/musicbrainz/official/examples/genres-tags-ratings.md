Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

## Genres, tags and ratings

You can include _genres_, _tags_ and _ratings_ to get this secondary data. Below is an example of a request including all of the three. With authentication, you can include _user-genres_, _user-tags_ and _user-ratings_ to get a list of only the genres, tags and ratings added by the authenticated user.

Request: [http://musicbrainz.org/ws/2/artist/db92a151-1ac2-438b-bc43-b82e149ddd50?inc=aliases+genres+tags+ratings](http://musicbrainz.org/ws/2/artist/db92a151-1ac2-438b-bc43-b82e149ddd50?inc=aliases+genres+tags+ratings)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <artist id="db92a151-1ac2-438b-bc43-b82e149ddd50" type="Person" type-id="b6e035f4-3ce9-331c-97df-83397230b0df">
    <name>Rick Astley</name>
    <sort-name>Astley, Rick</sort-name>
    <disambiguation>English singer, songwriter and radio personality</disambiguation>
    <ipi>00159849414</ipi>
    <ipi-list>
      <ipi>00159849414</ipi>
    </ipi-list>
    <isni-list>
      <isni>0000000120203276</isni>
    </isni-list>
    <gender id="36d3d30a-839d-3eda-8cb3-29be4384e4a9">Male</gender>
    <country>GB</country>
    <area id="8a754a16-0027-3a29-b6d7-2b40ea0481ed">
      <name>United Kingdom</name>
      <sort-name>United Kingdom</sort-name>
      <iso-3166-1-code-list>
        <iso-3166-1-code>GB</iso-3166-1-code>
      </iso-3166-1-code-list>
    </area>
    <begin-area id="71baf484-64ec-4482-9ad2-225aa3b9a337">
      <name>Newton-le-Willows</name>
      <sort-name>Newton-le-Willows</sort-name>
    </begin-area>
    <life-span>
      <begin>1966-02-06</begin>
    </life-span>
    <alias-list count="5">
      <alias sort-name="Astley, Richard Paul" type="Legal name" type-id="d4dcd0c0-b341-3612-a332-c0ce797b25cf">Richard Paul Astley</alias>
    </alias-list>
    <tag-list>
      <tag count="2">
        <name>adult contemporary</name>
      </tag>
      <tag count="2">
        <name>blue-eyed soul</name>
      </tag>
      <tag count="0">
        <name>british</name>
      </tag>
      <tag count="0">
        <name>classic pop and rock</name>
      </tag>
      <tag count="2">
        <name>dance-pop</name>
      </tag>
      <tag count="0">
        <name>english</name>
      </tag>
      <tag count="2">
        <name>pop</name>
      </tag>
      <tag count="0">
        <name>uk</name>
      </tag>
    </tag-list>
    <genre-list>
      <genre count="2" id="650bda08-20ca-4012-b873-3b68c5768bdf">
        <name>blue-eyed soul</name>
      </genre>
      <genre count="2" id="b739a895-85ed-4ad3-8717-4e9ef5387dd8">
        <name>dance-pop</name>
      </genre>
      <genre count="2" id="911c7bbb-172d-4df8-9478-dbff4296e791">
        <name>pop</name>
      </genre>
    </genre-list>
    <rating votes-count="2">4.5</rating>
  </artist>
</metadata>
```

JSON Response

```
  {
    id: "db92a151-1ac2-438b-bc43-b82e149ddd50",
    name: "Rick Astley",
    sort-name: "Astley, Rick",
    disambiguation: "English singer, songwriter and radio personality",
    type-id: "b6e035f4-3ce9-331c-97df-83397230b0df",
    type: "Person",
    gender-id: "36d3d30a-839d-3eda-8cb3-29be4384e4a9",
    gender: "Male",
    life-span: {
      end: null,
      ended: false,
      begin: "1966-02-06"
    },
    country: "GB"
    area: {
      id: "8a754a16-0027-3a29-b6d7-2b40ea0481ed",
      name: "United Kingdom",
      sort-name: "United Kingdom",
      disambiguation: "",
      iso-3166-1-codes: ["GB"]
    },
    begin-area: {
      id: "71baf484-64ec-4482-9ad2-225aa3b9a337"
      name: "Newton-le-Willows",
      sort-name: "Newton-le-Willows",
      disambiguation: "",
    },
    end-area: null,
    ipis: ["00159849414"],
    isnis: ["0000000120203276"],
    aliases: [
      {
        name: "Richard Paul Astley",
        sort-name: "Astley, Richard Paul",
        type-id: "d4dcd0c0-b341-3612-a332-c0ce797b25cf",
        type: "Legal name",
        locale: null,
        primary: null,
        begin: null,
        end: null
        ended: false,
      }
    ],
    genres: [
      {
        name: "blue-eyed soul",
        id: "650bda08-20ca-4012-b873-3b68c5768bdf",
        count: 2,
        disambiguation: "",
      },
      {
        name: "dance-pop",
        id: "b739a895-85ed-4ad3-8717-4e9ef5387dd8",
        count: 2,
        disambiguation: "",
      },
      {
        name: "pop",
        id: "911c7bbb-172d-4df8-9478-dbff4296e791",
        count: 2,
        disambiguation: "",
      },
    ],
    tags: [
      {name: "blue-eyed soul", count: 2},
      {name: "british", count: 0}
    ],
    rating: {
      value: 4.5,
      votes-count: 2
    }
  }
```

  
Additionally, you can make a request to the _/tag_ or _/rating_ endpoint (requires authentication) to get _only_ the tags or ratings assigned to a specific entity by the authenticated user.

Request: [https://musicbrainz.org/ws/2/tag?id=ed35bc92-2b5a-4ddf-96d2-51af9ab239e7&entity=artist](https://musicbrainz.org/ws/2/tag?id=ed35bc92-2b5a-4ddf-96d2-51af9ab239e7&entity=artist)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <user-tag-list>
    <user-tag>
      <name>classical</name>
    </user-tag>
  </user-tag-list>
</metadata>
```

JSON Response

```
  {
    user-tags: [
      {
        name: "classical"
      },
      {
        name: "production music"
      }
    ]
  }
```

Request: [https://musicbrainz.org/ws/2/rating?id=ed35bc92-2b5a-4ddf-96d2-51af9ab239e7&entity=artist](https://musicbrainz.org/ws/2/rating?id=ed35bc92-2b5a-4ddf-96d2-51af9ab239e7&entity=artist)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <user-rating>80</user-rating>
</metadata>
```

JSON Response

```
  {
    user-rating: {
      value: 80
    }
  }
