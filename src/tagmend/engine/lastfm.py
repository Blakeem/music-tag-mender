"""Last.fm client: artist correction + top tags, with caching and pacing (M2).

Free API key only. Key endpoints:

* ``artist.getCorrection`` — canonical artist name + MBID (name normalization).
* ``artist.getTopTags``   — ranked community tags (genre source + confidence).
* ``album.getTopTags``    — optional per-album override.

Responses are cached persistently (``lastfm_cache``) and paced (default 1 req/sec)
so each unique artist is queried once ever and re-runs are free.

Planned public API::

    LastfmClient(api_key, *, cache, rate_per_sec=1.0)
        .get_correction(name) -> Correction
        .get_top_tags(artist) -> list[Tag]

Not yet implemented — see PLAN.md §8.
"""

from __future__ import annotations
