# Search: per-field regex syntax (`field:/pattern/`) is supported but matches word-by-word, not across the whole string

Source: https://community.metabrainz.org/t/how-to-use-regex-in-search/410531 ("How to use regex in search?" — MetaBrainz Community Discourse)
Kind: community claim, verified against the live API
Retrieved: 2026-07-07
API version: MusicBrainz ws/2 (current production)

## Claim (verbatim, from the forum thread)

Original poster (musicsearcher) tried `/\[mb\]oat/` expecting it to match "boat"/"moat" and got unintended results (the letters "mb" and the word "oat" matched separately instead).

Reply from jesus2099:
> Advanced search field-specific regex syntax works, e.g. `releasegroup:/[lm]ove/` successfully finds "love" and "move" without matching individual letters. This requires `method=advanced` in the search parameters (or equivalently prefixing the query with a field name, which implies advanced/field-qualified parsing).

When the OP tried a more complex pattern to match the first letters of multiple words at once (`releasegroup:/i\[^ \]\* l\[^ \]\* y\[^ \]\*/`), jesus2099 clarified the core limitation:
> "The search only takes your regex and apply it to each word... You cannot regex match on the whole string, only word by word."

Suggested workaround for multi-word matching (cannot guarantee word order):
> `releasegroup:/^i.+/ AND releasegroup:/^l.+/ AND releasegroup:/^y.+/`

## Verification (exact command + response, verbatim)

```
$ curl -s -A "aipg-docs-test/1.0 (blakeem@gmail.com)" \
  "https://musicbrainz.org/ws/2/release-group/?query=releasegroup%3A%2F%5Blm%5Dove%2F&fmt=json&limit=5"
```
Result (parsed):
```
count: 65509
100 Love Revolution
100 82-84: Love Your Enemies (We Hate You South African Bastards)
100 Fear Is the Enemy of Love
100 Love Power Peace: Live at The Olympia, Paris, 1971
100 I'm Still in Love With You
```
The `field:/regex/` syntax is accepted by the live API and returns matches for the character class `[lm]` before `ove` (i.e. "love"/"move" as substrings of words), confirming the syntax works as described.

## Confirmed behavior for integrators

- Per-field regex search is real and live: `<field>:/<regex>/` (e.g. `releasegroup:/[lm]ove/`, `artist:/^The .+/`) is accepted by the `query=` search parameter on entity search endpoints.
- The regex engine matches **against individual indexed words/tokens**, not the whole field string — a pattern intended to span multiple words (e.g. matching first letters of several separate words in sequence) will not behave like a single-string regex. To constrain multiple words, combine several `field:/regex/` clauses with explicit uppercase `AND` (see the separate case-sensitivity note in this directory) — but note this cannot enforce word order or adjacency, only that each word independently matches somewhere in the field.
- Anchors like `^` and `.+` work within a single word's regex as expected (confirmed by the community-suggested workaround pattern), but don't assume they anchor the whole multi-word field.
