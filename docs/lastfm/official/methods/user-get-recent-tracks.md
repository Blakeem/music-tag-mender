Source: https://www.last.fm/api/show/user.getRecentTracks
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# user.getRecentTracks

Get a list of the recent tracks listened to by this user. Also includes the currently playing track with the nowplaying="true" attribute if the user is currently listening.

## Example URLs

**JSON:** [/2.0/?method=user.getrecenttracks&user=rj&api_key=YOUR_API_KEY&format=json](http://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user=rj&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=user.getrecenttracks&user=rj&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=user.getrecenttracks&user=rj&api_key=YOUR_API_KEY)

## Params

**limit** (Optional): The number of results to fetch per page. Defaults to 50. Maximum is 200.
**user** (Required): The last.fm username to fetch the recent tracks of.
**page** (Optional): The page number to fetch. Defaults to first page.
**from** (Optional): Beginning timestamp of a range - only display scrobbles after this time, in UNIX timestamp format (integer number of seconds since 00:00:00, January 1st 1970 UTC). This must be in the UTC time zone.
**extended** (0|1) (Optional): Includes extended data in each artist, and whether or not the user has loved each track
**to** (Optional): End timestamp of a range - only display scrobbles before this time, in UNIX timestamp format (integer number of seconds since 00:00:00, January 1st 1970 UTC). This must be in the UTC time zone.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<recenttracks user="RJ" page="1" perPage="10" totalPages="3019">
  <track nowplaying="true">
    <artist mbid="2f9ecbed-27be-40e6-abca-6de49d50299e">Aretha Franklin</artist>
    <name>Sisters Are Doing It For Themselves</name>
    <mbid/>
    <album mbid=""/>
    <url>www.last.fm/music/Aretha+Franklin/_/Sisters+Are+Doing+It+For+Themselves</url>
    <date uts="1213031819">9 Jun 2008, 17:16</date>
    <streamable>1</streamable>
  </track>
  ...
</recenttracks>
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
