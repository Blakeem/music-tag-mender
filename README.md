# music-tag-mender
TagMend includes an MCP server and command line tools to correct meta tags and the folder structure your music files. All changes are tracked and can be safely rolled back so that tags can be cleaned and files moved and renamed without risk. Uses Last.fm free API. Created to make Navidrome MCP more useful by providing accurate names and genres.

## Configuration UI

TagMend stores its settings in an on-disk `settings.json` (see `settings.example.json` for
every supported key). You can edit it from a small local web form instead of memorizing
`tagmend config-set` keys:

```
tagmend config
```

This starts a loopback-only web server on `127.0.0.1:<random-port>`, prints the URL, and
opens your browser. Use the **Test Last.fm key** button to verify your API key, then **Save**.
Press Ctrl-C to stop the server.

When you launch the MCP server with `tagmend mcp` and a core setting (`music_path` or
`lastfm_api_key`) is still missing, TagMend auto-opens the same config UI in the background
and logs the URL to stderr, then continues serving normally.

Two environment switches control the browser/auto-launch behavior:

- `TAGMEND_NO_BROWSER` — start the server but do not open a browser window.
- `TAGMEND_NO_CONFIG_UI` — never auto-launch the config UI from `tagmend mcp`.

Edits apply on the next tool call: every command and MCP tool re-reads `settings.json`
fresh (there is no in-process settings cache).
