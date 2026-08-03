# TagMend — a music tag mender

> **Names (deliberate dual-naming):**
> - **GitHub repo / URL:** `music-tag-mender` — the descriptive slug captures the
>   long-tail search ("music tag mender", "fix music genre tags") in the URL.
> - **Display / brand:** **TagMend** (README H1, logo).
> - **PyPI package + CLI command + Python module:** `tagmend`
>   (`pip install tagmend`, `tagmend scan-library ~/Music`).
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

WAL mode. **Versioning is the heart of the safety story.** `PRAGMA user_version`
tracks the applied schema version (**currently 12**: the read-path snapshot landed at
M1; the `commits` / `tag_revisions` / `tag_revisions_staged` change-tracking tables
shipped at M3; `lastfm_cache` + `file_genre_status` shipped at M2). The model is
**resume-free** — a crash just leaves work staged for the next commit to sweep up; see
the semantics below.

### 7.1 Created NOW (M1 — the read-path snapshot)

A file's stable identity is a **DB-assigned integer surrogate** (`files.id`), anchored
by `(folder, filename)` at first scan. This is the durable id every later history table
references; it resolves the §15 identity question. The old `path` column is split into
the mutable `folder` (absolute directory) + `filename` (basename). Tags are stored in a
**normalized EAV linked table** (`file_tags`) — one row per value, no raw JSON.

```sql
CREATE TABLE IF NOT EXISTS files (
  id              INTEGER PRIMARY KEY,           -- stable surrogate identity
  folder          TEXT NOT NULL,                 -- absolute directory
  filename        TEXT NOT NULL,                 -- basename incl. extension
  ext             TEXT NOT NULL,                 -- lowercased, e.g. '.flac'
  size_bytes      INTEGER,                       -- signature part 1
  mtime_ns        INTEGER,                       -- signature part 2 (st_mtime_ns)
  is_missing      INTEGER NOT NULL DEFAULT 0,    -- 1 = path gone from disk
  first_seen_at   TEXT NOT NULL,                 -- ISO-8601 UTC
  updated_at      TEXT NOT NULL,                 -- ISO-8601 UTC
  tags_updated_at TEXT,                          -- NULL = tags not yet read ("unprocessed")
  status          TEXT NOT NULL DEFAULT 'scanned',
  UNIQUE (folder, filename)
);

CREATE TABLE IF NOT EXISTS file_tags (
  file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  name      TEXT NOT NULL,                -- canonical tag name, lowercase
  ordinal   INTEGER NOT NULL DEFAULT 0,   -- 0-based index for multi-value tags
  value     TEXT NOT NULL,
  PRIMARY KEY (file_id, name, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_file_tags_name_value ON file_tags(name, value);
```

The signature is **`size_bytes` + `mtime_ns`** (fast, NAS-friendly); an incremental
scan re-reads a file's tags only when that signature changes or the file was never read.

### 7.2 The M2 genre tables (shipped) + the change-tracking tables (M3, shipped)

M2 shipped **two** tables — and they differ from the original sketch. The planned
`artist_cache` (an artist-level workflow row holding a single `chosen_genre` + review
status) was **superseded**: caching happens at the *request* level (`lastfm_cache`,
which also negative-caches "not on Last.fm") and workflow status at the *file* level
(`file_genre_status`), because genre resolution merges artist **and album** tags
(§9), so there is no single artist-level "chosen genre" to cache. An artist-level
table may return with M4's name-correction/review loop. The change-tracking tables
(`commits`, `tag_revisions`, `tag_revisions_staged`) **shipped in M3** (write path),
including the version-0 baseline; the revision logs are **keyed to `files.id`** with
a composite PK `(file_id, version)`.

The model deliberately mirrors **git** (see §7's semantics): a **`commits`** row is a
group of changes applied together (git's *commit*); its `id` is the `commit_id` each
revision references. A **staging area** (`*_staged`, git's *index*) holds the desired
target until you commit; committing turns each staged row into an append-only revision
and deletes it. Per-file `version` is the friendly per-file restore handle.

The shared commit machinery (the `commits` table, the crash-safe per-file commit loop,
and a small `RevisionDomain` seam) lives in `engine/commits.py` and is **domain-neutral**,
so the tags side (`engine/staging.py`, shipped) and the future paths side
(`engine/paths.py`, §18) are two parallel implementations of the same lifecycle.

```sql
-- M2 (shipped): parsed Last.fm responses, so re-runs never re-hit the API.
-- `found` is the negative-cache sentinel: 0 = artist/album genuinely absent from
-- Last.fm (remembered, so re-runs skip it too); distinct from found=1 with tags=[].
CREATE TABLE lastfm_cache (
  request_key     TEXT PRIMARY KEY,   -- hash of method + identity params
  found           INTEGER NOT NULL,   -- 1 = found, 0 = negative cache
  tags            TEXT NOT NULL,      -- JSON: [[name, weight], ...] parsed top tags
  fetched_at      TEXT NOT NULL
);

-- M2 (shipped): per-file genre workflow status. Only the two TERMINAL/negative
-- outcomes get a row: 'no_match' (nothing usable on Last.fm) and 'manual' (user/LLM
-- excluded it — "I'll tag this by hand"). "pending" is the ABSENCE of a row, and
-- "done" is DERIVED from the staged/committed revision tables — no 'tagged' state
-- to desync. source_artist/source_album record the identity the decision was
-- computed against, so a later artist/album retag makes a 'no_match' row STALE and
-- the file is automatically retried on the next resolve_genres pass.
CREATE TABLE file_genre_status (
  file_id         INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
  status          TEXT NOT NULL,      -- no_match | manual
  source_artist   TEXT,
  source_album    TEXT,
  updated_at      TEXT NOT NULL
);

-- M3: one row per commit (a group of changes applied together). id == commit_id.
CREATE TABLE commits (
  id              INTEGER PRIMARY KEY,
  created_at      TEXT,
  origin          TEXT,               -- auto | manual | revert | organize
  message         TEXT,               -- the group-level human description
  reverted_from   INTEGER REFERENCES commits(id),  -- the commit this one undoes
  status          TEXT                -- applying | applied | interrupted (crash marker)
);

-- M3: staging area (git's index). One pending tag change per file; holds the TARGET.
-- Resume-free (schema v5): staged rows carry NO commit_id (no claiming). A commit turns
-- each staged row into a tag_revisions row then deletes it; a crash leaves leftover rows
-- staged for the next commit to sweep into a new commit.
CREATE TABLE tag_revisions_staged (
  file_id         INTEGER REFERENCES files(id),
  managed_tags    TEXT,               -- JSON: proposed target snapshot of managed tags
  origin          TEXT,               -- auto | manual
  note            TEXT,
  staged_at       TEXT,
  PRIMARY KEY (file_id)
);

-- M3: APPEND-ONLY revision log, keyed by the stable file_id. Never updated/deleted.
CREATE TABLE tag_revisions (
  file_id         INTEGER REFERENCES files(id),
  version         INTEGER,            -- 0 = original as-found; +1 per write
  commit_id       INTEGER REFERENCES commits(id),  -- NULL for the version-0 baseline
  created_at      TEXT,
  origin          TEXT,               -- scan | auto | manual | revert
  reverted_from   INTEGER,            -- target version restored (origin='revert')
  managed_tags    TEXT,              -- JSON: FULL snapshot of managed tags at this version
  diff            TEXT,              -- JSON: {tag: {from, to}} human-readable change
  note            TEXT,
  PRIMARY KEY (file_id, version)
);
```

> **File identity — resolved in M1.** A file's durable identity is the integer
> surrogate `files.id`, assigned at first scan and anchored by `(folder, filename)`.
> `path` is now a *mutable* attribute split into `folder` + `filename`, so a future
> move/rename (§18) only updates those columns while the `id` — and therefore the
> `tag_revisions` and `path_revisions` history hanging off it — stays continuous.

### Versioning / undo semantics (your requirement)

The mental model is **git**, mapped onto the library:

| git | TagMend |
|---|---|
| working tree | the audio files + the live `files`/`file_tags` snapshot |
| index / staging | `tag_revisions_staged` / `path_revisions_staged` (the desired target) |
| commit | apply staged changes to disk + append revision rows under one `commit_id` |
| revert / checkout | restore to a prior `version`, recorded as a *new* commit (append-only) |
| log / diff | `history(file)` + the `diff` column |

It's cleaner than git in one way: the working files aren't mutated until **commit**, so
staging = "the plan" and commit = "apply + record." Concretely:

- **Stage.** A proposed change lands in `*_staged` (one pending change per file — a
  re-stage replaces it). Nothing on disk changes. The **version-0 baseline is captured
  here** (see below), but no further history is written until commit.
- **Commit.** Create a `commits` row (`status='applying'`), then per file: write the
  file on disk (idempotent), and in **one DB transaction** append the revision (with this
  `commit_id`) **and** delete the staged row. Flip the commit to `status='applied'` when
  done. A commit groups many files (e.g. an artist cascade, §10, or an organize run, §18)
  so it can be reverted as a unit.
- **Crash recovery (resume-free).** The staged table *is* the journal: anything still in
  `*_staged` was not durably committed. There is **no claim step** — recovery is simply
  *running commit again*: the leftover staged rows are swept into a **new** commit (so a
  batch interrupted mid-way can split across two commit ids), and any commit left
  `applying` is flipped to the terminal `interrupted` status. Every step is idempotent.
- **Version 0** is captured **at stage time** — the original as-found tags, with
  `commit_id` NULL (it precedes any commit). Capturing it when the change is *staged*
  (not at commit) means a crash-then-rescan can never overwrite the snapshot before the
  baseline is frozen. This is the permanent safety baseline.
- **Revert(file_id, target_version)** = read `managed_tags` from the target revision,
  write them back to the file, then **append a new revision** under a fresh
  `origin='revert'` commit with `reverted_from=target_version`. History is append-only —
  you can revert a revert, and you never lose any prior state. **Every revert is a
  commit** (shipped M3.5): even a single-file revert gets its own commit row, so every
  disk mutation appears in `list_commits` and is itself undoable.
- **Revert a whole commit** (`revert_commit(commit_id)`, shipped M3.5) = restore every
  file the commit changed to its pre-commit snapshot, grouped under ONE new
  `origin='revert'` commit whose `commits.reverted_from` records the undone commit. The
  mental model is *checkout-and-recommit*, not history rewriting: nothing after the
  target commit is ever lost. Safety rules: **skip + report** — a file changed again by
  a *later* commit is skipped (`skipped_later_changes`), never silently rolled past;
  the **staging area must be empty** (git's "commit or stash first" — also enforced
  per-file by `revert_tags`); `dry_run` previews the exact per-file plan; crash
  recovery is resume-free (run it again — already-reverted files report as skipped).
- "Managed tags" = a deliberately **narrow** set: `GENRE`, `ARTIST`(careful),
  `ALBUMARTIST`, `MUSICBRAINZ_ARTISTID`. We never touch title/track/art, so
  snapshots stay small and reverts can't damage unrelated metadata.

> **Why not just use git directly?** It would version whole binary files (doubling a
> large library on disk, defeating the surgical managed-tag-only revert), entangle the
> tag and path axes we deliberately split, and assume it owns a tree that other tools
> (Picard, the file manager) also edit. We borrow git's *ideas* — commit-as-batch and
> "a folder exists iff it holds files" — without those costs.

---

## 8. Last.fm integration

Free API key only. Key endpoints:

- **`artist.getCorrection`** → canonical artist name + MBID. *This is the
  artist-name normalization feature* — no need to infer from search.
- **`artist.getTopTags`** → ranked community tags with 0–100 *normalized* `count`
  (top tag = 100). A genre source.
- **`album.getTopTags`** → ranked tags for the album, **also weighted**. Album tags
  diverge usefully from artist tags (e.g. Daft Punk *Random Access Memories* tags as
  disco/funk, not the artist's electronic/house), so v1 **merges** artist + album, it
  does not pick one. (`album.getInfo.tags` is unweighted and weaker — not used.)

> **Tag-source still under evaluation.** Last.fm is the *folksonomy* source; MusicBrainz
> supplies the **canonical genre vocabulary** (see §9). Whether the genre *tags
> themselves* come from Last.fm, MusicBrainz, or both is an open question (§15) being
> decided before M2 implementation.

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

**Full design: `docs/genre-tagging-spec.md`.** Summary:

1. Source genre tags for each file's artist **and** album (weighted folksonomy tags),
   cached and paced.
2. **Match against a controlled vocabulary** — the full **MusicBrainz genre list**
   (2,145 genres, CC0), bundled at `src/tagmend/data/genre_vocabulary.yml` and
   regenerated by `scripts/build_genre_vocabulary.py` from the MusicBrainz data dump.
   Matching is by **fold-key** (lowercase, strip non-alphanumerics) so spelling/spacing
   variants collapse; **aliases** (also from the dump, deduped by fold-key) catch
   synonyms folding can't reach (`rnb` → `r&b`). A tag is kept *iff* it matches the
   vocabulary — that is what drops junk (`2013`, `seen live`, `favourite albums`).
3. The vocabulary's canonical `name` (MusicBrainz lowercase spelling) is what gets
   **written** — never the raw tag spelling. So genres are spelled consistently.
4. Merge artist ∪ album, drop the weak tail (`genre_min_weight`), order by weight, cap
   (`genre_max_count`), and write a **multi-value** `genre` tag through the existing
   revertible commit engine.
5. The **LLM's value** shifts from curating the whole vocabulary to **growing the alias
   list** from unmatched tags and resolving ambiguity (§10).

*(Supersedes the earlier small hand-curated allow-list + weight-threshold sketch.)*

---

## 10. The review workflow (auto vs human/LLM)

> **Shipped reality (M2):** for the *genre* axis the workflow engine is
> `file_genre_status` (§7.2) — `resolve_genres` auto-stages what it can resolve, flags
> `no_match` for what Last.fm doesn't know, and honors a sticky `manual` exclusion
> set via `set_genre_status`. The artist-*name* review loop sketched below
> (`needs_review` → LLM picks canonical name → cascade) is **M4** and will get its
> own artist-level state when built. The diagram is the M4 target, not current code.

`files.status` / the artist-level review state *is* the workflow engine.

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

## 12. MCP tool surface

The tags side organizes into a **symmetric family** mirroring git, so a future paths
side (§18) reads identically: `stage_/unstage_/diff_/commit_/history_/revert_` × the
domain (`tags` | `paths`), plus domain-neutral discovery (`list_files`, `get_file`) and
commit inspection (`list_commits`, `get_commit`).

**Shipped (M0 readiness + M1 read path + M3 write path + M2 genres + M3.5 rollback
& visibility), 18 tools:**

| Tool | Purpose |
|---|---|
| `check_health()` | Readiness probe (M0): settings load, music path reachable, ledger opens; also reports any `interrupted` commit left by a crash. |
| `scan_library(path, mode)` | Walk a folder into the `files`/`file_tags` snapshot; `mode` ∈ incremental/full/presence. (M1.) |
| `get_library_stats()` | Library-wide snapshot counts (total/present/missing/unprocessed, by ext, tag-value total) + per-status genre workflow counts. (M1; genre block M3.5.) |
| `list_files(path?, limit?, genre_status?)` | List tracked files with their current managed tags + genre workflow status — discovers `file_id`s, and `genre_status="no_match"` is the fix-by-hand worklist (rows carry the `source_artist`/`source_album` the lookup used). (M3; filter M3.5.) |
| `get_file(file_id)` | One tracked file with its current managed tags. (M3.) |
| `stage_tags(file_id, tags, note?)` | Stage a managed-tag target (git's index); captures the v0 baseline; writes nothing to disk. (M3.) |
| `unstage_tags(file_id)` | Drop a pending staged change. (M3.) |
| `diff_tags(path?)` | Show staged-but-uncommitted changes enriched with the current→target diff (`git diff --staged`). (M3.) |
| `commit_tags(message?, path?)` | Apply all (or a subtree of) staged changes to disk as one revertible commit; append revisions. (M3.) |
| `history_tags(file_id)` | The append-only revision log + diffs for a file. (M3.) |
| `revert_tags(file_id, version, note?)` | Restore a file's managed tags to a prior version, recorded under its own single-file `origin='revert'` commit (append-only; refused while the file has a staged change). (M3; own commit M3.5.) |
| `revert_commit(commit_id, note?, dry_run?)` | Undo an entire commit as a unit: every file back to its pre-commit tags under ONE new `origin='revert'` commit with `reverted_from` set. Files changed by later commits are skipped + reported; requires an empty staging area; `dry_run` previews the per-file plan. (M3.5.) |
| `list_commits(limit?)` | List commits newest first (the revertible units); status applied/applying/interrupted. (M3.) |
| `get_commit(commit_id)` | One commit row by id. (M3.) |
| `resolve_genres(artist?, album?, file_ids?, limit?)` | Query Last.fm (cached/paced), resolve through the vocabulary (§9), and stage the result with `origin="auto"` — only `genre` is replaced. Flags unresolvable files `no_match`. (M2.) |
| `list_artists()` | Distinct `artist` tag values with file counts — the scoping aid for `resolve_genres`. (M2.) |
| `set_genre_status(status, file_ids?, artist?)` | Mark files `manual` (sticky-exclude from genre tagging — "I'll handle these by hand") or `pending` (re-queue). (M2.) |
| `reset_genre_status(file_ids?, artist?)` | Clear any genre status row (`no_match` *and* `manual`), returning files to pending. (M2.) |

> The MCP layer is intentionally `origin`-free for the tag tools: every direct MCP
> `stage_tags` is `manual`; `resolve_genres` stages with `origin="auto"` internally.

**Artist-name normalization + review loop (M4) — not yet built:**

| Tool | Purpose |
|---|---|
| `resolve_artists()` | Query `artist.getCorrection` (cached/paced), classify auto vs needs_review. |
| `list_pending_review()` | Artists/files needing a human/LLM decision. |
| `get_artist_candidate(name)` | Full Last.fm context for one artist (tags, correction, similar). |
| `approve_mapping(input_name, canonical_name)` | Record an approved artist-level decision. |
| `commit_artist(input_name)` | Stage the approved tags for all that artist's files, then `commit_tags` them. |
| `review_stats()` | Review-workflow progress (pending/auto/applied/error counts). |

**Organize / paths family (opt-in, §18) — added once M6 lands:** the `*_paths` mirror of
the tags family, one-for-one. `stage_paths(path)` computes destination paths from the
target scheme and **stages** them (`stage_paths_batch` for a whole run, `unstage_paths` to
drop one); `commit_paths` applies the moves; `diff_paths` / `history_paths` / `revert_paths`
round it out. Because the path domain is the same `RevisionDomain` seam, tags and paths
revert **independently**.

Each tool mirrors a 1:1 engine operation; CLI subcommands are added selectively (the CLI
surface is being chosen *after* the MCP set proves out in practice).

---

## 13. Repo structure

```
music-tag-mender/             # GitHub repo slug (SEO)
├── PLAN.md                    # this file
├── README.md                  # H1: "TagMend — a music tag mender"
├── docs/
│   └── genre-tagging-spec.md  # full genre sourcing/vocabulary/matching design (§9)
├── scripts/
│   └── build_genre_vocabulary.py  # regen genre_vocabulary.yml from the MusicBrainz dump
├── pyproject.toml             # [project] name = "tagmend"
├── src/tagmend/               # importable package = tagmend
│   ├── __init__.py
│   ├── log.py               # one shared logger factory — used everywhere (§20)
│   ├── config.py            # settings.json in OS config dir via platformdirs (§19)
│   ├── engine/
│   │   ├── db.py             # SQLite connection (WAL); schema added per-feature
│   │   ├── schema.py         # DDL for all tables + PRAGMA user_version (v12)
│   │   ├── health.py         # check_health: settings + music + db + interrupted-commit (M0/M3)
│   │   ├── scan.py           # filesystem discovery + signatures (size/mtime)
│   │   ├── store.py          # pure data access for files/file_tags + tag_revisions[_staged] (M1/M3)
│   │   ├── library.py        # scan orchestration (3 modes) + stats + list_files/get_file (M1/M3)
│   │   ├── lastfm.py         # client: artist/album getTopTags, cache, pacing (M2; getCorrection lands at M4)
│   │   ├── classify.py       # vocab + overlay loader → fold-key index; classify.classify_genres, pure (M2)
│   │   ├── genres.py         # resolve_genres orchestration + file_genre_status workflow (M2)
│   │   ├── tags.py           # mutagen read (M1) / write (M3) of the managed tag set
│   │   ├── versioning.py     # tag-revision baseline/append + revert + history (M3)
│   │   ├── commits.py        # domain-neutral commit core: commits table + RevisionDomain
│   │   │                     #   seam + the shared crash-safe run_commit loop (M3)
│   │   ├── staging.py        # tags domain (TagDomain) + stage/diff/commit_tags orchestration (M3)
│   │   └── paths.py          # opt-in paths domain + path_revisions (§18) — STUB (M6)
│   ├── data/
│   │   └── genre_vocabulary.yml  # MusicBrainz genres + aliases (generated; shipped as package data)
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
  CLI wired together, and a working `check_health`/`check-health` that proves the music
  path is reachable. Dry-run only, nothing writes.
- **M1 — Read path (shipped).** `files` + `file_tags` snapshot (stable integer
  `file_id`, normalized EAV tags); `scan_library` with three modes
  (incremental/full/presence); `get_library_stats`; `tags.py` read via mutagen "easy"
  mode + alias map. CLI `scan-library`/`get-library-stats` + MCP
  `scan_library`/`get_library_stats`. Reads
  files into the ledger only — never writes music files.
- **M2 — Last.fm + genre resolution (genre side shipped).** **Done:** the
  genre-vocabulary foundation — the MusicBrainz-derived controlled vocabulary
  (`genre_vocabulary.yml`, 2,145 genres + 556 aliases), the dump-streaming build
  script, the full design (`docs/genre-tagging-spec.md`) — **plus the whole genre
  pipeline**: `lastfm.py` (cached/paced artist+album top-tags, negative cache),
  `classify.py` (vocab/overlay fold-key index + the pure `classify.classify_genres`),
  `genres.py` (the `resolve_genres` tool feeding the commit core with `origin="auto"`), the
  `file_genre_status` workflow table (§7.2), genre settings (`genre_min_weight`,
  `genre_max_count`, `genre_use_album_tags`, `lastfm_rate_per_sec`,
  `genre_stage_limit`), and 4 MCP tools (`resolve_genres`, `list_artists`,
  `set_genre_status`, `reset_genre_status`). Schema is **v6**. **Moved out:**
  artist-name normalization (`artist.getCorrection`) is now part of **M4** — it is
  the review loop's reason to exist. *Note: M3's write-path core was built ahead of M2.*
- **M3.5 — Revert & visibility gaps (shipped).** Closed the two pre-bulk-run gaps:
  **(a)** `revert_commit(commit_id, note?, dry_run?)` — group-level undo (one
  `origin='revert'` commit, `reverted_from` link, skip+report for later-changed files,
  empty-staging guard, dry-run preview), built on a shared per-file core so the
  single-file `revert_tags` is now literally a group revert of one and **every revert
  is itself a commit**; **(b)** genre-status visibility — `list_files` grew a
  `genre_status` filter (`pending`/`no_match`/`manual`/`staged`/`done`) + per-file
  status fields (with the `source_artist`/`source_album` a `no_match` was computed
  against), and `get_library_stats` a per-status `genre` count block. The derived-status
  logic (`store.derived_genre_status`) mirrors `genres._select` — keep in sync.
- **M3 — Write path + versioning + commit core (core shipped).** The git-like
  stage → commit → history → revert engine: the **domain-neutral commit core**
  (`commits.py`: `commits` table, the `RevisionDomain` seam, the crash-safe
  `run_commit` loop, **resume-free** recovery), the tags domain (`staging.py`:
  `stage_tags`/`unstage_tags`/`diff_tags`/`commit_tags` with v0 baseline captured at
  stage time), atomic mutagen writes, `revert`, `history`, and the full tags MCP family
  + discovery + commit-inspection tools (§12). **Remaining for M4:** the artist-level
  `commit_artist` cascade convenience. **Backups proven before any real run.**
- **M4 — Artist-name normalization + review loop.** `artist.getCorrection` in
  `lastfm.py`, artist-level resolution state, `resolve_artists`,
  `list_pending_review`, `approve_mapping`, `commit_artist` cascade + re-run (§10,
  §12). Must land **before** organize (M6), since canonical names drive folder layout.
- **M5 — Polish.** Genre vocabulary tuning, album-level override, docs, packaging.
- **M6 — Organize (opt-in moves & renames).** Stable `file_id` migration,
  `stage_paths` (dry-run path plan), `commit_paths` (atomic per-item moves),
  `path_revisions` append-only log, `revert_paths`, `history_paths`. Gated behind a
  config flag; **revert proven before any real run** (same bar as M3). See **§18**.

## 15. Open questions

- ~~Genre tag source: Last.fm vs MusicBrainz vs both?~~ **RESOLVED: Last.fm only** (+ the
  bundled MusicBrainz vocabulary as the offline filter). Tested on the user's obscure
  library — MusicBrainz genres were *empty* for half the artists; Last.fm's folksonomy is
  far denser and, once vocab-filtered, clean. MusicBrainz's value is **identity** (MBID /
  disambiguation), used in Phase 2, where Last.fm can be queried by MBID. See
  `docs/genre-tagging-spec.md` §2.3 / §11.
- ~~Genre scope default: artist-level only, or mixed-catalog?~~ **RESOLVED:** merge
  artist + album tags (no precedence); album tags add per-record accuracy. See §9 / spec.
- ~~Multi-value genres — allow N, or force single?~~ **RESOLVED:** multi-value, ordered
  by weight, optionally capped by `genre_max_count`. Navidrome reads multi-value tags.
- ~~Signature: `size+mtime` vs content hash?~~ **RESOLVED (M1):** `size_bytes` +
  `mtime_ns` (`st_mtime_ns`) — fast and NAS-friendly.
- ~~Should `revert` be exposed in the CLI bulk path or MCP-only?~~ **RESOLVED (M3):**
  shipped as the MCP `revert_tags(file_id, version)`; per-file and deliberate. The CLI
  surface (which MCP tools become subcommands) is deferred until the MCP set proves out.
- ~~Tag storage: raw JSON vs normalized?~~ **RESOLVED (M1):** normalized EAV linked
  table (`file_tags`), one row per value, no raw JSON.
- ~~Stable `file_id` scheme?~~ **RESOLVED (M1):** DB-assigned integer surrogate
  (`files.id`), anchored by `(folder, filename)` at first scan. (Re-identifying a file
  moved *outside* the tool is handled later by the move log, not by the id itself.)
- **Organize (§18):** what is the default target naming scheme
  (`Artist/(Year) Album/NN Title.ext`?), and how configurable should it be?
- **Organize (§18):** when a folder rename collapses two artist spellings into one
  destination, how do we handle the merge / collision (refuse, suffix, or merge)?
- **Organize (§18):** do we move non-audio sidecars (cover art, `.nfo`) with the
  album? (~~delete now-empty source folders?~~ **RESOLVED:** yes — prune source dirs
  that become empty after a move, scoped to those dirs and gated by
  `organize.prune_empty_dirs`; no global empty-folder sweep. See §18.3.)

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
| PyPI package + CLI + module | `tagmend` | Short and ergonomic: `pip install tagmend`, `tagmend scan-library …`. PyPI normalizes case, so `TagMend` ≡ `tagmend`. |
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

### 18.2 Data model (DDL already created at schema v6; move *logic* lands at M6)

The path side reuses the **same `commits` + staging machinery as the tag side** (§7):
`path_revisions_staged` is the move plan (git's index), and the shared `commits` table
groups a whole organize run — so there is **no separate `move_plans` table**, and the
old `plan_id` is just `commit_id`. There is also **no `kind` column**: folders are not
first-class. A folder rename is simply N per-file move rows sharing one `commit_id`
(the folder is the common path prefix); rename-vs-move is derivable from `from_path` /
`to_path`; and folders are created on demand and **pruned when empty** (§18.3), which is
how we reproduce git's "a folder exists iff it holds files" for free.

```sql
-- APPEND-ONLY location log. One row per move/rename. Never updated, never deleted.
CREATE TABLE path_revisions (
  file_id       INTEGER REFERENCES files(id),
  version       INTEGER,             -- 0 = path as-found at first scan; +1 per move
  commit_id     INTEGER REFERENCES commits(id),  -- NULL for the version-0 baseline
  created_at    TEXT,
  origin        TEXT,                -- scan | organize | revert
  reverted_from INTEGER,            -- target version restored (origin='revert')
  from_path     TEXT,               -- absolute source path
  to_path       TEXT,               -- absolute destination path
  note          TEXT,
  PRIMARY KEY (file_id, version)
);

-- Staging area (git's index) for moves: one pending move per file; holds the TARGET.
-- Mirrors tag_revisions_staged (§7) — resume-free, so NO commit_id. A commit applies +
-- clears it; a crash leaves leftovers staged for the next commit.
CREATE TABLE path_revisions_staged (
  file_id       INTEGER REFERENCES files(id),
  to_path       TEXT,               -- proposed destination absolute path
  origin        TEXT,               -- organize
  note          TEXT,
  staged_at     TEXT,
  PRIMARY KEY (file_id)
);
```

Because revert is keyed by `file_id`/`version` and grouped by `commit_id`, it can
operate per-file or per-run, and an interrupted run recovers the **resume-free** way (§7):
the next `commit_paths` sweeps the rows still in `path_revisions_staged` into a new commit.

`PathDomain` will be the second `RevisionDomain` (the first is the shipped tags
`TagDomain`), reusing the commit core in `engine/commits.py` unchanged. The move-specific
parts — the disk action, plus three **parked seam questions** (intra-batch move ordering
to avoid clobber, the collision policy of §15, and folder-rename atomicity vs the per-file
commit boundary) — are sketched as a design note in `engine/paths.py`.

### 18.3 Semantics

- **Plan first.** `stage_paths(path)` computes destination paths from the target
  scheme + the (already cleaned) tags, detects collisions, and **stages** them into
  `path_revisions_staged`. Nothing on disk changes.
- **Commit atomically.** `commit_paths()` opens a `commits` row, then per item:
  ensures the destination folder exists (`mkdir -p`), moves the file with a temp +
  atomic-rename where the OS/filesystem allows (NAS-safe), appends a `path_revisions`
  row under the run's `commit_id`, and clears the staged row. Non-audio sidecars (art,
  `.nfo`) move with their album by default (configurable).
- **Folders emerge; pruning is by emptiness, not by tracking creates.** After a move,
  a source directory that is now empty is removed (walking upward), gated by
  `organize.prune_empty_dirs`. We **scope pruning to the source dirs of the move** — we
  never sweep the whole library for empty folders — and `rmdir` naturally refuses on a
  non-empty dir, so a folder still holding art/junk is left alone.
- **Revert.** `revert_paths(file_id, version)` `mkdir -p`s the target path, moves the
  file back, prunes any now-empty source dir, and appends a new `origin='revert'` row —
  same append-only model as tag reverts. Reverting a whole `commit_id` undoes a run.
- **This is why we need no `kind` and no folder-create records:** reverting a move that
  *created* folder `B` removes `B` simply because the move-back leaves it empty — and if
  you had meanwhile dropped another file into `B`, it is *not* empty, so it (and your
  file) survive. Emptiness is a safer signal than a tracked "created" flag.

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

---

## 22. Database naming conventions

Applied consistently across every table the engine creates, so the schema reads
predictably as it grows milestone by milestone:

- **Tables:** plural `snake_case` (`files`, `file_tags`, `artist_cache`).
- **Surrogate primary key:** a bare `id` (`INTEGER PRIMARY KEY`) where a table needs a
  stable internal identity (e.g. `files.id`).
- **Foreign keys:** `<singular>_id` referencing the parent's `id` (e.g. `file_id`
  references `files(id)`); declare `ON DELETE CASCADE` where children are owned.
- **Caches:** keep the `_cache` suffix (`artist_cache`, `lastfm_cache`).
- **Timestamps:** ISO-8601 **UTC** stored as `TEXT`, with an `_at` suffix
  (`first_seen_at`, `updated_at`, `tags_updated_at`). Produced via
  `datetime.now(UTC).isoformat()` — never a naive datetime.
- **Booleans:** `INTEGER` 0/1 with an `is_` prefix (`is_missing`).
- **Append-only logs:** `_revisions` suffix (`tag_revisions`, `path_revisions`) with a
  composite PK `(file_id, version)`; **version 0 = baseline**, never updated/deleted.
- **Commits (the batch grouping):** a `commits` table whose `id` is referenced as
  `commit_id` on the revision rows it groups (git's commit). One commit = one organize
  run or one artist cascade, so it reverts as a unit; baselines have `commit_id` NULL.
  `status` is `applying` → `applied`, or the terminal `interrupted` a later commit stamps
  onto a crashed run.
- **Staging area:** `_staged` suffix on a mirror of each revision log
  (`tag_revisions_staged`, `path_revisions_staged`), PK `file_id` (one pending change
  per file), holding the desired target until committed. **Resume-free (schema v5):**
  staged rows carry **no `commit_id`** — there is no claiming. This is the *only* place
  rows are mutated/deleted — committing moves a row into the append-only log and clears
  it; a crash just leaves the row staged for the next commit to sweep up.
- **SQL safety:** all values bound via `?` placeholders (never string-formatted —
  bandit S608); identifier-only interpolation (column lists) is the sole exception and
  carries a `# noqa: S608` with no untrusted input.

## 23. CLI & MCP tool conventions

The three layers (core engine ↔ CLI ↔ MCP) map **1:1** onto the same operation, and a
shared enum (`ScanMode`) is used identically across all three so behavior can't drift:

> The authoritative naming grammar (the closed verb set, the shape table, and the
> finding-noun glossary) lives in **`CLAUDE.md` § Tool naming**. This section covers only
> how the three layers line up.

- **Engine functions:** `snake_case`, **verb-first**, descriptive
  (`scan_library`, `get_library_stats`, `read_tags`). All logic lives here; the frontends
  are thin marshalling wrappers.
- **CLI subcommands:** the MCP tool name with `-` for `_`, no aliases (`check-health`,
  `scan-library`, `get-library-stats`, `detect-mismatches`).
- **MCP tool names:** `verb_object` (`scan_library`, `get_library_stats`,
  `check_health`) for LLM clarity, since the tool name is part of the model-facing UX
  and benefits from being self-describing.
- **The 1:1 map (M1 example):** core `library.scan_library` ↔ CLI `scan-library` ↔ MCP
  `scan_library`; core `library.get_library_stats` ↔ CLI `get-library-stats` ↔ MCP
  `get_library_stats`.
- **Shared enums:** `ScanMode` (`incremental`/`full`/`presence`) is the single source
  of truth; the CLI takes it directly as a Typer option and the MCP tool accepts the
  equivalent `Literal[...]` string and maps it onto the same enum.
- **Errors:** the engine raises `ValueError` for caller/config problems; the CLI
  catches it, echoes the message, and exits non-zero; the MCP tool catches it and
  returns `{"ok": False, "error": ...}` (never raising across the JSON-RPC boundary).
