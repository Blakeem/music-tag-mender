Source: https://musicbrainz.org/doc/Rating_System
Version: MusicBrainz ws/2 (production), page transcluded from https://wiki.musicbrainz.org/Rating_System
Retrieved: 2026-07-07

## Description

The rating system allows users to rate MusicBrainz entities (artists, labels, release groups, recordings, events, works).

## Details

User may assign a value between 1 and 5 to various entities as a rating, these values are then aggregated by the server to compute an average community rating for that same entity.

The community rating is shown by default for each entity, however, once a user submits their own rating that value will be shown instead.

## Coverage note (not part of the source page)

This page documents the **display/UI** scale only (1–5, aggregated into a community average). It does
**not** mention the web-service API's 0–100 `<user-rating>` integer scale used when submitting ratings
via `POST /ws/2/rating` (see `official/musicbrainz-api.md` §"ratings" in this doc set). The relationship
between the two scales is not stated on any official page found; see
`community-undocumented/rating-value-scale-and-api-mapping.md` for the verified mapping.
