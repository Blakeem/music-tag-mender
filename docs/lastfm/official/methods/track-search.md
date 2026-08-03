Source: https://www.last.fm/api/show/track.search
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# track.search

Search for a track by track name. Returns track matches sorted by relevance.

## Example URLs

**JSON:** [/2.0/?method=track.search&track=Believe&api_key=YOUR_API_KEY&format=json](http://ws.audioscrobbler.com/2.0/?method=track.search&track=Believe&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=track.search&track=Believe&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=track.search&track=Believe&api_key=YOUR_API_KEY)

## Params

**limit** (Optional): The number of results to fetch per page. Defaults to 30.
**page** (Optional): The page number to fetch. Defaults to first page.
**track** (Required): The track name
**artist** (Optional): Narrow your search by specifying an artist.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<results for="Believe" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:Query role="request" searchTerms="Believe" startPage="1"/>
  <opensearch:totalResults>25329</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <opensearch:itemsPerPage>20</opensearch:itemsPerPage>
  <trackmatches>
    <track>
      <name>Believe</name>
      <artist>Disturbed</artist>
      <url>http://www.last.fm/music/Disturbed/_/Believe</url>
      <streamable fulltrack="0">1</streamable>
      <listeners>66068</listeners>
      <image size="small">...</image>
    </track>
    ...
  </trackmatches>
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
