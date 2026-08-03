Source: https://www.last.fm/api/show/album.search
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# album.search

Search for an album by name. Returns album matches sorted by relevance.

## Example URLs

**JSON:** [/2.0/?method=album.search&album=believe&api_key=YOUR_API_KEY&format=json](http://ws.audioscrobbler.com/2.0/?method=album.search&album=believe&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=album.search&album=believe&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=album.search&album=believe&api_key=YOUR_API_KEY)

## Params

**limit** (Optional): The number of results to fetch per page. Defaults to 30.
**page** (Optional): The page number to fetch. Defaults to first page.
**album** (Required): The album name
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<results for="believe">
  <opensearch:Query role="request" searchTerms="believe" startPage="1"/>
  <opensearch:totalResults>734</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <opensearch:itemsPerPage>20</opensearch:itemsPerPage>
  <albummatches>
    <album>
      <name>Make Believe</name>
      <artist>Weezer</artist>
      <id>2025180</id>
      <url>http://www.last.fm/music/Weezer/Make+Believe</url>
      <image size="small">http://userserve-ak.last.fm/serve/34/8673675.jpg</image>
      <image size="medium">http://userserve-ak.last.fm/serve/64/8673675.jpg</image>
      <image size="large">http://userserve-ak.last.fm/serve/126/8673675.jpg</image>
      <streamable>0</streamable>
    </album>
    ...
  </albummatches>
</results>
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
