Source: https://www.last.fm/api/authentication
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# User Authentication

The authentication API provides third-parties with a secure means of creating Last.fm user sessions over the Last.fm API, for deeper integration with our platform. All *write* services require authentication.

## 1. Get an API Key

You will need to [apply for a key](https://www.last.fm/api/account/create) before authenticating with the API.

## 2. Configure Your API Account

Head over to your [api accounts page](https://www.last.fm/api/accounts), and select the account you wish to configure. You need to supply an application name, a description and an optional logo. Each of your account pages contains an *API key* and *secret*; you will need both to use the API.

## 3. Choose your authentication path

- If you're building a web application, see the [web application how-to](https://www.last.fm/api/webauth) for more details.
- If you're building a desktop application, see the [desktop application how-to](https://www.last.fm/api/desktopauth) for more details.
- If you're building on a standalone device such as a mobile phone, see the [mobile how-to](https://www.last.fm/api/mobileauth) for more details.

In some cases, you may want to choose a different authentication path from the obvious (e.g. a mobile app could well use the desktop path if there's a web browser on the device). If in doubt, check them all out.

## 4. Authentication Spec

See the full [authentication API specification](https://www.last.fm/api/authspec) for an overview of the API.
