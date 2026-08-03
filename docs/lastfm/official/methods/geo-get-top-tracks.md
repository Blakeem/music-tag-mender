Source: https://www.last.fm/api/show/geo.getTopTracks
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# geo.getTopTracks

Get the most popular tracks on Last.fm last week by country

## Example URLs

**JSON:** [/2.0/?method=geo.gettoptracks&country=spain&api_key=YOUR_API_KEY&format=json](http://ws.audioscrobbler.com/2.0/?method=geo.gettoptracks&country=spain&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=geo.gettoptracks&country=spain&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=geo.gettoptracks&country=spain&api_key=YOUR_API_KEY)

## Params

**country** (Required): A country name, as defined by the ISO 3166-1 country names standard
**location** (Optional): A metro name, to fetch the charts for (must be within the country specified)
**limit** (Optional): The number of results to fetch per page. Defaults to 50.
**page** (Optional): The page number to fetch. Defaults to first page.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<toptracks country="Spain">
  <track rank="1">
    <name>Violet Hill</name>
    <playcount>1055</playcount>
    <mbid/>
    <url>http://www.last.fm/music/Coldplay/_/Violet+Hill</url>
    <streamable fulltrack="0">1</streamable>
    <artist>
      <name>Coldplay</name>
      <mbid>cc197bad-dc9c-440d-a5b5-d52ba2e14234</mbid>
      <url>http://www.last.fm/music/Coldplay</url>
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
