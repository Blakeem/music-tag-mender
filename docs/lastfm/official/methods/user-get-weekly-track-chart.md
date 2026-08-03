Source: https://www.last.fm/api/show/user.getWeeklyTrackChart
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# user.getWeeklyTrackChart

Get a track chart for a user profile, for a given date range. If no date range is supplied, it will return the most recent track chart for this user.

## Example URLs

**JSON:** [/2.0/?method=user.getweeklytrackchart&user=rj&api_key=YOUR_API_KEY&format=json](http://ws.audioscrobbler.com/2.0/?method=user.getweeklytrackchart&user=rj&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=user.getweeklytrackchart&user=rj&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=user.getweeklytrackchart&user=rj&api_key=YOUR_API_KEY)

## Params

**user** (Required): The last.fm username to fetch the charts of.
**from** (Optional): The date at which the chart should start from. See User.getWeeklyChartList for more.
**to** (Optional): The date at which the chart should end on. See User.getWeeklyChartList for more.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<weeklytrackchart user="joanofarctan" from="1212321600" to="1212926400">
  <track rank="1">
    <artist mbid="17b0d7f1-fad3-404e-87ae-874e6e158c3a">Dirk Leyers</artist>
    <name>Wellen</name>
    <mbid/>
    <playcount>3</playcount>
    <url>http://www.last.fm/music/Dirk+Leyers/_/Wellen</url>
  </track>
  ...
</weeklytrackchart>
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
