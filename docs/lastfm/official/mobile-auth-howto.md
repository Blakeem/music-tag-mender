Source: https://www.last.fm/api/mobileauth
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# Authentication: Mobile Application How-To

This authentication how-to is for standalone mobile devices only.

## 1. Get an API Key

You can apply for an API key [here](https://www.last.fm/api/account/create). When you have been granted an API Key you can configure your accounts by visiting [last.fm/api/accounts](https://www.last.fm/api/api/accounts) . Here you will see a **shared secret** which will be required in [Section 4](#_4-sign-your-calls).

## 2. Request authorization from the user

Send a request to [auth.getMobileSession](https://www.last.fm/api/show/auth.getMobileSession), sending the user's credentials to the call. The parameters for this call are defined as:

**username** (Required): The last.fm username.
**password** (Required): A plaintext password.
**api_key** (Required): A Last.fm API key.
**api_sig** (Required): A Last.fm method signature. See [Section 4](#_4-sign-your-calls) for more information.

This webservice has to be called via POST and HTTPS. It will fail if you try to use it via GET or HTTP.

[auth.getMobileSession](https://www.last.fm/api/show/auth.getMobileSession) will return a `session key` in response to be used on subsequent calls.

### 2.1 Session Lifetime

Session keys have an infinite lifetime by default. You are recommended to store the key securely. Users are able to revoke privileges for your application on their Last.fm settings screen, rendering session keys invalid.

## 3. Make authenticated web service calls

You can now sign your web service calls with a method signature, provided along with the session key you received in Section 2 and your API key. You will need to include all three as parameters in subsequent calls in order to be able to access services that require authentication. You can visit individual method call pages to find out if they require authentication. Your three authentication parameters are defined as:

**sk** (Required): The session key returned by [auth.getMobileSession](https://www.last.fm/api/show/auth.getMobileSession) service.
**api_key** (Required): Your 32-character API key.
**api_sig** (Required): Your API method signature, constructed as explained in [Section 4](#_4-sign-your-calls)

## 4. Sign your calls

Construct your api method signatures by first ordering all the parameters sent in your call alphabetically by parameter name and concatenating them into one string using a `<name><value>` scheme. So for a call to `auth.getMobileSession` you may have:

```xml
**api_key**xxxxxxxx**method**auth.getMobileSession**password**xxxxxxx**username**xxxxxxxx
```

Ensure your parameters are [utf8](http://www.utf-8.com) encoded. Now append your **secret** to this string. Finally, generate an [md5](http://en.wikipedia.org/wiki/MD5) hash of the resulting string. For example, for an account with a secret equal to 'mysecret', your api signature will be:

```xml
api signature = md5("api_keyxxxxxxxxmethodauth.getMobileSession
                         passwordxxxxxxxusernamexxxxxxxxmysecret")
```

Where *md5()* is an md5 hashing operation and its argument is the string to be hashed. The hashing operation should return a 32-character hexadecimal md5 hash.
