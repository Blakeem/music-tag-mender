Source: https://www.last.fm/api/scrobbling
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# Scrobbling 2.0 Documentation

## Overview

This is a guide on how to send scrobbles to Last.fm.

Scrobbling is a way to send information about the music a user is listening to. A client is anything that plays music, such as desktop music players, mobile apps, websites, etc.

For every track a user listens to the client should send a track.updateNowPlaying request and a track.scrobble request.

Scrobbling 2.0 is not backwards compatible with the old Submissions Protocol 1.2.1 (which is deprecated).

## Now Playing Requests

The "Now Playing" service lets a client notify Last.fm that a user has started listening to a track. This does not affect a user's charts, but will feature the current track on their profile page, along with an indication of what music player they're using.

This API method call is optional for scrobbling clients, but recommended. Requests should be sent as soon as a user starts listening to a track.

### Sending a Request

The web service method for sending Now Playing information is [track.updateNowPlaying](https://www.last.fm/api/show/track.updateNowPlaying).

As with all our write web services, requests must be sent as HTTP POST requests to `http://ws.audioscrobbler.com/2.0/` with form urlencoded parameters in the body of the request. The text encoding must be UTF-8.

Requests must be [authenticated](https://www.last.fm/api/authentication).

### How we handle requests

Once a request has been received by Last.fm the following sequence of events takes place on our side:

- Check that the request passes our [filters](#filtered-requests).
- Find the track in our catalogue.
- Update the user's Now Playing status with the track and client.
- Return a response to the client indicating the outcome of the request.

### Error handling

Last.fm signals the success or failure of a request by three different means:

- The HTTP status code.
- The lfm status attribute of the lfm XML element returned in the response body. This will be either "ok" or "failed".
- The lfm [error code](https://www.last.fm/api/errorcodes) (when the lfm status was "failed") further describes the cause of the error. This is the error element's code attribute in the XML returned in the response body.

We recommend that your client logs all failed requests and their responses (HTTP headers, and the xml body) to assist debugging.

Now Playing requests that fail should not be retried.

## Scrobble Requests

The scrobble service lets a client add a track-play to a user's profile. This data is used to show a user's listening history and generate personalised charts and recommendations (and more).

### When is a scrobble a scrobble?

A track should only be scrobbled when the following conditions have been met:

- The track must be longer than 30 seconds.
- *And* the track has been played for at least half its duration, or for 4 minutes (whichever occurs earlier.)

As soon as these conditions have been met, the scrobble request may be sent at any time. It is often most convenient to send a scrobble request when a track has finished playing.

Other considerations:

- Do not attempt to determine a track's meta data from its filename. Please only use meta data from well-structured sources such as ID3 tags.
- Do not use the corrections returned by the now playing service as input for the scrobble request, unless they have been explicitly approved by the user.

### When to set the "chosenByUser" parameter

This parameter is used to indicate when a scrobble comes from a source that the user doesn't have "direct" control over. In most cases where a user is scrobbling their own music you can safely ignore this parameter. However, if the user is listening to music that is effectively chosen by someone other than themselves (e.g. from a Last.fm radio stream; from some other recommendation service; or radio show put together by a DJ or host) then this value should be set to "false". If there is any ambiguity or doubt then don't send this value.

### Sending a Request

The web service method for sending scrobbles is [track.scrobble](https://www.last.fm/api/show/track.scrobble).

As with all our write web services, requests must be sent as HTTP POST requests to **http://ws.audioscrobbler.com/2.0/** with form urlencoded (using utf-8) parameters in the body of the request.

Requests must be [authenticated](https://www.last.fm/api/authentication).

Multiple scrobbles may be sent in a single batch request, this is recommended when there are cached scrobbles to be sent in the case of previous errors. A batch request may contain up to 50 scrobbles.

### How we handle requests

Once a request has been received by Last.fm the following sequence of events takes place on our side:

- Check that the request passes our [filters](#filtered-requests).
- Find the track in our catalogue.
- Store the scrobble in the user's profile.
- Return a response to the client indicating the outcome of the request.

### Error handling

Last.fm signals the success or failure of a request by three different means:

- The HTTP status code.
- The lfm status attribute of the lfm XML element returned in the response body. This will be either `ok` or `failed`.
- The lfm [error code](https://www.last.fm/api/errorcodes) (when the lfm status was `failed`) further describes the cause of the error. This is the error element's code attribute in the XML returned in the response body.

We recommend that your client logs all failed requests and their responses (HTTP headers, and the xml body) to assist debugging.

No matter what the HTTP status code is, ## you must inspect the content of the response

. If the HTTP status is not 200 OK it indicates there was an error (that should be logged), but it does not indicate how to handle it. Additionally a HTTP status of 200 OK does not mean the request was successful.

For example if the request was missing a required parameter you will receive an HTTP "400 Bad Request" status and retrying without modifying the request will always give the same response.

Next inspect the lfm status and lfm status code. If the lfm status is "ok" then the request succeeded.

For example if you send a request that is missing the artist parameter the response will be the following:

```xml
<?xml version="1.0" encoding="utf-8"?>
<lfm status="failed">
    <error code="6">Missing required parameter artist</error>
</lfm>

```

Lfm error codes that indicate a scrobble request should be retried are:

- **11** :  Service Offline - This service is temporarily offline, try again later.
- **16** : The service is temporarily unavailable, please try again.

Additionally this lfm error code indicates that the client should reauthenticate to get a new session key before retrying the request:

- **9** : Invalid session key - Please re-authenticate

All other error codes indicate the scrobble request was incorrectly formed in some way and should not be retried.

This diagrams describes the flow for sending scrobble requests and handling the response.

### Retrying cached scrobbles

Since the server connectivity may be variable (either because of network outage, or server failure), requests will occasionally fail. It is recommended that clients hold scrobbles that need be retried in a local cache. This cache should survive client restarts, allowing the user to close the client and restart later without losing unsubmitted scrobbles. Scrobbles should be sent in order, therefore cached scrobbles should be sent before new scrobbles. Scrobbles can be sent in batches of up to 50 scrobbles per request.

## Filtered Requests

A scrobble or Now Playing request may be ignored if we detect bad meta data. This is not treated as an error condition, so if filtering takes place the response will have an "ok" status. The server will return an ignored message with an associated ignored code. This information is useful if the client wants to show information about why a track was not added to the user's profile.

In the case of batch scrobble requests, each scrobble is filtered separately. So if only one scrobble has bad meta data and is ignored other scrobbles in the request will still be accepted.

Possible ignored message codes:

- **0** : None (the request passed all filters).
- **1** : Filtered artist.
- **2** : Filtered track.
- **3** : Timestamp too far in the past.
- **4** : Timestamp too far in the future.
- **5** : Max daily scrobbles exceeded.

We may add additional ignored codes in the future.

For example if you sent a scrobble request with **artist="Unknown** Artist" the response will look something like this:

```xml
<?xml version='1.0' encoding='utf-8'?>
<lfm status="ok">
    <scrobbles accepted="0" ignored="1">
        <scrobble>
            <track corrected="0">Test Track</track>
            <artist corrected="0">Unknown Artist</artist>
            <album corrected="0"></album>
            <albumartist corrected="0"></albumartist>
            <timestamp>1288728940</timestamp>
            <ignoredmessage code="1">
                Artist name failed filter: Unknown Artist
            </ignoredmessage>
        </scrobble>
    </scrobbles>
</lfm>

```

## Meta data corrections

The Last.fm catalogue contains correction information which we use to merge mispelled artists and tracks into their correct versions. If we find such a correction when resolving a track in our catalogue we will return it in the response.

This information could optionally be used by the client to suggest track meta data (ID3 tags, etc) corrections to the user. They should not be applied automatically. Most clients will simply ignore corrections.

Corrections are indicated by a **corrected="1"** attribute. Its value will contain the corrected version (so the value will differ from that in the request).

For example if you send a scrobble request with **artist="Bjork"** the response will look something like this:

```xml
<?xml version='1.0' encoding='utf-8'?>
<lfm status="ok">
    <scrobbles accepted="1" ignored="0">
        <scrobble>
            <track corrected="0">Wanderlust</track>
            <artist corrected="1">Björk</artist>
            <album corrected="0"></album>
            <albumartist corrected="0"></albumartist>
            <timestamp>1288728745</timestamp>
            <ignoredmessage code="0"></ignoredmessage>
        </scrobble>
    </scrobbles>
</lfm>

```

This response shows the artist was corrected to "Björk".

Considerations:

- Do not use the corrections returned by the now playing service as input for the scrobble request, unless they have been explicitly approved by the user.

## Help

If you experience any problems using the scrobbling API please report them on our [support forums](https://support.last.fm). Try to give as much information about the requests and responses as possible.
