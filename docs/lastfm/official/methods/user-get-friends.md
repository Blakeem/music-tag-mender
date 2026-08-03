Source: https://www.last.fm/api/show/user.getFriends
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# user.getFriends

Get a list of the user's friends on Last.fm.

## Example URLs

**JSON:** [/2.0/?method=user.getfriends&user=rj&api_key=YOUR_API_KEY&format=json](http://ws.audioscrobbler.com/2.0/?method=user.getfriends&user=rj&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=user.getfriends&user=rj&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=user.getfriends&user=rj&api_key=YOUR_API_KEY)

## Params

**user** (Required): The last.fm username to fetch the friends of.
**recenttracks** (Optional): Whether or not to include information about friends' recent listening in the response.
**limit** (Optional): The number of results to fetch per page. Defaults to 50.
**page** (Optional): The page number to fetch. Defaults to first page.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<lfm status="ok" total="109" page="1" perPage="50" totalPages="3">
    <friends user="joanofarctan">
        <user>
            <name>eartle</name>
            <realname>Michael Coffey</realname>
            <image size="small">http://userserve-ak.last.fm/serve/34/45718509.jpg</image>
            <image size="medium">http://userserve-ak.last.fm/serve/64/45718509.jpg</image>
            <image size="large">http://userserve-ak.last.fm/serve/126/45718509.jpg</image>
            <image size="extralarge">http://userserve-ak.last.fm/serve/252/45718509.jpg</image>
            <url>http://www.last.fm/user/eartle</url>
            <id>7737850</id>
            <country>UK</country>
            <age>29</age>
            <gender>m</gender>
            <subscriber>1</subscriber>
            <playcount>45366</playcount>
            <playlists>4</playlists>
            <bootstrap>0</bootstrap>
            <registered unixtime="1189696970">2007-09-13 15:22</registered>
            <type>subscriber</type>
        </user>
        ...
    </friends>
</lfm>
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
