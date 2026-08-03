Source: https://www.last.fm/api/authspec
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# Authentication API

This is Version 1.0 of the Last.fm authentication API specification.

## 1. Authors

[Anil Bawa-Cavia](http://www.last.fm/user/joanofarctan)

## 2. Requirements

You must have applied for, and received, a Last.fm API account, via the account application screen. You must configure your [Last.fm API account](https://www.last.fm/api/accounts) with:

- Your application name and description.
- Your application logo.

Your account page contains your *secret* which must be used when making authenticated calls – see Section 8 below.

### 2.1 Web-based Authentication

You must also configure a callback URL which will be used in Section 3.2 below.

## 3. Authentication For Web Applications

### 3.1 Request authorization from the user

Web applications should send a user to [last.fm/api/auth](https://www.last.fm/api/auth), sending an API key as a parameter, in order to authenticate the user. This should be an HTTP GET request. Your request will look like this:

```xml
http://www.last.fm/api/auth/?api_key=xxxxxxxxxx
```

If the user is not logged in to Last.fm, they will be redirected to the login page before being asked to grant your web application permission to use their account. On this page they will see the name of your application, along with the application description and logo as supplied in Section 2.

### 3.2 Create an authentication handler

Once the user has granted permission to use their account on the Last.fm page, Last.fm will redirect to your **callback url**, supplying an authentication token as a GET variable.

```xml
<callback_url>/?token=yyyyyy
```

The script located at your callback url should pick up this authentication token and use it to create a Last.fm web service session as described in Section 3.3.

### 3.3 Create a Web Service Session

Send your api key along with an api signature and your authentication token as arguments to the [auth.getSession](https://www.last.fm/api/show/auth.getSession) API method call. The parameters for this call are defined as such:

**api_key**: Your 32-character API Key.
**token**: The authentication token received at your callback url as a GET variable.
**api_sig**: Your 32-character API method signature, as explained in [Section 8](#_8-signing-calls).

The call will respond with a *session key* that can be used in authenticated calls.

## 4. Authentication For Desktop Applications

### 4.1 Fetch a request token

Make an API method call to the [auth.getToken](https://www.last.fm/api/show/auth.getToken) service. You should send the following arguments to that call:

**api_key**: Your 32-character API Key.
**api_sig**: A 32-character API method signature, constructed as explained in [Section 8](#_8-signing-calls).

This will return a token. To see the response format check the method documentation page. The token is not authorized by the user at this stage and cannot be used to create a session until it has been authorized.

### 4.2 Request authorization from the user

Your application needs to open an instance of a web browser and send the user to [last.fm/api/auth](https://www.last.fm/api/auth) with your API key and authentication token as parameters. Use an HTTP GET request. Your request will look like this:

```xml
http://www.last.fm/api/auth/?api_key=xxxxxxxxxx&token=yyyyyy
```

If the user is not logged in to Last.fm, they will be redirected to the login page before being asked to grant your application permission to use their account. On this page they will see the name of your application, along with the application description and logo as supplied in Section 2. Once the user has granted your application permission to use their account, the browser-based process is over and the user is asked to close their browser and return to your application.

### 4.3 Create a Web Service Session

Send your api key along with an api signature and your authentication token as arguments to the [auth.getSession](https://www.last.fm/api/show/auth.getSession) API method call. The parameters for this call are defined as such:

**api_key**: Your 32-character API Key.
**token**: The authentication token received at your callback url as a GET variable.
**api_sig**: Your 32-character API method signature, as explained in [Section 8](#_8-signing-calls).

The call will respond with a *session key* that can be used in authenticated calls.

## 5. Authentication For Mobile Applications

Send a request to [auth.getMobileSession](https://www.last.fm/api/show/auth.getMobileSession), sending the user's credentials to the call. The parameters for this call are defined as:

**password** (Required): The user's password in plaintext.
**username** (Required): The user's Last.fm username.
**api_key** (Required): A Last.fm API key.
**api_sig** (Required): A Last.fm method signature. See [Section 8](#8) for more information.

This call **must** be a POST made over HTTPS.

[auth.getMobileSession](https://www.last.fm/api/show/auth.getMobileSession) will return a *session key* in response to be used in authenticated calls.

## 6. Tokens & Sessions

### 6.1 Authentication Tokens

Authentication tokens are API account specific. They are valid for 60 minutes from the moment they are granted and can only used once (they are consumed when a session is created).

### 6.2 Session Lifetime

Session keys have an infinite lifetime by default. You are recommended to store the key securely. Users are able to revoke privileges for your application on their Last.fm settings screen, rendering session keys invalid.

## 7. Making authenticated calls

You should sign authenticated web service calls with a method signature, provided along with the session key you received from [auth.getSession](https://www.last.fm/api/show/auth.getSession) and your API key. You will need to include all three as parameters in authenticated calls. You can visit individual method call pages to find out if they require authentication. Your three authentication parameters are defined as:

**sk** (Required): The session key returned by [auth.getSession](https://www.last.fm/api/show/auth.getSession) service.
**api_key** (Required): Your 32-character API key.
**api_sig** (Required): Your API method signature, constructed as explained in [Section 8](#_8-signing-calls).

## 8. Signing Calls

Sign your authenticated calls by first ordering the parameters sent in your call alphabetically by parameter name and concatenating them into one string using a `<name><value>` scheme. You must not include the **format** and **callback** parameters. So for a call to `auth.getSession` you may have:

```xml
api_keyxxxxxxxxxxmethodauth.getSessiontokenyyyyyy
```

Ensure your parameters are [utf8](http://www.utf-8.com) encoded. Now append your **secret** to this string. Finally, generate an [md5](http://en.wikipedia.org/wiki/MD5) hash of the resulting string. For example, for an account with a secret equal to 'ilovecher', your api signature will be:

```xml
api signature = md5("api_keyxxxxxxxxxxmethodauth.getSessiontokenyyyyyyilovecher")
```

Where `md5()` is an md5 hashing operation and its argument is the string to be hashed. The hashing operation should return a 32-character hexadecimal md5 hash.
