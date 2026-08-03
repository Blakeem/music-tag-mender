Source: https://www.last.fm/api/show/artist.getTags
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# artist.getTags

Get the tags applied by an individual user to an artist on Last.fm. If accessed as an authenticated service /and/ you don't supply a user parameter then this service will return tags for the authenticated user. To retrieve the list of top tags applied to an artist by all users use artist.getTopTags.

## Example URLs

**JSON:** [/2.0/?method=artist.getTags&artist=Red%20Hot%20Chili%20Peppers&user=RJ&api_key=YOU...](http://ws.audioscrobbler.com/2.0/?method=artist.getTags&artist=Red%20Hot%20Chili%20Peppers&user=RJ&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=artist.getTags&artist=Red%20Hot%20Chili%20Peppers&user=RJ&api_key=YOU...](http://ws.audioscrobbler.com/2.0/?method=artist.getTags&artist=Red%20Hot%20Chili%20Peppers&user=RJ&api_key=YOUR_API_KEY)

## Params

**artist** (Required (unless mbid)]: The artist name
**mbid** (Optional): The musicbrainz id for the artist
**user** (Optional): If called in non-authenticated mode you must specify the user to look up
**autocorrect[0|1]** (Optional): Transform misspelled artist names into correct artist names, returning the correct version instead. The corrected artist name will be returned in the response.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<tags artist="Sally Shapiro">
  <tag>
    <name>italo</name>
    <url>http://www.last.fm/tag/italo</url>
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
