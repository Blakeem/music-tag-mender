Source: https://www.last.fm/api/show/auth.getSession
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# auth.getSession

Fetch a session key for a user. The third step in the authentication process. See the authentication how-to for more information.

## Params

**token** (Required): A 32-character ASCII hexadecimal MD5 hash returned by step 1 of the authentication process (following the granting of permissions to the application by the user)
**api_key** (Required): A Last.fm API key.
**api_sig** (Required): A Last.fm method signature. See [authentication](https://www.last.fm/api/authentication) for more information.

## Auth

This service does **not** require authentication.

## Sample Response

```xml
<lfm status="ok">
  <session>
    <name>MyLastFMUsername</name>
    <key>d580d57f32848f5dcf574d1ce18d78b2</key>
     <subscriber>0</subscriber>
  </session>
</lfm>
```

## Errors

- **4**: Invalid authentication token supplied
- **14**: This token has not been authorized
- **15**: This token has expired
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
