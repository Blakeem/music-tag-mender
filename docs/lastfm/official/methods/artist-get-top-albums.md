Source: https://www.last.fm/api/show/artist.getTopAlbums
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# artist.getTopAlbums

Get the top albums for an artist on Last.fm, ordered by popularity.

## Example URLs

**JSON:** [/2.0/?method=artist.gettopalbums&artist=cher&api_key=YOUR_API_KEY&format=json](http://ws.audioscrobbler.com/2.0/?method=artist.gettopalbums&artist=cher&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=artist.gettopalbums&artist=cher&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=artist.gettopalbums&artist=cher&api_key=YOUR_API_KEY)

## Params

**artist** (Required (unless mbid)]: The artist name
**mbid** (Optional): The musicbrainz id for the artist
**autocorrect[0|1]** (Optional): Transform misspelled artist names into correct artist names, returning the correct version instead. The corrected artist name will be returned in the response.
**page** (Optional): The page number to fetch. Defaults to first page.
**limit** (Optional): The number of results to fetch per page. Defaults to 50.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<topalbums artist="Cher">
  <album rank="1">
    <name>Believe</name>
    <mbid>61bf0388-b8a9-48f4-81d1-7eb02706dfb0</mbid>
    <listeners>24486</listeners>
    <url>http://www.last.fm/music/Cher/Believe</url>
    <image size="small">...</image>
    <image size=" medium">...</image>
    <image size="large">...</image>
  </album>
  ...
</topalbums>
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
