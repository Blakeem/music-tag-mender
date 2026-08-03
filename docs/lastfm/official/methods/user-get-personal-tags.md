Source: https://www.last.fm/api/show/user.getPersonalTags
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# user.getPersonalTags

Get the user's personal tags

## Example URLs

**JSON:** [/2.0/?method=user.getpersonaltags&user=rj&tag=rock&taggingtype=artist&api_key=YOUR...](http://ws.audioscrobbler.com/2.0/?method=user.getpersonaltags&user=rj&tag=rock&taggingtype=artist&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=user.getpersonaltags&user=rj&tag=rock&taggingtype=artist&api_key=YOUR...](http://ws.audioscrobbler.com/2.0/?method=user.getpersonaltags&user=rj&tag=rock&taggingtype=artist&api_key=YOUR_API_KEY)

## Params

**user** (Required): The user who performed the taggings.
**tag** (Required): The tag you're interested in.
**taggingtype[artist|album|track]** (Required): The type of items which have been tagged
**limit** (Optional): The number of results to fetch per page. Defaults to 50.
**page** (Optional): The page number to fetch. Defaults to first page.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<taggings user="RJ" tag="rock" page="1" perPage="50" totalPages="1" total="11">
  <artists>
    <artist>
      <name>John Hammond</name>
      <mbid>d83e599c-2d5a-44ec-b727-587e1455b1b5</mbid>
      <url>http://www.last.fm/music/John+Hammond</url>
      <streamable>1</streamable>
      <image size="small">http://userserve-ak.last.fm/serve/34/255418.jpg</image>
      <image size="medium">http://userserve-ak.last.fm/serve/64/255418.jpg</image>
      <image size="large">http://userserve-ak.last.fm/serve/126/255418.jpg</image>
      <image size="extralarge">http://userserve-ak.last.fm/serve/252/255418.jpg</image>
      <image size="mega">http://userserve-ak.last.fm/serve/_/255418/John+Hammond.jpg</image>
    </artist>
  </artists>
</taggings>
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
