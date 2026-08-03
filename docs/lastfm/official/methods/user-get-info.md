Source: https://www.last.fm/api/show/user.getInfo
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# user.getInfo

Get information about a user profile.

## Example URLs

**JSON:** [/2.0/?method=user.getinfo&user=rj&api_key=YOUR_API_KEY&format=json](http://ws.audioscrobbler.com/2.0/?method=user.getinfo&user=rj&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=user.getinfo&user=rj&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=user.getinfo&user=rj&api_key=YOUR_API_KEY)

## Params

**user** (Optional): The user to fetch info for. Defaults to the authenticated user.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<user>
    <id>1000002</id>
    <name>RJ</name>
    <realname>Richard Jones </realname>
    <url>http://www.last.fm/user/RJ</url>
    <image>http://userserve-ak.last.fm/serve/126/8270359.jpg</image>
    <country>UK</country>
    <age>27</age>
    <gender>m</gender>
    <subscriber>1</subscriber>
    <playcount>54189</playcount>
    <playlists>4</playlists>
    <bootstrap>0</bootstrap>
    <registered unixtime="1037793040">2002-11-20 11:50</registered>
</user>
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
