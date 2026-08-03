Source: https://www.last.fm/api/show/track.getInfo
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# track.getInfo

Get the metadata for a track on Last.fm using the artist/track name or a musicbrainz id.

## Example URLs

**JSON:** [/2.0/?method=track.getInfo&api_key=YOUR_API_KEY&artist=cher&track=believe&format=json](http://ws.audioscrobbler.com/2.0/?method=track.getInfo&api_key=YOUR_API_KEY&artist=cher&track=believe&format=json)
**XML:** [/2.0/?method=track.getInfo&api_key=YOUR_API_KEY&artist=cher&track=believe](http://ws.audioscrobbler.com/2.0/?method=track.getInfo&api_key=YOUR_API_KEY&artist=cher&track=believe)

## Params

**mbid** (Optional): The musicbrainz id for the track
**track** (Required (unless mbid)]: The track name
**artist** (Required (unless mbid)]: The artist name
**username** (Optional): The username for the context of the request. If supplied, the user's playcount for this track and whether they have loved the track is included in the response.
**autocorrect[0|1]** (Optional): Transform misspelled artist and track names into correct artist and track names, returning the correct version instead. The corrected artist and track name will be returned in the response.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<track>
  <id>1019817</id>
  <name>Believe</name>
  <mbid/>
  <url>http://www.last.fm/music/Cher/_/Believe</url>
  <duration>240000</duration>
  <streamable fulltrack="1">1</streamable>
  <listeners>69572</listeners>
  <playcount>281445</playcount>
  <artist>
    <name>Cher</name>
    <mbid>bfcc6d75-a6a5-4bc6-8282-47aec8531818</mbid>
    <url>http://www.last.fm/music/Cher</url>
  </artist>
  <album position="1">
    <artist>Cher</artist>
    <title>Believe</title>
    <mbid>61bf0388-b8a9-48f4-81d1-7eb02706dfb0</mbid>
    <url>http://www.last.fm/music/Cher/Believe</url>
    <image size="small">http://userserve-ak.last.fm/serve/34/8674593.jpg</image>
    <image size="medium">http://userserve-ak.last.fm/serve/64/8674593.jpg</image>
    <image size="large">http://userserve-ak.last.fm/serve/126/8674593.jpg</image>
  </album>
  <toptags>
    <tag>
      <name>pop</name>
      <url>http://www.last.fm/tag/pop</url>
    </tag>
    ...
  </toptags>
  <wiki>
    <published>Sun, 27 Jul 2008 15:44:58 +0000</published>
    <summary>...</summary>
    <content>...</content>
  </wiki>
</track>
```

## Attributes

- **duration**: In milliseconds
- **fulltrack**: An attribute value of 1 indicates a full length preview is available for streaming
- **streamable**: A tag value of 1 indicates a 30 second preview of this song is available for streaming

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
