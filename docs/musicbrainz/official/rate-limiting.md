Source: https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting
Transcluded from wiki.musicbrainz.org/MusicBrainz_API/Rate_Limiting, revision #78895
Version: ws/2 (current production version)
Retrieved: 2026-07-07

---

## Contents

-   [1 Introduction](#Introduction)
-   [2 How throttling works](#How_throttling_works)
    -   [2.1 User-Agent](#User-Agent)
    -   [2.2 Source IP address](#Source_IP_address)
    -   [2.3 Global](#Global)
-   [3 More about User-Agent throttling](#More_about_User-Agent_throttling)
-   [4 How can I be a good citizen and be smart about using the Web Service?](#How_can_I_be_a_good_citizen_and_be_smart_about_using_the_Web_Service?)
    -   [4.1 Provide meaningful User-Agent strings](#Provide_meaningful_User-Agent_strings)
    -   [4.2 Scheduling](#Scheduling)
    -   [4.3 Checking for changes](#Checking_for_changes)

## Introduction

MusicBrainz has finite resources and wishes to make the MusicBrainz database available to as much of the Internet community as possible. However, at certain times of day the number of requests to the [MusicBrainz API](https://musicbrainz.org/doc/MusicBrainz_API) exceeds our capacity for handling these requests. Honoring all of these requests would overload our servers and degrade the service for everyone. For this reason we rate limit our Web Service, which limits the number of requests that clients can make in a given period of time.

## How throttling works

We may change the blocking/throttling rules at any time in order to protect the overall site health.

As of 2012-01-08 our rules are as follows:

When a request reaches our servers we check three conditions, in the following order:

1.  User-Agent string: are we receiving too many requests from this application?
2.  Source IP address: are we receiving too many requests from this particular IP address?
3.  Global: are the MusicBrainz servers as a whole too busy to handle this request?

If the answer to any one of those questions is "yes", then the request is denied with a 503 Service Unavailable error, and processing stops. Otherwise, we continue to the next check. If all checks pass then the request is honoured.

Read on for details of how each check works.

### User-Agent

For user-agents associated with [headphones](https://github.com/rembo10/headphones/wiki): we allow through (on average) 50 requests per second, and decline (http 503) the rest. This includes headphones itself, across several versions, as well as beets, the tagger it uses, when we can determine it's been called by headphones.

For "python-musicbrainz/0.7.3": we allow through (on average) 50 requests per second, and decline the rest (though recently this has not been hit).

For "anonymous" user-agents (see below): we allow through (on average) 50 requests per second, and decline (http 503) the rest.

For other user-agents: allow through.

### Source IP address

Unless you have agreed otherwise with MusicBrainz, the rule is as follows:

The rate at which your IP address is making requests is measured. If that rate is too high, **all** your requests will be declined (http 503) until the rate drops again. Currently that rate is (on average) 1 request per second.

For example: if your requests are coming in at 4 requests per second, we don't honour 25% of them and decline the other 75% - we decline 100% of them, until the rate drops to 1 per second or lower.

### Global

We allow through 300 requests each second (on average), and decline (http 503) the rest.

## More about User-Agent throttling

Applications which misbehave can end up degrading the quality of the MusicBrainz service; such applications (based on their User-Agent header) may be subject to throttling specific to that application.

If we see an application that's misbehaving, we like to try to get in touch with its maintainers to address the problem. Therefore, _there needs to be enough information in the User-Agent string for us to contact the maintainers_.

User-Agents which break this rule are what we call "anonymous". The anonymous User-Agents, and therefore the ones subject to the throttling described earlier, are:

| User-Agent String | Version |
| --- | --- |
| <blank> | \- |
| Java | any |
| Python-urllib | any |
| Jakarta Commons-HttpClient | any |
| Apache-HttpClient | UNAVAILABLE (java 1.4) |

## How can I be a good citizen and be smart about using the Web Service?

("How do I avoid getting blocked?" / "I'm blocked! How do I get un-blocked?").

To avoid being blocked or throttled by MusicBrainz, applications should follow the advice below. If you're using an application maintained by someone else, you may need to contact the maintainers to get them to fix any problems.

If you are an application author and your application has been blocked or throttled, and none of the information on this page helped you resolve the problem, please [contact us](https://musicbrainz.org/doc/ContactUs) to talk about the problem.

### Provide meaningful User-Agent strings

_Each request sent to MusicBrainz needs to include a User-Agent header, with enough information in the User-Agent for us (MusicBrainz) to contact the application maintainers._ We strongly suggest including your application's version number in the User-Agent string too.

This is so that if there's a problem we can contact you. We suggest that your User-Agent string should look like:

```
Application name/<version> ( contact-url )
```

or

```
Application name/<version> ( contact-email )
```

Two examples:

```
MyAwesomeTagger/1.2.0 ( http://myawesometagger.example.com )
MyAwesomeTagger/1.2.0 ( me@example.com )
```

Our client libraries now support functions for setting the User-Agent string. If you are using one of [our libraries](https://musicbrainz.org/doc/Developer_Resources), you will need to add a call to set the User-Agent string via the library and your application should work again.

### Scheduling

Please refrain from having your applications wake up at a certain time of the day to perform some action. For instance, having your application wake up at 03:00 local time and query a lot of data at MusicBrainz is _a bad idea_. If this application gets distributed to many users around the globe (e.g. in a Linux distribution), then at various times around the clock, but always at the beginning of the hour, MusicBrainz will be overloaded with requests from your application. Also, 03:00 in your timezone might be the peak time for MusicBrainz somewhere else in the world. If you program your application this way and it impacts our service, we will block your application.

If there is a task you would like to perform in the background and are tempted to do it at an off-peak time, you should have your application make calls at random intervals throughout the day. If the application spreads its calls throughout the day, it spreads the load on the MusicBrainz servers across the day as well and avoids creating artificial peak times.

### Checking for changes

If you want your application to poll MusicBrainz to see if some metadata has changed, _please don't do this_. Metadata really doesn't change all that often and therefore polling for changes will rarely give good results. We currently do not have a good solution in place to let users know when metadata does change, but it is something we would like to address in the future.
