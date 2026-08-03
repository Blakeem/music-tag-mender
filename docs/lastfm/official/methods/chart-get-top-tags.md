Source: https://www.last.fm/api/show/chart.getTopTags
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# chart.getTopTags

Get the top artists chart

## Example URLs

**JSON:** [/2.0/?method=chart.gettoptags&api_key=YOUR_API_KEY&format=json](http://ws.audioscrobbler.com/2.0/?method=chart.gettoptags&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=chart.gettoptags&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=chart.gettoptags&api_key=YOUR_API_KEY)

## Params

**page** (Optional): The page number to fetch. Defaults to first page.
**limit** (Optional): The number of results to fetch per page. Defaults to 50.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<tags page="1" perPage="50" totalPages="5" total="250">
  <tag>
    <name>rock</name>
    <url>http://www.last.fm/tag/rock</url>
    <reach>309437</reach>
    <taggings>3064604</taggings>
    <streamable>1</streamable>
    <wiki>
      <published>Sun, 24 Oct 2010 17:40:33 +0000</published>
      <summary>
Rock music is a genre of music started in America. It h...
      </summary>
      <content>
Rock music is a genre of music started in America. It has its roots in 1940s and 1950s rock and roll and rockabilly, which evolved from blues, country music and other influences. According to the All Music Guide, “In its pu...
      </content>
    </wiki>
</tag>
...
</tags>
```

## Errors

- **2**: Invalid service - This service does not exist
- **3**: Invalid Method - No method with that name in this package
- **4**: Authentication Failed - You do not have permissions to access the service
- **5**: Invalid format - This service doesn't exist in that format
- **6**: Invalid parameters - Your request is missing a required parameter
- **7**: Invalid resource specified
- **8**: Operation failed - Something else went wrong
- **9**: Invalid session key - Please re-authenticate
- **10**: Invalid API key - You must be granted a valid key by last.fm
- **11**: Service Offline - This service is temporarily offline. Try again later.
- **13**: Invalid method signature supplied
- **16**: There was a temporary error processing your request. Please try again
- **26**: Suspended API key - Access for your account has been suspended, please contact Last.fm
- **29**: Rate limit exceeded - Your IP has made too many requests in a short period
