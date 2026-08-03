Source: https://www.last.fm/api/rest
Version: Last.fm API 2.0
Retrieved: 2026-07-07

# REST Requests

> **API ROOT**
>
> The API root URL is located at [http://ws.audioscrobbler.com/2.0/](http://ws.audioscrobbler.com/2.0)

Generally speaking, you will send a method parameter expressed as 'package.method' along with method specific arguments to the root URL. The following parameters are required for all calls:

**api_key**: A Last.fm API Key.
**method**: An API method expressed as *package.method*, corresponding to a documented last.fm API method name.

For example:

```xml
http://ws.audioscrobbler.com/2.0/?method=artist.getSimilar&api_key=xxx...
```

If you are accessing a *write* service, you will need to submit your request as an HTTP POST request. All POST requests should be made to the root url:

```xml
http://ws.audioscrobbler.com/2.0/
```

With all parameters (including the 'method') sent in the POST body. In order to perform write requests you will need to authenticate a user with the API. See [authentication](https://www.last.fm/api/authentication) for more.

## REST Responses

Responses will be wrapped in an lfm status node

```xml
<lfm status="$status">
    ...
</lfm>
```

Where *$status* is either **ok** or **failed**. If the status is failed you'll get an error code and message. You can strip the status wrapper from the response by sending a **raw=true** argument with your method call.

## REST Errors

See the individual method call pages for service specific error codes. Errors will communicate a code and a message in the following format:

```xml
<lfm status="failed">
    <error code="10">Invalid API Key</error>
</lfm>

```

## JSON Responses

You can request API responses in JSON format with the following parameters:

**format=json**: Request API responses in JSON format.
**callback** (Optional): A callback function name which will wrap the JSON response.

## Note:

If you don't specify a callback, there's no default, and the response will be pure JSON content with a `application/json` MIME type. With a callback, the MIME type is `text/javascript`

The response is a translation of the XML response format, converted according to the following rules:

1. Attributes are expressed as string member values with the attribute name as key.
2. Element child nodes are expressed as object members values with the node name as key.
3. Text child nodes are expressed as string values, unless the element also contains attributes, in which case the text node is expressed as a string member value with the key `#text`. *
4. Repeated child nodes will be grouped as an array member with the shared node name as key.

* This idiom is rarely used in our XML responses.

## Example success response:

```json
{
"results": {
    "tagmatches": {
      "tag": \[{
        "name": "disco",
        "count": "55483",
        "url": "www.last.fm\\/tag\\/disco"
      },
      ...
      {
        "name": "disco pop",
        "count": "160",
        "url": "www.last.fm\\/tag\\/disco%20pop"
      }\]
    },
    "for": "disco"
  }
}
```

## Original XML response:

```xml
<?xml version="1.0" encoding="utf-8"?>
<lfm status="ok">
 <results for="disco" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
    <opensearch:Query role="request" searchTerms="disco" startPage="1" />
    <opensearch:totalResults>2641</opensearch:totalResults>
    <opensearch:startIndex>0</opensearch:startIndex>
    <opensearch:itemsPerPage>20</opensearch:itemsPerPage>
    <tagmatches>
      <tag>
        <name>disco</name>
        <count>55483</count>
        <url>www.last.fm/tag/disco</url>
      </tag>
      ...
      <tag>
        <name>disco pop</name>
        <count>160</count>
        <url>www.last.fm/tag/disco%20pop</url>
      </tag>
    </tagmatches>
  </results>
</lfm>

```

## JSON Errors

JSON errors do not follow the same transformation rules as success errors, but use the following simplified form:

## Example failure response:

```json
{
    "error": 10,
    "message": "Invalid API Key"
}
```
