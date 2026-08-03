Source: https://www.last.fm/api/show/artist.getCorrection
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# artist.getCorrection

Use the last.fm corrections data to check whether the supplied artist has a correction to a canonical artist

## Example URLs

**JSON:** [/2.0/?method=artist.getcorrection&artist=Guns and Roses&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=artist.getcorrection&artist=Guns%20and%20Roses&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=artist.getcorrection&artist=Guns and Roses&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=artist.getcorrection&artist=Guns%20and%20Roses&api_key=YOUR_API_KEY)

## Params

**artist** (Required): The artist name to correct.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<corrections>
  <correction index="0">
    <artist>
      <name>Guns N' Roses</name>
      <mbid>eeb1195b-f213-4ce1-b28c-8565211f8e43</mbid>
      <url>http://www.last.fm/music/Guns+N%27+Roses</url>
    </artist>
  </correction>
</corrections>
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
