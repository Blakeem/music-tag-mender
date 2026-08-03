Source: https://www.last.fm/api/show/album.getInfo
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# album.getInfo

Get the metadata and tracklist for an album on Last.fm using the album name or a musicbrainz id.

## Example URLs

**JSON:** [/2.0/?method=album.getinfo&api_key=YOUR_API_KEY&artist=Cher&album=Believe&format=json](http://ws.audioscrobbler.com/2.0/?method=album.getinfo&api_key=YOUR_API_KEY&artist=Cher&album=Believe&format=json)
**XML:** [/2.0/?method=album.getinfo&api_key=YOUR_API_KEY&artist=Cher&album=Believe](http://ws.audioscrobbler.com/2.0/?method=album.getinfo&api_key=YOUR_API_KEY&artist=Cher&album=Believe)

## Params

**artist** (Required unless mbid): The artist name
**album** (Required unless mbid): The album name
**mbid** (Optional): The musicbrainz id for the album
**autocorrect[0|1]** (Optional): Transform misspelled artist names into correct artist names, returning the correct version instead. The corrected artist name will be returned in the response.
**username** (Optional): The username for the context of the request. If supplied, the user's playcount for this album is included in the response.
**lang** (Optional): The language to return the biography in, expressed as an ISO 639 alpha-2 code.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<album>
  <name>Believe</name>
  <artist>Cher</artist>
  <id>2026126</id>
  <mbid>61bf0388-b8a9-48f4-81d1-7eb02706dfb0</mbid>
  <url>http://www.last.fm/music/Cher/Believe</url>
  <releasedate>6 Apr 1999, 00:00</releasedate>
  <image size="small">...</image>
  <image size="medium">...</image>
  <image size="large">...</image>
  <listeners>47602</listeners>
  <playcount>212991</playcount>
  <toptags>
    <tag>
      <name>pop</name>
      <url>http://www.last.fm/tag/pop</url>
    </tag>
    ...
  </toptags>
  <tracks>
    <track rank="1">
      <name>Believe</name>
      <duration>239</duration>
      <mbid/>
      <url>http://www.last.fm/music/Cher/_/Believe</url>
      <streamable fulltrack="0">1</streamable>
      <artist>
        <name>Cher</name>
        <mbid>bfcc6d75-a6a5-4bc6-8282-47aec8531818</mbid>
        <url>http://www.last.fm/music/Cher</url>
      </artist>
    </track>
    ...
  </tracks>
</album>
```

## Attributes

- **duration**: In seconds

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
