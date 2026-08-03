Source: https://musicbrainz.org/doc/MusicBrainz_API/Examples
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Examples, revision #78297
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/examples.md for focused reading.

---

### URL

Request: [https://musicbrainz.org/ws/2/url/46d8f693-52e4-4d03-936f-7ca8459019a7](https://musicbrainz.org/ws/2/url/46d8f693-52e4-4d03-936f-7ca8459019a7)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <url id="46d8f693-52e4-4d03-936f-7ca8459019a7">
    <resource>https://www.arvopart.ee/</resource>
  </url>
</metadata>
```

JSON Response

```
  {
    id: "46d8f693-52e4-4d03-936f-7ca8459019a7",
    resource: "https://www.arvopart.ee/"
  }
```

Request: [https://musicbrainz.org/ws/2/url?resource=https://www.nin.com/](https://musicbrainz.org/ws/2/url?resource=https://www.nin.com/)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <url id="bd9db597-5ccf-427d-94d9-c3bd23c00f37">
    <resource>https://www.nin.com/</resource>
  </url>
</metadata>
```

JSON Response

```
  {
    resource: "https://www.nin.com/",
    id: "bd9db597-5ccf-427d-94d9-c3bd23c00f37"
  }
```

Request: [https://musicbrainz.org/ws/2/url?resource=http://www.madonna.com/&resource=https://www.ladygaga.com/](https://musicbrainz.org/ws/2/url?resource=http://www.madonna.com/&resource=https://www.ladygaga.com/)

XML Response

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">
  <url-list count="2">
    <url id="15d85466-6ad6-4407-8a22-4e4bc183529c">
      <resource>https://www.ladygaga.com/</resource>
    </url>
    <url id="b663423b-9b54-4067-9674-fffaecf68851">
      <resource>http://www.madonna.com/</resource>
    </url>
  </url-list>
</metadata>
```

JSON Response

```
  {
    url-offset: 0,
    urls: [
      {
        id: "15d85466-6ad6-4407-8a22-4e4bc183529c",
        resource: "https://www.ladygaga.com/"
      },
      {
        id: "b663423b-9b54-4067-9674-fffaecf68851",
        resource: "http://www.madonna.com/"
      }
    ],
    url-count: 2
  }
```

