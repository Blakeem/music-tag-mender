Source: https://musicbrainz.org/doc/MusicBrainz_API/Search
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Search, revision #78397
Version: ws/2 (current production version)
Retrieved: 2026-07-07
Split from official/search.md: common query syntax and parameters shared by all search endpoints. Per-entity search field tables live in official/search/<entity>.md.

---

<table class="wikitable " style="text-align:center; background:#FFFDDF; width:100%; margin-top:0.5em; margin-bottom:0.5em;"><tbody><tr><td style="border:1px solid #ffcc00;"><b>Status:</b> This includes features to be released soon as part of the <a class="external text" href="https://blog.metabrainz.org/2025/03/18/schema-change-release-may-19-2025/" rel="nofollow">MusicBrainz 2025 Q2 schema change</a>.</td></tr></tbody></table>

The MusicBrainz API search requests provide a way to search for MusicBrainz entities based on different sorts of queries. The results are returned in either XML (matching the [MMD](https://musicbrainz.org/doc/MusicBrainz_XML_Meta_Data)) or JSON format, and are provided by a [search server](https://musicbrainz.org/doc/Search_Server) built using Lucene technology.

This sections lists the parameters common to all resources.

<table border="1" class="wikitable "><tbody><tr><td><b>type</b></td><td>Selects the entity index to be searched: annotation, area, artist, cdstub, event, instrument, label, place, recording, release, release-group, series, tag, work, url</td></tr><tr><td><b>fmt</b></td><td>Selects the representation of the results. Defaults to <code>xml</code>, but can also be set to <code>json</code>.</td></tr><tr><td><b>query</b></td><td>Lucene search query. This is mandatory</td></tr><tr><td><b>limit</b></td><td>An integer value defining how many entries should be returned. Only values between 1 and 100 (both inclusive) are allowed. If not given, this defaults to 25.</td></tr><tr><td><b>offset</b></td><td>Return search results starting at a given offset. Used for paging through more than one page of results.</td></tr><tr><td><b>dismax</b></td><td>If set to "true", switches the Solr query parser from edismax to <a class="external text" href="https://lucene.apache.org/solr/guide/6_6/the-dismax-query-parser.html" rel="nofollow">dismax</a>, which will escape certain special query syntax characters by default for ease of use. This is equivalent to switching from the "Indexed search with advanced query syntax" method to the plain "Indexed search" method on the website. Defaults to "false".</td></tr><tr><td><b>version</b></td><td><a href="/doc/MusicBrainz_XML_Meta_Data">MMD</a> version, defaults to 2, version 1 is no longer supported since <a class="external text" href="https://blog.metabrainz.org/2018/06/15/musicbrainz-search-overhaul/" rel="nofollow">search overhaul in 2018</a>.</td></tr></tbody></table>

  
The query field supports the full Lucene Search syntax; you can find a detailed guide at [Lucene Search Syntax](https://lucene.apache.org/core/7_7_2/queryparser/org/apache/lucene/queryparser/classic/package-summary.html#package.description). For example, you can set conditions while searching for a name with the AND operator.

Example: [https://musicbrainz.org/ws/2/recording?query=%22we%20will%20rock%20you%22%20AND%20arid:0383dadf-2a4e-4d10-a46a-e9e041da8eb3](https://musicbrainz.org/ws/2/recording?query=%22we%20will%20rock%20you%22%20AND%20arid:0383dadf-2a4e-4d10-a46a-e9e041da8eb3) will find any recordings of "We Will Rock You" by Queen.

  
To search for fields that are unknown or null, use the following syntax -

\-search\_field:\*

Example: For releases with no format set can be searched via: [\-format:\*](https://musicbrainz.org/search?query=-format%3A*&type=release&limit=25&method=advanced)

  
Numeric count based fields can be searched for by looking for 0

Example: [https://musicbrainz.org/ws/2/release-group/?query=releases:0](https://musicbrainz.org/ws/2/release-group/?query=releases:0)

  
To perform a literal search, you'll need to [escape characters special to Lucene](https://lucene.apache.org/core/4_3_0/queryparser/org/apache/lucene/queryparser/classic/package-summary.html#Escaping_Special_Characters). This is in addition to any URL encoding.

Example: [https://musicbrainz.org/ws/2/artist/?query=ac%5C%2Fdc](https://musicbrainz.org/ws/2/artist/?query=ac%5C%2Fdc)

  

