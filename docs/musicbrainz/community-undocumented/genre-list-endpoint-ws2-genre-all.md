# `/ws/2/genre/all` — bulk genre list endpoint (previously requested/unavailable, now live)

Source: https://community.metabrainz.org/t/genre-tag-names-api-list/404075 ("Genre tag names API list" — MetaBrainz Community Discourse)
Kind: community feature request whose resolution is verified live (endpoint now exists; thread itself describes it as NOT existing at time of posting)
Retrieved: 2026-07-07
API version: MusicBrainz ws/2 (current production)

## Claim (verbatim/paraphrased from the thread, as it stood historically)

Original poster **PoQStacker** asked for a web-service query to retrieve the full official genre list (distinct from per-entity `inc=genres`), analogous to `https://musicbrainz.org/ws/2/release/?inc=genre-list`.

**reosarevok** (MetaBrainz staff) confirmed that at the time, **there was no dedicated API endpoint for genres** — "requesting the full list of possible genres seemed irrelevant" since `inc=genres` only surfaces genres applicable to a specific entity, not the master list. As an interim workaround, the community pointed to a raw GitHub-hosted JSON file (`https://raw.githubusercontent.com/metabrainz/musicbrainz-server/master/entities.json`, read `tag.genres`), noting it updates biweekly with production deploys and sometimes gets new genres even before the live site does. A future endpoint tentatively named `ws/2/genre` was tracked under ticket **MBS-9880**.

## Verification (exact command + response, verbatim)

```
$ curl -s -D - -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/genre/all?fmt=json&limit=5"
```
```
HTTP/2 200
content-type: application/json; charset=utf-8
content-length: 455

{"genre-count":2165,"genre-offset":0,"genres":[{"name":"2 tone","id":"18797864-41b0-4602-8241-adbee761774c","disambiguation":""},{"disambiguation":"","id":"db325bd7-ae64-40bd-966a-a3af3cef8bb9","name":"2-step"},{"id":"99032cf7-a4e3-40ac-b943-293b44b9a65b","disambiguation":"","name":"3-step"},{"name":"aak","id":"bdc421e1-0164-4c14-9927-4217bfb25667","disambiguation":""},{"disambiguation":"","id":"6e319459-3520-4079-8755-d8c9f35ce78e","name":"abhang"}]}
```

The endpoint `/ws/2/genre/all` is now live and returns `genre-count: 2165` total genres, each with `id`/`name`/`disambiguation`, confirming ticket MBS-9880 (referenced in the thread) has since shipped. This resolves the exact gap the community thread complained about — the endpoint the thread said didn't exist now does.

## Confirmed behavior for integrators

- `GET /ws/2/genre/all` returns the complete, paginated master list of official MusicBrainz genres (2,165 at time of testing), each with `id`, `name`, `disambiguation` — no need to scrape the `entities.json` file on GitHub as a workaround anymore.
- Standard browse pagination applies: response includes `genre-count`, `genre-offset`, and a `genres` array; use `limit=`/`offset=` to page through the full list.
- This endpoint is easy to miss because, at least as of the community thread, it was undocumented/unannounced relative to the rest of `MusicBrainz_API` — if your official-docs capture of the API doesn't mention `/ws/2/genre/all`, that's a documentation gap, not a sign the endpoint doesn't exist; it does, as verified above.
- Per-entity genre tags remain available the original way too, via `inc=genres` on the relevant entity lookup (returns only genres that apply to that entity, not the master list).
