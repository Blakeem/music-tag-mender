Source: https://www.last.fm/api/show/user.getWeeklyAlbumChart
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# user.getWeeklyAlbumChart

Get an album chart for a user profile, for a given date range. If no date range is supplied, it will return the most recent album chart for this user.

## Example URLs

**JSON:** [/2.0/?method=user.getweeklyalbumchart&user=rj&api_key=YOUR_API_KEY&format=json](http://ws.audioscrobbler.com/2.0/?method=user.getweeklyalbumchart&user=rj&api_key=YOUR_API_KEY&format=json)
**XML:** [/2.0/?method=user.getweeklyalbumchart&user=rj&api_key=YOUR_API_KEY](http://ws.audioscrobbler.com/2.0/?method=user.getweeklyalbumchart&user=rj&api_key=YOUR_API_KEY)

## Params

**user** (Required): The last.fm username to fetch the charts of.
**from** (Optional): The date at which the chart should start from. See User.getChartsList for more.
**to** (Optional): The date at which the chart should end on. See User.getChartsList for more.
**api_key** (Required): A Last.fm API key.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<weeklyalbumchart user="RJ" from="1212321600" to="1212926400">
  <album rank="1">
    <artist mbid="80e577ba-841f-43ba-9f32-72e7c1692336">David Hudson</artist>
    <name>Bedarra</name>
    <mbid>dc30face-71db-413a-bcae-06accbd64aae</mbid>
    <playcount>10</playcount>
    <url>http://www.last.fm/music/David+Hudson+and+Friends/Bedarra</url>
  </album>
  ...
</weeklyalbumchart>
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
