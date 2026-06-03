# TagMend — a music tag mender

> **Names (deliberate dual-naming):**
> - **GitHub repo / URL:** `music-tag-mender` — the descriptive slug captures the
>   long-tail search ("music tag mender", "fix music genre tags") in the URL.
> - **Display / brand:** **TagMend** (README H1, logo).
> - **PyPI package + CLI command + Python module:** `tagmend`
>   (`pip install tagmend`, `tagmend scan ~/Music`).
> - **MCP:** exposed as a `tagmend mcp` subcommand — *not* in the project name,
>   since this is also a CLI today and may grow a GUI later.
>
> See **§17 Naming & distribution** for the rationale.

A CLI + MCP tool that cleans up the **genre** and **artist-name** metadata in a
personal music library, using **Last.fm** as the source of truth, with an
**append-only revision history per file** so every change is fully revertible.

Designed so the boring 95% runs deterministically and unattended (via the CLI); an
LLM (via the MCP subcommand) is pulled in only to resolve the ambiguous cases.

---

## 1. Problem statement

Music ripped/tagged over years (e.g. via MusicBrainz Picard) ends up with:

- **Missing or generic genres.** Synthwave artists tagged `Electronic`, `Electro`,
  `Synthpop`, or left blank — because "synthwave" is a Last.fm/MusicBrainz
  *folksonomy tag*, not a canonical MusicBrainz *genre*, and Picard doesn't write
  folksonomy tags unless explicitly configured.
- **Inconsistent artist names.** The same band split across multiple spellings
  (`Miami Nights 1984` vs `Miami Nights '84`), which fragments artist/album grouping
  in any library manager (Navidrome, Plex, etc.).
- **Junk tags** accumulated from multiple tagging passes.

The result: you can't reliably filter/search your library by genre, and artists
don't collapse into single entities.

## 2. Goals

1. **Re-derive clean genres** from Last.fm community tags (`artist.getTopTags`),
   mapped through a controlled vocabulary.
2. **Normalize artist names** to Last.fm's canonical spelling
   (`artist.getCorrection`), so duplicates merge.
3. **Wipe the junk and rebuild** the managed tag set cleanly.
4. **Never lose data.** Every write is versioned; any file can be reverted to any
   prior version, including its original as-found state.
5. **Automate exact/high-confidence matches**; queue everything ambiguous for
   human/LLM review, then cascade the approved fix to all of that artist's files.
6. **Be free and self-hostable** — no AcoustID key, no Last.fm Plus subscription,
   no paid services. Just a free Last.fm API key.
7. **Optionally tidy the library layout** (opt-in) — rename files and rename/move
   folders into a consistent scheme, with every move tracked in an append-only log
   so the entire reorganization is revertible. Off by default; see **§18**.

## 3. Non-goals (v1)

- Audio fingerprinting / AcoustID matching (we trust existing artist/album tags
  enough to query Last.fm; fingerprinting is a possible v2).
- Editing titles, track numbers, or embedded album art.
- A GUI. The MCP client *is* the UI; a CLI covers batch runs.
- Streaming-service or DRM'd files.

---

## 4. Prior art / landscape

> Researched 2026-06-02. **Verdict: the full combination does not exist.** Three of
> our five pillars exist *individually* somewhere; the two hardest — versioned
> revertible tag history and an LLM-in-the-loop review/cascade — have essentially
> no prior art in any tool, MCP or not.

### Direct MCP competitors

| Tool | Source | Writes file tags? | Undo/versioning? | LLM-in-loop? |
|---|---|---|---|---|
| `@cynosure-mcp/music-tagger` | AcoustID fingerprint → MusicBrainz (no Last.fm) | Yes (TagLib) | **No** | No — one-shot fingerprint+overwrite |
| `elcachorrohumano/lastfm-mcp-server` | Last.fm API | No — edits **Last.fm platform tags**, not files | No | No |
| `rianvdm/lastfm-mcp` | Last.fm API | No — read-only listening data | No | No |
| `gorums/music-mcp-server` | Local files only | Sidecar `.json` (unclear if real ID3) | DB rollback only, not per-file tags | Yes |
| `usercourses63/musicbrainz-mcp-server` | MusicBrainz query | No — read-only | No | No |

### `@cynosure-mcp/music-tagger` (the one the search surfaced) — verified

Real, MIT, TypeScript/Node, npm v1.0.3 (published 2026-05-12). Pipeline: ffmpeg
decode → Chromaprint fingerprint → **AcoustID** → **MusicBrainz** metadata → Cover
Art Archive → write via TagLib. Exactly **two tools** (`read_tags`, `tag_music`).
Confirmed by unpacking the tarball: **no Last.fm**, genres come from MusicBrainz
community votes, and a `dist` grep for `revert|undo|history|backup|version|lastfm`
found **no undo/versioning and no Last.fm**. Not LLM-in-the-loop in our sense (no
review queue / no auto-vs-ambiguous classification / no cascade). Requires a free
**AcoustID** key — "free," but the opposite of our deliberate no-AcoustID stance.
*Caveat: its GitHub repo 404s, so stars/last-commit couldn't be verified; it's one
of ~18 batch-published `@cynosure-mcp/*` servers, suggesting low individual maturity.*

### Adjacent (non-MCP)

- **beets `lastgenre` plugin** — the real cousin: it **already** fetches Last.fm
  tags → genre with a **whitelist** (drops "seen live"/"favorites"), auto-on-import
  or manual. *So "Last.fm folksonomy → genre" is not novel in isolation.* But: no
  native undo/revert (a history-log is an unimplemented 2015 request, beets#1392),
  no AI review, MusicBrainz-centric, not MCP.
- **MusicBrainz Picard** — incumbent desktop GUI; no per-file tag revision history,
  no LLM assist, folksonomy genres need plugin/config fiddling.
- **MP3Tag** — session-scoped Ctrl-Z undo only (not persistent per-file history).
- **OneTagger / Nickvision Tagger / Yate / ai-music-tagger** — none combine
  Last.fm-tag genre derivation + versioned undo + AI review.

### Gap analysis — where we're novel

| Pillar | Occupied by | Status |
|---|---|---|
| Last.fm-tag genre cleanup | beets `lastgenre` (non-MCP) | Occupied — **don't lead with this** |
| **Versioned/revertible per-file tag history** | **Nobody** | **OPEN — strongest differentiator** |
| **LLM-in-the-loop review + cascade** | Nobody (as specified) | **OPEN — novel** |
| MCP interface for tag-writing from Last.fm | Nobody | OPEN |
| Free (no AcoustID/Plus) | beets; cynosure needs AcoustID | Matched, stricter than cynosure |

**Positioning:** lead the project's pitch with **versioned/revertible history + the
LLM review loop + MCP**, *not* with "Last.fm genres" (beets has that). The combination
is unoccupied and the two safety/AI pillars are genuinely new.

---

## 5. Architecture: engine + thin frontends

The valuable, risky work is deterministic and must not live inside an LLM. Build a
**core engine** once; expose it through two thin frontends.

```
                 ┌─────────────────────────┐
   MCP client →  │  MCP server (FastMCP)    │ ┐
                 └─────────────────────────┘ │   thin wrappers,
                 ┌─────────────────────────┐ │   no business logic
   terminal   →  │  CLI (argparse/typer)    │ ┘
                 └─────────────────────────┘
                              │  calls
                 ┌────────────▼─────────────┐
                 │       Core engine         │
                 │  scan · lastfm · classify │
                 │  tagwrite · versioning    │
                 │  organize (opt-in moves)  │
                 └────────────┬─────────────┘
                       ┌──────┴───────┐
                  ┌────▼────┐   ┌──────▼──────┐
                  │ SQLite  │   │  music files │
                  │  ledger │   │ (mutagen RW) │
                  └─────────┘   └─────────────┘
```

- **MCP server** = primary interface for AI-in-the-loop review. Universal standard;
  clients configure it per-folder trivially.
- **CLI** = nearly free once the engine exists; the better UX for the unattended
  bulk pass.
- A **Skill** is explicitly *out of scope for v1* — if wanted later it's just a
  playbook teaching Claude how to drive the MCP, reusing everything below.

---

## 6. Stack decision

| Concern | Choice | Why |
|---|---|---|
| Language | **Python 3.12** (`requires-python = ">=3.12"`) | Dev + floor pinned to 3.12: mature, fast, fully supported by every dep below; 3.10 is security-only (EOL Oct 2026). The deciding factor for the language itself is tag **writing** across FLAC/MP3/M4A. |
| Tag read/write | **mutagen** | Mature, multi-format, read *and* write. (Node's `music-metadata` cannot write.) |
| MCP server | **FastMCP** (`mcp` SDK) | Terse, official. |
| HTTP | `httpx` | Async-friendly, timeouts, retries. |
| State | **SQLite** (stdlib `sqlite3`, WAL mode) | Zero-dep, fast, perfect for the ledger + cache. |
| CLI | **`typer`** | Type-hint driven, auto `--help` + shell completion, good end-user UX; pairs with strict typing. |
| Config | **`platformdirs`** + JSON | One `settings.json` in the OS config dir, shared by CLI **and** MCP (the MCP process can't see the shell env); holds `music_path`, `lastfm_api_key`, etc. See **§19**. |
| Logging | stdlib `logging` via one shared helper | Mandatory project-wide; no bare `print`. See **§20**. |
| Tests | `pytest` + **synthesized** sample files | Never write to the real library; fixtures are generated empty/near-empty tagged files committed to `tests/fixtures/`. See **§21**. |
| Packaging | `uv` / `pyproject.toml` (hatchling backend) | Modern, fast. Distributed via `uv tool install tagmend` / `uvx tagmend mcp` (pipx as fallback). See **§17**. |
| Quality gates | `ruff` (near-all rules) + `mypy --strict` | Enforced every change; tuned for LLM-assisted dev. See **§21**. |

---

## 7. Data model (SQLite)

WAL mode. Four tables. **Versioning is the heart of the safety story.**

```sql
-- One row per unique artist name encountered (the dedupe + cache unit)
CREATE TABLE artist_cache (
  input_name      TEXT PRIMARY KEY,   -- the raw name as found in files
  canonical_name  TEXT,               -- from artist.getCorrection
  mbid            TEXT,
  top_tags        TEXT,               -- JSON: [{name, weight}], from getTopTags
  chosen_genre    TEXT,               -- resolved genre to write
  status          TEXT,               -- pending|auto|needs_review|approved|error
  reviewed_at     TEXT
);

-- Raw Last.fm responses, so re-runs never re-hit the API
CREATE TABLE lastfm_cache (
  request_key     TEXT PRIMARY KEY,   -- hash of method+params
  response        TEXT,               -- JSON
  fetched_at      TEXT
);

-- One row per file; points at its current revision
CREATE TABLE files (
  path            TEXT PRIMARY KEY,
  sig             TEXT,               -- size+mtime signature → "needs rescan?"
  artist_key      TEXT REFERENCES artist_cache(input_name),
  album           TEXT,
  current_version INTEGER,            -- → tag_revisions.version
  status          TEXT,               -- pending|auto|needs_review|approved|applied|error
  updated_at      TEXT
);

-- APPEND-ONLY revision log. Unique on (path, version). Never updated, never deleted.
CREATE TABLE tag_revisions (
  path            TEXT,
  version         INTEGER,            -- 0 = original as-found; +1 per write
  created_at      TEXT,
  origin          TEXT,               -- scan | auto | manual | revert
  reverted_from   INTEGER,            -- set when origin='revert'
  managed_tags    TEXT,              -- JSON: FULL snapshot of managed tags at this version
  diff            TEXT,              -- JSON: {tag: {from, to}} human-readable change
  note            TEXT,
  PRIMARY KEY (path, version)
);
```

> **Note — file identity vs. path (relevant once §18 organize lands).** The tables
> above key files by `path`. That's fine while files never move. The opt-in
> move/rename feature (**§18**) breaks that assumption: a file's path changes but its
> tag-revision history must follow it. The plan there is to introduce a stable
> surrogate `file_id` (content-derived or DB-assigned at first scan) that `files`,
> `tag_revisions`, and the new `path_revisions` table all reference, demoting `path`
> to a mutable attribute. We keep `path`-keyed tables for the v1 read/write path and
> migrate to `file_id` when we build organize, so the two history logs (tags *and*
> paths) stay linked across a move. Final identity scheme is an open question (§15).

### Versioning / undo semantics (your requirement)

- **Version 0** is captured **at first scan, before any write** — the original
  as-found tags. This is the permanent safety baseline.
- Every write **appends** a new row with an incremented `version`, storing both a
  **full snapshot** of the managed tag set (`managed_tags`) and a **diff** of what
  changed. `files.current_version` advances.
- **Revert(path, target_version)** = read `managed_tags` from the target revision,
  write them back to the file, then **append a new revision** with
  `origin='revert'` and `reverted_from=target_version`. History is append-only —
  you can revert a revert, and you never lose any prior state.
- "Managed tags" = a deliberately **narrow** set: `GENRE`, `ARTIST`(careful),
  `ALBUMARTIST`, `MUSICBRAINZ_ARTISTID`. We never touch title/track/art, so
  snapshots stay small and reverts can't damage unrelated metadata.

---

## 8. Last.fm integration

Free API key only. Key endpoints:

- **`artist.getCorrection`** → canonical artist name + MBID. *This is the
  artist-name normalization feature* — no need to infer from search.
- **`artist.getTopTags`** → ranked community tags with 0–100 `count` (weight).
  This is the genre source + confidence signal.
- **`album.getTopTags`** → optional per-album override for mixed-catalog artists
  (v1 defaults to artist-level).

### Rate limiting & caching (facts)

- Documented limit: **~5 requests/sec per IP**, averaged over 5 min. No published
  daily quota, but abuse → throttle/ban.
- **There is no batch endpoint** — one call per artist. "Batching" = chunking your
  library while pacing. Default pace: **1 req/sec** (well under the cap).
- **Dedupe by artist + persistent cache**: each unique artist is queried *once
  ever*; results live in `lastfm_cache` and `artist_cache`. A few-hundred-artist
  library = a few hundred calls total; re-runs are free. Cache TTL configurable
  (default: never expire; manual `refresh` per artist).

---

## 9. Genre resolution logic

1. For each artist: `getCorrection` (name) + `getTopTags` (genres).
2. Filter tags through a **controlled vocabulary / allow-list** (e.g. `synthwave`,
   `retrowave`, `darksynth`, `synthpop`, ...). Drop junk tags
   (`seen live`, `favorites`, `00s`).
3. Apply a **weight threshold** (e.g. ≥ 50, or top tag dominates by N points) to
   decide *auto* vs *needs_review*.
4. **Tag→genre mapping table** (shipped default, user-overridable) maps Last.fm tag
   strings to the canonical genre string written to files. This mapping is the main
   place the **LLM adds value** — curating it and resolving ambiguity.
5. Default scope is **artist-level genre**; album-level override available.

---

## 10. The review workflow (auto vs human/LLM)

`files.status` / `artist_cache.status` *is* the workflow engine.

```
scan ──> pending
              │  engine resolves artist via Last.fm
       ┌──────┴───────┐
   high-confidence   ambiguous / no match / name collision
       │                     │
     auto                needs_review  ──>  LLM reviews via MCP
       │                     │              (picks canonical name / genre)
       │                  approve_mapping
       │                     │
       └──────► commit ◄─────┘   (writes tags, bumps version)
                  │
               applied
```

- Auto cases can be applied unattended (CLI `--apply`) or after a bulk confirm.
- `needs_review` items surface via MCP `list_pending_review`. The LLM fixes the
  **artist-level** mapping; the engine then **cascades deterministically** to all
  of that artist's files and re-runs them. (Your "fix primary data, then re-run for
  that artist" loop.)

---

## 11. Safety model

- **Dry-run by default.** Engine computes the full plan and writes nothing until a
  mapping is `approved`. CLI gains `--apply`; MCP has an explicit `commit_*` tool.
- **Version 0 baseline** captured before the first write → always revertible.
- **Surgical writes.** Only the narrow managed-tag set. `ALBUMARTIST` is the merge
  key for collapsing duplicate artists; per-track `ARTIST` is touched cautiously
  (preserves "feat." credits) and is opt-in.
- **Atomic writes on network shares.** Write to temp + atomic rename where the
  format/OS allows, to survive a dropped NAS connection mid-write.
- **Append-only history.** Revisions are never mutated or deleted.
- **Moves are opt-in and tracked.** File/folder reorganization (§18) is disabled by
  default, dry-run-planned first, executed atomically per item, and recorded in an
  append-only `path_revisions` log so any rename/move is individually revertible.

---

## 12. MCP tool surface (v1)

| Tool | Purpose |
|---|---|
| `scan(path)` | Walk a folder, populate `files` + capture version-0 baselines. |
| `resolve_artists()` | Query Last.fm (cached/paced), classify auto vs needs_review. |
| `list_pending_review()` | Return artists/files needing human/LLM decisions. |
| `get_artist_candidate(name)` | Full Last.fm context for one artist (tags, correction, similar). |
| `approve_mapping(input_name, canonical_name, genre)` | Record an approved artist-level decision. |
| `commit_artist(input_name)` | Apply approved tags to all that artist's files (bumps versions). |
| `revert(path, version)` | Restore a file to a prior revision (append-only). |
| `history(path)` | Show the revision log + diffs for a file. |
| `stats()` | Library-wide progress (pending/auto/applied/error counts). |
| `health_check()` | **Readiness probe (M0):** verify settings load, music path is reachable & readable, and the SQLite ledger opens. The first tool we ship; callable from the MCP Inspector to prove the environment is wired up. |

**Organize (opt-in, §18) — added once M6 lands:**

| Tool | Purpose |
|---|---|
| `plan_organize(path)` | Compute the dry-run move/rename plan (folder + file targets) without touching disk. |
| `commit_organize(plan_id)` | Execute an approved move plan atomically per item; append `path_revisions`. |
| `revert_move(file_id, version)` | Restore a file/folder to a prior path revision (append-only). |
| `move_history(file_id)` | Show the path-revision log for a file. |

Mirror the same operations as CLI subcommands.

---

## 13. Repo structure

```
music-tag-mender/             # GitHub repo slug (SEO)
├── PLAN.md                    # this file
├── README.md                  # H1: "TagMend — a music tag mender"
├── pyproject.toml             # [project] name = "tagmend"
├── src/tagmend/               # importable package = tagmend
│   ├── __init__.py
│   ├── log.py               # one shared logger factory — used everywhere (§20)
│   ├── config.py            # settings.json in OS config dir via platformdirs (§19)
│   ├── engine/
│   │   ├── db.py             # SQLite connection (WAL); schema added per-feature later
│   │   ├── doctor.py         # health_check: settings + music path + db readiness (M0)
│   │   ├── scan.py           # walk library, signatures, version-0 capture
│   │   ├── lastfm.py         # client: getCorrection/getTopTags, cache, pacing
│   │   ├── classify.py       # vocabulary, thresholds, auto vs review
│   │   ├── tags.py           # mutagen read/write of managed tag set
│   │   ├── versioning.py     # tag-revision append + revert
│   │   └── moves.py          # opt-in file/folder reorganization + path_revisions (§18)
│   ├── data/
│   │   └── genre_vocabulary.yml  # default tag→genre allow-list (shipped as package data)
│   ├── mcp_server.py          # FastMCP — thin wrapper over engine
│   └── cli.py                 # typer — `tagmend …`; `tagmend mcp` launches the server
└── tests/
    ├── conftest.py           # shared fixtures (temp library, temp config/db)
    ├── fixtures/             # synthesized sample .flac/.mp3/.m4a with known tags (§21)
    └── test_*.py
```

`pyproject.toml` entry point: `[project.scripts] tagmend = "tagmend.cli:app"`.
The `tagmend mcp` subcommand imports and runs `mcp_server.py` (FastMCP over stdio),
so there is one install, one command, and the MCP server is just one of its modes.

---

## 14. Roadmap / milestones

- **M0 — Skeleton.** Repo, `pyproject`, strict ruff + mypy gates, shared logger,
  `settings.json` config, SQLite connection (WAL, no tables yet), FastMCP server +
  CLI wired together, and a working `health_check`/`doctor` that proves the music
  path is reachable. Dry-run only, nothing writes. **← current milestone.**
- **M1 — Read path.** `scan` + version-0 capture; `tags.py` read; `stats`.
- **M2 — Last.fm.** Client with cache + pacing; `resolve_artists`; classification.
- **M3 — Write path + versioning.** `commit_artist`, append revisions,
  atomic writes, `revert`, `history`. **Backups proven before any real run.**
- **M4 — Review loop.** `list_pending_review`, `approve_mapping`, cascade + re-run.
- **M5 — Polish.** Genre vocabulary tuning, album-level override, docs, packaging.
- **M6 — Organize (opt-in moves & renames).** Stable `file_id` migration,
  `plan_organize` (dry-run path plan), `commit_organize` (atomic per-item moves),
  `path_revisions` append-only log, `revert_move`, `move_history`. Gated behind a
  config flag; **revert proven before any real run** (same bar as M3). See **§18**.

## 15. Open questions

- Genre scope default: artist-level only in v1, or detect mixed-catalog artists?
- Multi-value genres (e.g. `synthwave; retrowave`) — allow N, or force single?
- Signature: `size+mtime` (fast, NAS-friendly) vs content hash (safer, slow)?
- Should `revert` be exposed in the CLI bulk path or MCP-only (to keep it deliberate)?
- **Organize (§18):** what is the stable `file_id` — content hash (survives moves
  *and* re-tags, but slow on a NAS) vs. a DB-assigned id anchored by `(size, mtime,
  path)` at first scan (fast, but re-identifying a file moved outside the tool is
  harder)?
- **Organize (§18):** what is the default target naming scheme
  (`Artist/(Year) Album/NN Title.ext`?), and how configurable should it be?
- **Organize (§18):** when a folder rename collapses two artist spellings into one
  destination, how do we handle the merge / collision (refuse, suffix, or merge)?
- **Organize (§18):** do we move non-audio sidecars (cover art, `.nfo`) with the
  album, and do we delete now-empty source folders?

## 16. References

- Last.fm API: `artist.getCorrection`, `artist.getTopTags`, `album.getTopTags`,
  rate-limit terms.
- mutagen docs (FLAC/Vorbis, ID3, MP4).
- MCP / FastMCP SDK.

## 17. Naming & distribution

Deliberate **dual-naming** — each name assigned to the layer where it works best,
so we cover both discovery and ergonomics instead of trading one for the other:

| Layer | Name | Why |
|---|---|---|
| GitHub repo / URL | `music-tag-mender` | Hyphens are word boundaries to crawlers → the URL matches the descriptive long-tail query ("music tag mender", "fix music genre tags"). Competing tools (Picard, Mp3tag) don't contain "mender", so the field is wide open. |
| Display / brand | **TagMend** | Memorable CamelCase for the README H1, logo, and word-of-mouth. |
| PyPI package + CLI + module | `tagmend` | Short and ergonomic: `pip install tagmend`, `tagmend scan …`. PyPI normalizes case, so `TagMend` ≡ `tagmend`. |
| MCP | `tagmend mcp` subcommand | Keep "mcp" out of the project name — this is a CLI today and may grow a GUI. |

The two names reinforce each other in search: the repo ranks for the descriptive
phrase, the README/PyPI page link `tagmend` ↔ the repo, so the brand and the
keywords become associated and cover a wider surface.

**Trademark note:** "Last.fm" stays out of the name (it's a trademark). Use it
*descriptively* in the tagline/README only — e.g. "powered by Last.fm community
tags" — never in a way that implies endorsement.

**Tagline:** *TagMend — mend your music tags. Genre & artist-name cleanup from
Last.fm community tags, with full revertible history.*

**GitHub repo topics (for descriptive-search SEO):** `music`, `metadata`, `id3`,
`flac`, `tagging`, `genre`, `lastfm`, `musicbrainz`, `mcp`, `cli`.

---

## 18. File & folder organization (opt-in moves & renames)

> Added per the move/rename requirement. This is a **distinct, opt-in** capability
> layered on top of the tag engine. It is **off by default** (`organize.enabled =
> false`) because not everyone wants their on-disk layout touched. Like tag writes,
> it is **dry-run-planned, atomic, and fully revertible** — but it gets its **own
> append-only log** (`path_revisions`) separate from `tag_revisions`, since renaming
> a file and re-genre-ing it are independent concerns that must revert independently.

### 18.1 Why a separate table

A tag change rewrites bytes *inside* a file at a fixed path; a move/rename changes
the file's *location/name* with identical bytes. Mixing them in one log would make
"revert just the move, keep the new genre" (and vice-versa) awkward. So:

- `tag_revisions` — the managed-tag content history (§7), keyed by `file_id`.
- `path_revisions` — the location history, also keyed by `file_id`.

Both reference the **stable `file_id`** introduced for organize (see the §7 note),
so a file keeps one continuous identity across any combination of re-tags and moves.

### 18.2 Proposed data model (finalized at M6)

```sql
-- APPEND-ONLY location log. One row per move/rename. Never updated, never deleted.
CREATE TABLE path_revisions (
  file_id       TEXT,                -- stable identity (see §7 note / §15)
  version       INTEGER,             -- 0 = path as-found at first scan; +1 per move
  created_at    TEXT,
  origin        TEXT,                -- scan | organize | revert
  reverted_from INTEGER,            -- set when origin='revert'
  kind          TEXT,               -- file_rename | folder_rename | move
  from_path     TEXT,               -- absolute source path
  to_path       TEXT,               -- absolute destination path
  plan_id       TEXT,               -- groups all moves committed together (one organize run)
  note          TEXT,
  PRIMARY KEY (file_id, version)
);

-- Optional: a row per planned-but-not-yet-committed reorganization, for review/approval.
CREATE TABLE move_plans (
  plan_id       TEXT PRIMARY KEY,
  created_at    TEXT,
  status        TEXT,               -- proposed | approved | committed | aborted
  summary       TEXT                -- JSON: list of {file_id, kind, from, to}
);
```

Folder renames are modeled as a set of per-file move rows sharing a `plan_id`
(the folder is just the common path prefix), so revert can operate per-file or
per-plan, and an interrupted run is recoverable by replaying the plan.

### 18.3 Semantics

- **Plan first.** `plan_organize(path)` computes destination paths from the target
  scheme + the (already cleaned) tags, detects collisions, and returns a dry-run
  diff. Nothing on disk changes.
- **Commit atomically.** `commit_organize(plan_id)` moves each item with a temp +
  atomic-rename where the OS/filesystem allows (NAS-safe), appends a `path_revisions`
  row per item, and advances the file's current path version. Non-audio sidecars
  (art, `.nfo`) move with their album by default (configurable).
- **Revert.** `revert_move(file_id, version)` moves the file back to the target
  revision's path and appends a new `origin='revert'` row — same append-only model
  as tag reverts. `revert` of a whole `plan_id` undoes an entire run.
- **Empty source folders** left behind by a move are removed only when empty and
  only if `organize.prune_empty_dirs = true`.

### 18.4 Default target scheme (proposed, configurable)

```
<library_root>/<AlbumArtist>/(<Year>) <Album>/<NN> <Title>.<ext>
```

Driven by config (`organize.path_template`, `organize.folder_template`). The dirty
test fixtures in `music/` (e.g. `Miami_Nights(1984)-_Early_Summer_(2010)` and
`Miami Nights '84 - 2012 - Turbulence`) are exactly the messy inputs this should
normalize — and a good demonstration of why artist-name normalization (§8) must
run *before* organize, so both spellings land under one canonical folder.

---

## 19. Settings & configuration

Both frontends need the same settings, but the **MCP server runs as a subprocess of
the MCP client and cannot see the shell environment** the CLI was launched from. So
config lives in **one file on disk**, not in env vars:

- **Location:** `platformdirs.user_config_dir("tagmend")` → e.g.
  `%APPDATA%\tagmend\settings.json` (Windows), `~/.config/tagmend/settings.json`
  (Linux), `~/Library/Application Support/tagmend/settings.json` (macOS).
- **Contents (v1):** `music_path`, `lastfm_api_key`; more added per feature
  (`organize.*`, pacing, vocabulary path, db path override).
- **Precedence:** explicit CLI flag / MCP arg > `TAGMEND_*` env override (CLI
  convenience only) > `settings.json` > built-in defaults.
- **Secret hygiene:** the file holds the Last.fm key; created with user-only
  permissions where the OS supports it, and never logged.
- `config.py` exposes a typed `Settings` object + `load_settings()` so the engine
  never reads raw JSON directly.

The default SQLite ledger lives alongside it in `user_data_dir("tagmend")`.

---

## 20. Logging

A single shared logger, used **everywhere** — no bare `print()` in engine, CLI, or
server code (user-facing CLI output goes through Typer/`rich`, which is distinct
from logging).

- `tagmend/log.py` exposes `get_logger(__name__)` returning a configured child of
  the `tagmend` root logger. (Named `log.py`, not `logging.py`, to avoid shadowing
  the stdlib module under the strict linter.)
- **MCP-safe:** when running as the MCP server over **stdio**, logs go to **stderr**
  only — stdout is the JSON-RPC channel and must never be polluted.
- Level via `--verbose`/`-v` flags and `TAGMEND_LOG_LEVEL`; default `INFO` (CLI) /
  `WARNING` (server).
- Structured-ish: timestamp, level, logger name, message; never logs secrets.

---

## 21. Quality gates (required every change)

Tuned to catch bugs/security/maintainability issues early — especially valuable for
LLM-assisted development. All three must pass before anything is considered done:

1. **`ruff check`** — near-comprehensive rule set (pyflakes, bugbears, security
   `S`/bandit, comprehensions, naming, complexity, import hygiene, pathlib, datetime,
   typing-modernization, and more) + `ruff format`.
2. **`mypy --strict`** — full static typing; no implicit `Any`, no untyped defs.
3. **`pytest`** — tests run against **synthesized fixtures**, never the real `music/`
   library.

Exact commands live in `CLAUDE.md` so any fresh context can run the gate immediately.
