Source: https://musicbrainz.org/doc/Development/OAuth2
Transcluded from wiki.musicbrainz.org/Development/OAuth2, revision #78911
Version: ws/2 (current production version)
Retrieved: 2026-07-07

Note: this is the OAuth2 flow, an alternative to HTTP digest authentication (see musicbrainz-api.md
"Authentication" section — digest auth uses the same username/password as the main website, over
HTTPS). OAuth2 is required for third-party apps acting on behalf of a MusicBrainz user without
handling that user's password directly; the "Scopes" section below is the authoritative list of
what each permission unlocks (tag, rating, collection, submit_isrc, submit_barcode, profile, email).

---

OAuth2 is a protocol that lets external applications request authentication of the user and authorization to perform actions using the [web service](https://musicbrainz.org/doc/Development/XML_Web_Service/Version_2) on their behalf without getting their password.

## Contents

-   [1 Basic steps](#Basic_steps)
    -   [1.1 Registering an application](#Registering_an_application)
    -   [1.2 Obtaining access token](#Obtaining_access_token)
    -   [1.3 Using access token](#Using_access_token)
    -   [1.4 Refreshing access token](#Refreshing_access_token)
-   [2 Authorization](#Authorization)
    -   [2.1 Authorization request](#Authorization_request)
    -   [2.2 Exchanging authorization code for an access token](#Exchanging_authorization_code_for_an_access_token)
    -   [2.3 Refreshing an access token](#Refreshing_an_access_token)
    -   [2.4 Revoking a token](#Revoking_a_token)
-   [3 Scopes](#Scopes)
-   [4 Access token usage](#Access_token_usage)
    -   [4.1 Bearer tokens](#Bearer_tokens)

## Basic steps

### Registering an application

All developers need to [register their application](https://musicbrainz.org/account/applications) before getting started. A registered OAuth application is assigned a unique Client ID and Client Secret. The OAuth protocol recognizes two types of applications:

1.  Confidential - Applications that can keep secrets. Typically web applications, running on a server.
2.  Public - Applications that have their code exposed to the public and therefore cannot keep secrets. There are typically installed desktop applications or web applications running in the browser.

The Client Secret assigned to Confidential applications should not be shared. This does not apply to Public applications, in which case the assigned Client Secret is not treated as a secret and can be embedded in the application code.

### Obtaining access token

The general work-flow for obtaining an access token is the following:

1.  Redirect the user to the OAuth authorization page with the appropriate parameters. From a desktop application, you need to open a browser with the URL for the user.
2.  After the user authorizes the request, you will receive an authorization code. The authorization code is either delivered to the configured redirect URL or the user copies it to the application manually if using the out-of-band (OOB) method.
3.  Send a request exchange the authorization code for an access token and optionally a refresh token.

Details of the steps are explained below.

### Using access token

After an application has obtained an access token, it may use the token in the web service to get user details or submit data to MusicBrainz on the user's behalf. MusicBrainz currently only supports Bearer tokens, which are very easy to use but can only be sent over HTTPS.

### Refreshing access token

Access tokens have a limited life-time. During the authorization process, application receives a refresh token, in addition to the first access token. This refresh token allows applications to obtain new access tokens.

## Authorization

### Authorization request

The authorization sequence starts by redirecting the user to the authorization endpoint with a set of query string parameters describing the authorization request. The endpoint is located at `https://musicbrainz.org/oauth2/authorize` and is only accessible over HTTPS. HTTP connections are refused.

The following set of query string parameters is supported by the MusicBrainz authentication endpoint:

<dl><dt><code>response_type</code></dt><dd>Must be always set to <code>code</code>.</dd><dt><code>client_id</code></dt><dd>Client ID assigned to your application. You can find it on the website in your list of registered applications.</dd><dt><code>redirect_uri</code></dt><dd>URL where clients should be redirected after authorization. This must match exactly the URL you entered when registering your application. Desktop applications can use either <code>urn:ietf:wg:oauth:2.0:oob</code> or <code>http://localhost</code> with a custom port.</dd><dt><code>scope</code></dt><dd>Space delimited list of scopes the application requests. See below for a list of all available scopes.</dd><dt><code>state</code> (optional)</dt><dd>Any string the application wants passed back after authorization. For example, this can be a CSRF token from your application. This parameter is optional, but strongly recommended.</dd><dt><code>code_challenge</code> (optional)</dt><dd>MusicBrainz supports the use of "Proof Key for Code Exchange" (PKCE) by clients. This is strongly recommended to avoid authorization code interception attacks. See <a class="external text" href="https://tools.ietf.org/html/rfc7636#section-4.1" rel="nofollow">RFC 7636</a> for the process of generating a <code>code_verifier</code> and then a <code>code_challenge</code> (passed here) based on that.</dd><dt><code>code_challenge_method</code> (optional)</dt><dd>Either <code>S256</code> (recommended) or <code>plain</code> (the default if not specified).</dd></dl>

There are two extra parameters applicable only to web server applications:

<dl><dt><code>access_type</code> (optional)</dt><dd>Indicates if your application needs to access the API when the user is not present at the browser. This parameter defaults to <code>online</code>. If your application needs to refresh access tokens when the user is not present at the browser, then use <code>offline</code>. This will result in your application obtaining a refresh token the first time your application exchanges an authorization code for a user.</dd><dt><code>approval_prompt</code> (optional)</dt><dd>Indicates if the user should be re-prompted for consent. The default is <code>auto</code>, so a given user should only see the consent page for a given set of scopes the first time through the sequence. If the value is <code>force</code>, then the user sees a consent page even if they have previously given consent to your application for a given set of scopes.</dd></dl>

For example, a complete authorization request from a web application requesting permissions to read the user's private tags and ratings would look like this:

```
https://musicbrainz.org/oauth2/authorize?
  response_type=code&
  client_id=k1Mm4xTmAh5zhXtiPEQekViNbgMT8_RG&
  redirect_uri=http%3A%2F%2Fwww.example.com.com%2Fauth2callback&
  scope=tag%20rating&
  state=1351449443
```

The response to the authorization request will be sent to the URL indicated in the `redirect_uri` parameter. The authorization endpoint will redirect the user to this URL with a set of specific query string parameters indicating the result. If the user does not approve the request or there is a problem with the request, it will return an error describing the problem. Otherwise it will return an authorization code that can be exchanged for an access token by the token endpoint.

In case of an error, the response will look like this:

```
http://www.example.com/oauth2callback?state=1351449443&error=access_denied
```

Possible error codes are `unsupported_response_type`, `invalid_scope` and `access_denied`. If there is a problem with the `client_id` or `redirect_uri` parameters, the authorization endpoint will _not_ redirect back to your application and only inform the user about the problem.

A successful authorization response:

```
http://www.example.com/oauth2callback?state=1351449443&code=4-H4vg4V2kEEhHPM7kWpN18d9trJenOp
```

### Exchanging authorization code for an access token

Once your application receives an authorization code, it can send a POST request the token endpoint located at `https://musicbrainz.org/oauth2/token`, to exchange the code for an access token. As before, this endpoint is only avilable over HTTPS and HTTP requests will be refused.

The requires parameters for exchanging the authorization code are:

<dl><dt><code>grant_type</code></dt><dd>Must be set to <code>authorization_code</code>.</dd><dt><code>code</code></dt><dd>Authorization code from the initial request.</dd><dt><code>client_id</code></dt><dd>Client ID assigned to your application.</dd><dt><code>client_secret</code></dt><dd>Client secret assigned to your application.</dd><dt><code>redirect_uri</code></dt><dd>Redirect URL registered with the application.</dd><dt><code>token_type</code> (optional)</dt><dd>Only <code>bearer</code> is allowed to be passed here.</dd><dt><code>code_verifier</code> (optional)</dt><dd>If you're using PKCE, pass the <code>code_verifier</code> here. We'll reject the access token request if it doesn't agree with the <code>code_challenge</code> and <code>code_challenge_method</code> sent with the initial request to <code>https://musicbrainz.org/oauth2/authorize</code>. The process is described in detail by <a class="external text" href="https://tools.ietf.org/html/rfc7636#section-4.5" rel="nofollow">RFC 7636</a>.</dd></dl>

An example of an authorization code exchange would look like this:

```
POST /oauth2/token HTTP/1.1
Host: musicbrainz.org
Content-Type: application/x-www-form-urlencoded

grant_type=authorization_code&
code=4-H4vg4V2kEEhHPM7kWpN18d9trJenOp&
client_id=k1Mm4xTmAh5zhXtiPEQekViNbgMT8_RG&
client_secret=2dj1b7PvccAkDLxIebEIFTGGO_eETc7K&
redirect_uri=http%3A%2F%2Fwww.example.com.com%2Fauth2callback
```

In case the authorization code and the client credentials were all valid, the server would respond with a JSON document in this format:

```
{
  "access_token":"UF7GvG2pl70jTogIwOhD32BhI_aIevPF",
  "expires_in":3600,
  "token_type":"Bearer",
  "refresh_token":"GjSCBBjp4fnbE0AKo3uFu9qq9K2fFm4u"
}
```

### Refreshing an access token

If you have an installed application, or web application with offline access, you have received a refresh token during the authorization process. This token can be used to get a new access token without any user interaction. Access tokens have a limited life-time, but the refresh token stays valid until the user manually revokes it.

The required parameters for refreshing an access token are:

<dl><dt><code>grant_type</code></dt><dd>Must be set to <code>refresh_token</code>.</dd><dt><code>refresh_token</code></dt><dd>Refresh token received during the authorization process.</dd><dt><code>client_id</code></dt><dd>Client ID assigned to your application.</dd><dt><code>client_secret</code></dt><dd>Client secret assigned to your application.</dd><dt><code>token_type</code> (optional)</dt><dd>Only <code>bearer</code> is allowed to be passed here.</dd></dl>

An example of an authorization code exchange would look like this:

```
POST /oauth2/token HTTP/1.1
Host: musicbrainz.org
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token&
refresh_token=GjSCBBjp4fnbE0AKo3uFu9qq9K2fFm4u&
client_id=k1Mm4xTmAh5zhXtiPEQekViNbgMT8_RG&
client_secret=2dj1b7PvccAkDLxIebEIFTGGO_eETc7K
```

In case the refresh token and the client credentials were all valid, the server would respond with a JSON document in this format:

```
{
  "access_token":"GjtKfJS6G4lupbQcCOiTKo4HcLXUgI1p",
  "expires_in":3600,
  "token_type":"Bearer",
  "refresh_token":"GjSCBBjp4fnbE0AKo3uFu9qq9K2fFm4u"
}
```

### Revoking a token

If a refresh or access token is no longer needed, e.g. when the end user logs out, changes identity, or uninstalls your application, you should notify us to remove the unused token. MusicBrainz implements a token revocation endpoint ([RFC 7009](https://tools.ietf.org/html/rfc7009)) at `https://musicbrainz.org/oauth2/revoke`. The only supported method is `POST`. Once again, it's only available over HTTPS.

The required parameters for revoking a token are:

<dl><dt><code>token</code></dt><dd>Either a refresh token or access token.</dd><dt><code>client_id</code></dt><dd>Client ID assigned to your application.</dd><dt><code>client_secret</code></dt><dd>Client secret assigned to your application.</dd></dl>

If your application is installed or offline and `token` is a refresh token, we'll revoke the entire authorization grant associated with that token. If it's an access token, we'll only revoke the access token given; any associated refresh token can still be used to request a new access token.

If your application is web-based or online, revoking the access token will revoke the entire authorization grant, as there's no refresh token in this case.

An example of a token revocation would look like this:

```
POST /oauth2/revoke HTTP/1.1
Host: musicbrainz.org
Content-Type: application/x-www-form-urlencoded

token=GjSCBBjp4fnbE0AKo3uFu9qq9K2fFm4u&
client_id=k1Mm4xTmAh5zhXtiPEQekViNbgMT8_RG&
client_secret=2dj1b7PvccAkDLxIebEIFTGGO_eETc7K
```

If all parameters are present and the client credentials are okay, then a 200 OK response with no body is returned. The response status will indicate success even if the token was invalid (as the client wouldn't be able to handle such an error in a reasonable way).

## Scopes

Authorization requests have a limited scope. You should request only the scopes that your application necessarily needs. The following scopes are available in the MusicBrainz OAuth implementation:

<dl><dt><code>profile</code></dt><dd>View the user's public profile information (username, age, country, homepage).</dd><dt><code>email</code></dt><dd>View the user's email.</dd><dt><code>tag</code></dt><dd>View and modify the user's private tags.</dd><dt><code>rating</code></dt><dd>View and modify the user's private ratings.</dd><dt><code>collection</code></dt><dd>View and modify the user's private collections.</dd><dt><code>submit_isrc</code></dt><dd>Submit new ISRCs to the database.</dd><dt><code>submit_barcode</code></dt><dd>Submit barcodes to the database.</dd></dl>

## Access token usage

MusicBrainz supports one type of access token: Bearer tokens.

### Bearer tokens

Bearer tokens are very easy to use and consist only of one component, which you should treat as a password. For this reason, it is only possible to use them over HTTPS. If you try to send them over plain HTTP, they will be ignored.

The preferred method to use bearer tokens is via the `Authorization` header. An authenticated request would look like the following:

```
 GET /oauth2/userinfo HTTP/1.1
 Host: musicbrainz.org
 Authorization: Bearer dFngty-XelYCQpveDtCXT_1NWxtH5OPA
```

You can try it with the curl command line application:

```
 curl -H "Authorization: Bearer dFngty-XelYCQpveDtCXT_1NWxtH5OPA" https://musicbrainz.org/oauth2/userinfo
```

If you can't use the `Authorization` header, there is an alternative in which you perform a `POST` request and pass the `access_token` in the body that way:

```
 POST /oauth2/userinfo HTTP/1.1
 Host: musicbrainz.org
 Content-Type: application/x-www-form-urlencoded
 
 access_token=dFngty-XelYCQpveDtCXT_1NWxtH5OPA
```

An example of that, using curl again:

```
 curl -X POST -d "access_token=dFngty-XelYCQpveDtCXT_1NWxtH5OPA" https://musicbrainz.org/oauth2/userinfo
```

There is one last option suitable for testing only, and that is passing the `access_token` in the URI as a query parameter. You should avoid this unless the previous two methods are impossible, because it increases the likelihood of the token being logged by web servers and history data.

```
 GET https://musicbrainz.org/oauth2/userinfo?access_token=dFngty-XelYCQpveDtCXT_1NWxtH5OPA
```

With curl, that's just:

```
 curl "https://musicbrainz.org/oauth2/userinfo?access_token=dFngty-XelYCQpveDtCXT_1NWxtH5OPA"
```

Bearer tokens are specified in [RFC 6750](http://tools.ietf.org/html/rfc6750).
