Source: https://www.last.fm/api/show/user.getTopTracks
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# user.getTopTracks

Get the top tracks listened to by a user. You can stipulate a time period. Sends the overall chart by default.

## Example URLs

**JSON:** [/2.0/?method=user.gettoptracks&user=rj&api_key=YOUR_API_KEY&format=json](http://ws.audioscrobbler.com/2.0/?method=user.gettoptracks&user=rj&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=user.gettoptracks&user=rj&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=user.gettoptracks&user=rj&api_key=YOUR_API_KEY)

## Params

**user** (Required): The user name to fetch top tracks for.
**period** (Optional): overall | 7day | 1month | 3month | 6month | 12month - The time period over which to retrieve top tracks for.
**limit** (Optional): The number of results to fetch per page. Defaults to 50.
**page** (Optional): The page number to fetch. Defaults to first page.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<toptracks user="RJ" type="overall">
  <track rank="1">
    <name>Learning to Live</name>
    <playcount>42</playcount>
    <mbid/>
    <url>
      http://www.last.fm/music/Dream+Theater/_/Learning+to+Live
    </url>
    <streamable fulltrack="0">1</streamable>
    <artist>
      <name>Dream Theater</name>
      <mbid>28503ab7-8bf2-4666-a7bd-2644bfc7cb1d</mbid>
      <url>http://www.last.fm/music/Dream+Theater</url>
    </artist>
    <image size="small">...</image>
    <image size="medium">...</image>
    <image size="large">...</image>
  </track>
  ...
</toptracks>
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
