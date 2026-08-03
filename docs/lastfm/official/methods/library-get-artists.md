Source: https://www.last.fm/api/show/library.getArtists
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# library.getArtists

A paginated list of all the artists in a user's library, with play counts and tag counts.

## Example URLs

**JSON:** [/2.0/?method=library.getartists&api_key=YOUR_API_KEY&user=joanofarctan&format=json](http://ws.audioscrobbler.com/2.0/?method=library.getartists&api_key=YOUR_API_KEY&user=joanofarctan&format=json)
**XML:** [/2.0/?method=library.getartists&api_key=YOUR_API_KEY&user=joanofarctan](http://ws.audioscrobbler.com/2.0/?method=library.getartists&api_key=YOUR_API_KEY&user=joanofarctan)

## Params

**user** (Required): The user whose library you want to fetch.
**limit** (Optional): The number of results to fetch per page. Defaults to 50.
**page** (Optional): The page number you wish to scan to.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<artists user="RJ" page="1" perPage="50" totalPages="20">
  <artist rank="1">
    <name>Dream Theater</name>
    <playcount>1346</playcount>
    <tagcount>0</tagcount>
    <mbid>28503ab7-8bf2-4666-a7bd-2644bfc7cb1d</mbid>
    <url>http://www.last.fm/music/Dream+Theater</url>
    <streamable>1</streamable>
    <image size="small">http://userserve-ak.last.fm/serve/50/95853.jpg</image>
    <image size="medium">http://userserve-ak.last.fm/serve/85/95853.jpg</image>
    <image size="large">http://userserve-ak.last.fm/serve/160/95853.jpg</image>
  </artist>
  ...
</artists>
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
