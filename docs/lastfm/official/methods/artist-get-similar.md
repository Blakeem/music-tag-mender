Source: https://www.last.fm/api/show/artist.getSimilar
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# artist.getSimilar

Get all the artists similar to this artist

## Example URLs

**JSON:** [/2.0/?method=artist.getsimilar&artist=cher&api_key=YOUR_API_KEY&format=json](http://ws.audioscrobbler.com/2.0/?method=artist.getsimilar&artist=cher&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=artist.getsimilar&artist=cher&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=artist.getsimilar&artist=cher&api_key=YOUR_API_KEY)

## Params

**limit** (Optional): Limit the number of similar artists returned
**artist** (Required (unless mbid)]: The artist name
**autocorrect[0|1]** (Optional): Transform misspelled artist names into correct artist names, returning the correct version instead. The corrected artist name will be returned in the response.
**mbid** (Optional): The musicbrainz id for the artist
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<similarartists artist="Cher">
  <artist>
    <name>Sonny & Cher</name>
    <mbid>3d6e4b6d-2700-458c-9722-9021965a8164</mbid>
    <match>1</match>
    <url>www.last.fm/music/Sonny%2B%2526%2BCher</url>
    <image size="small">http://userserve-ak.last.fm/serve/34/71168880.png</image>
    <image size="medium">http://userserve-ak.last.fm/serve/64/71168880.png</image>
    <image size="large">http://userserve-ak.last.fm/serve/126/71168880.png</image>
    <image size="extralarge">http://userserve-ak.last.fm/serve/252/71168880.png</image>
    <image size="mega">http://userserve-ak.last.fm/serve/500/71168880/Sonny++Cher.png</image>
    <streamable>1</streamable>
  </artist>
  ...
</similarartists>
```

## Attributes

- **match**: A similarity value between 0 (not similar) and 1 (very similar)

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
