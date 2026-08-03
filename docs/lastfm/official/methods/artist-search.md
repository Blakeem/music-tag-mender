Source: https://www.last.fm/api/show/artist.search
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# artist.search

Search for an artist by name. Returns artist matches sorted by relevance.

## Example URLs

**JSON:** [/2.0/?method=artist.search&artist=cher&api_key=YOUR_API_KEY&format=json](http://ws.audioscrobbler.com/2.0/?method=artist.search&artist=cher&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=artist.search&artist=cher&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=artist.search&artist=cher&api_key=YOUR_API_KEY)

## Params

**limit** (Optional): The number of results to fetch per page. Defaults to 30.
**page** (Optional): The page number to fetch. Defaults to first page.
**artist** (Required): The artist name
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<results for="cher" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:Query role="request" searchTerms="cher" startPage="1"/>
  <opensearch:totalResults>386</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <opensearch:itemsPerPage>20</opensearch:itemsPerPage>
  <artistmatches>
    <artist>
      <name>Cher</name>
      <mbid>bfcc6d75-a6a5-4bc6-8282-47aec8531818</mbid>
      <url>www.last.fm/music/Cher</url>
      <image_small>http://userserve-ak.last.fm/serve/50/342437.jpg</image_small>
      <image>http://userserve-ak.last.fm/serve/160/342437.jpg</image>
      <streamable>1</streamable>
    </artist>
	...
  </artistmatches>
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
