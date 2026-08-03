Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md (per-entity search fields) for focused reading; common query syntax lives in official/search/syntax.md.

---

## Tag

#### Example

[http://musicbrainz.org/ws/2/tag/?query=shoegaze](http://musicbrainz.org/ws/2/tag/?query=shoegaze)

#### Search Fields

The [tag](https://musicbrainz.org/doc/Folksonomy_Tagging) index contains these fields you can search:

| Field | Description |
| --- | --- |
| tag | (part of) the tag's name |

#### Xml

```
<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#" xmlns:ext="http://musicbrainz.org/ns/ext#-2.0">
    <tag-list count="5" offset="0">
        <tag ext:score="100">
            <name>shoegaze</name>
        </tag>
        <tag ext:score="62">
            <name>rock shoegaze</name>
        </tag>
        <tag ext:score="62">
            <name>indie shoegaze</name>
        </tag>
        <tag ext:score="50">
            <name>doom metal ethereal shoegaze</name>
        </tag>
        <tag ext:score="31">
            <name>ambient folk classical electronic shoegaze alternative rock indie</name>
        </tag>
    </tag-list>
</metadata>
```

#### Json

```
{
  "created": "2013-02-05T08:20:52.180Z",
  "count": 5,
  "offset": 0,
  "tags": [
    {
      "score": "100",
      "name": "shoegaze"
    },
    {
      "score": "62",
      "name": "rock shoegaze"
    },
    {
      "score": "62",
      "name": "indie shoegaze"
    },
    {
      "score": "50",
      "name": "doom metal ethereal shoegaze"
    },
    {
      "score": "31",
      "name": "ambient folk classical electronic shoegaze alternative rock indie"
    }
  ]
}
```

