Source: https://www.last.fm/api/show/tag.getInfo
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# tag.getInfo

Get the metadata for a tag

## Example URLs

**JSON:** [/2.0/?method=tag.getinfo&tag=disco&api_key=YOUR_API_KEY&format=json](http://ws.audioscrobbler.com/2.0/?method=tag.getinfo&tag=disco&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=tag.getinfo&tag=disco&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=tag.getinfo&tag=disco&api_key=YOUR_API_KEY)

## Params

**lang** (Optional): The language to return the wiki in, expressed as an ISO 639 alpha-2 code.
**tag** (Required): The tag name
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<tag>
    <name>disco</name>
    <url>http://www.last.fm/tag/disco</url>
    <reach>27199</reach>
    <taggings>114210</taggings>
    <streamable>1</streamable>
    <wiki>
        <published>Thu, 19 Aug 2010 03:22:16 +0000</published>
        <summary><!\[CDATA\[Disco is a genre of dance-oriented music\]\]></summary>
        <content><!\[CDATA\[Disco is a genre of dance-oriented music that originated in African American, gay and Hispanic American communities in 1970s. (truncated for readability in sample)\]\]></content>
    </wiki>
</tag>
```

## Attributes

- **reach**: The number of users that have used this tag
- **streamable**: A value of '1' indicates this tag can be used as a radio station
- **taggings**: The total number of times this tag has been used

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
