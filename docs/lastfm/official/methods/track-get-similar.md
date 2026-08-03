Source: https://www.last.fm/api/show/track.getSimilar
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# track.getSimilar

Get the similar tracks for this track on Last.fm, based on listening data.

## Example URLs

**JSON:** [/2.0/?method=track.getsimilar&artist=cher&track=believe&api_key=YOUR_API_KEY&forma...](http://ws.audioscrobbler.com/2.0/?method=track.getsimilar&artist=cher&track=believe&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=track.getsimilar&artist=cher&track=believe&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=track.getsimilar&artist=cher&track=believe&api_key=YOUR_API_KEY)

## Params

**track** (Required (unless mbid)]: The track name
**artist** (Required (unless mbid)]: The artist name
**mbid** (Optional): The musicbrainz id for the track
**autocorrect[0|1]** (Optional): Transform misspelled artist and track names into correct artist and track names, returning the correct version instead. The corrected artist and track name will be returned in the response.
**limit** (Optional): Maximum number of similar tracks to return
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<similartracks track="Believe" artist="Cher">
  <track>
    <name>Ray of Light</name>
    <mbid/>
    <match>10.95</match>
    <url>http://www.last.fm/music/Madonna/_/Ray+of+Light</url>
    <streamable fulltrack="0">1</streamable>
    <artist>
      <name>Madonna</name>
      <mbid>79239441-bfd5-4981-a70c-55c3f15c1287</mbid>
      <url>http://www.last.fm/music/Madonna</url>
    </artist>
    <image size="small">http://cdn.last.fm/coverart/50x50/1934.jpg</image>
    <image size="medium">http://cdn.last.fm/coverart/130x130/1934.jpg</image>
    <image size="large">http://cdn.last.fm/coverart/130x130/1934.jpg</image>
  </track>
  ...
</similartracks>
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
