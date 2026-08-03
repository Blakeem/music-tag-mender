Source: https://www.last.fm/api/intro
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# Introduction

The Last.fm API allows you to call *methods* that respond in [REST](http://en.wikipedia.org/wiki/Representational_State_Transfer) style xml. Individual methods are detailed in the menu on the left.

> **API ROOT**
>
> The API root URL is located at [http://ws.audioscrobbler.com/2.0/](http://ws.audioscrobbler.com/2.0)

Generally speaking, you will send a *method* parameter expressed as 'package.method' along with method specific arguments to the root URL. The API supports multiple transport formats but will respond in Last.fm idiom xml by default.

Note:

- Please use an identifiable User-Agent header on all requests. This helps our logging and reduces the risk of you getting banned.
- Be reasonable in your usage of the API and ensure you don't make an excessive number of calls as that can impact the reliability of the service to you and other users. We encourage best practice implementation, for example, if you're making a web application, try not to hit the API on page load. Your account may be suspended if your application is continuously making several calls per second  or if you’re making excessive calls. See our [API Terms of Service](https://www.last.fm/api/tos) for more information on limits.
- If you are planning to use our API for commercial purposes, please contact us via email at [partners@last.fm](mailto:partners@last.fm).
- We assume that you are using an [RFC 3986](http://www.ietf.org/rfc/rfc3986.txt)-compliant HTTP client to access the web services. In particular, pay attention to your url encoding. This will not be an issue for 99% of developers.

## Encoding

Use [UTF-8](http://www.utf-8.com) encoding when sending arguments to API methods.

## Request Styles

You can get more information on how to work with [REST requests](https://www.last.fm/api/rest) or [XML-RPC requests](https://www.last.fm/api/xmlrpc) when calling the Last.fm API.

## Authentication

The [authentication protocol](https://www.last.fm/api/authentication) allows you to perform actions on user accounts in a manner that is secure for Last.fm users. All write services require authentication.

## Scrobbling

We encourage services that use the Last.fm API to build-in scrobbling natively into their applications (where applicable, and particularly for media players), to allow users to send listening data in to their Last.fm user profiles. This can be done through our [Scrobbling API](https://www.last.fm/api/scrobbling).

## Discussion

Join the [Last.fm Support Forums](https://support.last.fm/c/support/api) for information about new Web Services, access to beta API's, provide feedback and discuss development with other developers.

## Terms of Service

For our API Terms of Service please see [here](https://www.last.fm/api/tos)
