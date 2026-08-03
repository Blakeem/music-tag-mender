Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## URL

#### Example

[http://musicbrainz.org/ws/2/url/?query=Hello](http://musicbrainz.org/ws/2/url/?query=Hello)

#### Search Fields

The [URL](https://musicbrainz.org/doc/URL) index contains the following fields you can search

| Field | Description |
| --- | --- |
| relationtype | the type of a relationship the URL is in (e.g. "wikidata") |
| targetid | the MBID of an entity related to the URL |
| targettype | an entity type related to the URL (e.g. "artist") |
| uid | the URL's MBID |
| url | the actual URL string |
| url\_ancestor | the actual URL string, but also returns any ancestor paths (e.g. "https://example.org/some/stuff" will match "https://example.org/some") |
| url\_descendent | the actual URL string, but also returns any descendant paths (e.g. "https://example.org/some/stuff" will match "https://example.org/some/stuff/here") |

#### Xml

```
<metadata created="2017-03-11T01:04:24.353Z" xmlns="http://musicbrainz.org/ns/mmd-2.0#" xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">
  <url-list count="0" offset="0"/>
</metadata>
```

#### Json

```
{
  "created": "2017-03-11T01:04:24.353Z",
  "count": 0,
  "offset": 0,
  "urls": [ ]
}
```

