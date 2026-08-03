Source: https://www.last.fm/api/show/auth.getMobileSession
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# auth.getMobileSession

Create a web service session for a user. Used for authenticating a user when the password can be inputted by the user. Accepts email address as well, so please use the username supplied in the output. Only suitable for standalone mobile devices. See the authentication how-to for more. You must use HTTPS and POST in order to use this method.

## Params

**password** (Required): The password in plain text.
**username** (Required): The last.fm username or email address.
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

> **DEPRECATED BEHAVIOUR**
>
> This method has other parameters which are now deprecated and should not be used.

A previous version of **auth.getMobileSession** accepted an **authToken** parameter in place of **password**, where authToken was defined as:

```xml
authToken = md5(username + md5(password))
```

We recommend all clients update to the latest version as support for **authToken** will be removed in the future.

## Errors

- **4**: You must use POST in order to use this method.
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
