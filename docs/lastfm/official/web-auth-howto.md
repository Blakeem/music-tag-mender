Source: https://www.last.fm/api/webauth
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# Authentication: Web Application How-To

This authentication how-to is for web applications only. Desktop application developers should see the [desktop application how-to](https://www.last.fm/api/desktopauth).

## 1. Get an API Key

If you don’t already have an API account, please [apply for one](https://www.last.fm/api/account/create). For each of your [accounts](https://www.last.fm/api/accounts) you will have a **shared secret** which you will require in [Section 6](#_6-sign-your-calls). You will also need to set up a **callback url** which our authentication service will redirect to in [Section 4](#_4-fetch-a-web-service-session).

## 2. Request authorization from the user

Send your user to [last.fm/api/auth](https://www.last.fm/api/auth) with your API key as a parameter. Use an HTTP GET request. Your request will look like this:

```xml
http://www.last.fm/api/auth/?api_key=xxx
```

If the user is not logged in to Last.fm, they will be redirected to the login page before being asked to grant your web application permission to use their account. On this page they will see the name of your application, along with the application description and logo as supplied in Section 1.

### 2.1 Custom callback url

You can optionally specify a callback URL that is different to your API Account callback url. Include this as a query param `cb`. This allows you to have users forward to a specific part of your site after the authorisation process.

```xml
http://www.last.fm/api/auth/?api_key=xxx&cb=http://example.com
```

## 3. Create an authentication handler

Once the user has granted permission to use their account on the Last.fm page, Last.fm will redirect to your **callback url**, supplying an authentication token as a GET variable.

```xml
<callback_url>/?token=xxxxxxx
```

If the **callback url** already contains a query string then token variable will be appended, like;

```xml
<callback_url>&token=xxxxxxx
```

The script located at your callback url should pick up this authentication token and use it to create a Last.fm web service session as described in Section 4.

### 3.1 Authentication Tokens

Authentication tokens are user and API account specific. They are valid for 60 minutes from the moment they are granted.

## 4. Fetch a Web Service Session

Send your api key along with an api signature and your authentication token as arguments to the [auth.getSession](https://www.last.fm/api/show/auth.getSession) API method call. The parameters for this call are defined as such:

**api_key**: Your 32-character API Key.
**token**: The authentication token received at your callback url as a GET variable.
**api_sig**: Your 32-character API method signature, as explained in [Section 6](#_6-sign-your-calls)

Note: You can only use an authentication token once. It will be consumed when creating your web service session.

The response format of this call is shown on the [auth.getSession](https://www.last.fm/api/show/auth.getSession) method page.

### 4.1 Session Lifetime

Session keys have an infinite lifetime by default. You are recommended to store the key securely. Users are able to revoke privileges for your application on their Last.fm settings screen, rendering session keys invalid.

## 5. Make authenticated web service calls

You can now sign your web service calls with a method signature, provided along with the session key you received in [Section 4](#_4-fetch-a-web-service-session) and your API key. You will need to include all three as parameters in subsequent calls in order to be able to access services that require authentication. You can visit individual method call pages to find out if they require authentication. Your three authentication parameters are defined as:

**sk** (Required): The session key returned by [auth.getSession](https://www.last.fm/api/show/auth.getSession) service.
**api_key** (Required): Your 32-character API key.
**api_sig** (Required): Your API method signature, constructed as explained in [Section 6](#6)

## 6. Sign your calls

Construct your api method signatures by first ordering all the parameters sent in your call alphabetically by parameter name and concatenating them into one string using a `<name><value>` scheme. So for a call to `auth.getSession` you may have:

```xml
**api_key**xxxxxxxx**method**auth.getSession**token**xxxxxxx
```

Ensure your parameters are [utf8](http://www.utf-8.com) encoded. Now append your **secret** to this string. Finally, generate an [md5](http://en.wikipedia.org/wiki/MD5) hash of the resulting string. For example, for an account with a secret equal to 'mysecret', your api signature will be:

```xml
api signature = md5("api_keyxxxxxxxxmethodauth.getSessiontokenxxxxxxxmysecret")
```

Where `md5()` is an md5 hashing operation and its argument is the string to be hashed. The hashing operation should return a 32-character hexadecimal md5 hash.
