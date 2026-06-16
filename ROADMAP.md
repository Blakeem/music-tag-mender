# TagMend — ROADMAP (gaps to close before the full-library run)

> Working checklist of what's **still missing**, written 2026-06-15 after auditing the
> code against `PLAN.md`. Ordered by what blocks the first real run against the full
> library. `PLAN.md` is the design of record; this file is the punch-list.
>
> **Update 2026-06-15 (Maphra investigation):** the `Maphra` case was investigated
> against the **live** Last.fm API and **already auto-tags correctly** — see Gap 2.
> An earlier draft of this file wrongly claimed it lands in `no_match`; that was an
> untested guess and is corrected below.

## Where we are (verified against the code)

Shipped & tested: **M0** readiness, **M1** read path, **M3** write path + git-like
commit/version/revert core, **M3.5** `revert_commit` + genre-status visibility, **M2**
genre pipeline (Last.fm → vocabulary → stage). Schema **v6**. **18 MCP tools.**
`lastfm_api_key` **is** configured. Managed tag set: `genre`, `artist`, `albumartist`,
`musicbrainz_artistid`.

The safety story (stage → commit → revert, append-only history, v0 baseline) is real
and is the strongest part of the tool. The gaps below are about **coverage and
ergonomics for the bulk run**, not the core engine.

---

## Gap 1 — Artist-name normalization + review loop (M4) — *the big one*

This is the half of the original mission (`PLAN.md §2.2`) that isn't built yet, and
it's what collapses `Miami Nights '84` / `Miami_Nights(1984)` into one artist. Nothing
here exists in code today.

- [ ] `lastfm.artist_correction(name)` — wrap `artist.getCorrection` (cached/paced),
      returns canonical name + MBID. (Currently `lastfm.py` has *only* top-tags.)
- [ ] Artist-level resolution state (the `artist_cache` table `PLAN.md §7.2` parked) —
      auto vs `needs_review`, the approved canonical name.
- [ ] MCP `resolve_artists()` — classify every distinct artist auto vs needs_review.
- [ ] MCP `list_pending_review()` — the artists awaiting a human/LLM decision.
- [ ] MCP `get_artist_candidate(name)` — full Last.fm context for one artist.
- [ ] MCP `approve_mapping(input_name, canonical_name)` — record the decision.
- [ ] MCP `commit_artist(input_name)` — cascade the approved name to every file of that
      artist, then `commit_tags`. (The "fix primary data, re-run for that artist" loop.)
- [ ] MCP `review_stats()` — pending/auto/applied/error progress.

Must land **before** organize (M6), since canonical names drive folder layout.

---

## Gap 2 — The `Maphra` case — **RESOLVED: no gap, it already works**

`Maphra` (YouTube vocal covers) was the out-of-the-box test: `artist=MAPHRA`,
`genre=Music` (junk), **no album tag**, real source artist buried in the title.
Investigated end-to-end against the **live** API + real settings on 2026-06-15:

- **Last.fm has her.** `artist.getTopTags("MAPHRA")` → `metal(100), alternative
  metal(69), metalcore(46), rock(7), screamo, vocal art, alternative, female
  vocalist`. Case-insensitive; `getCorrection` confirms canonical `Maphra`.
- **The `albumartist`→`artist` fallback already exists** (`genres.py:74-81`): with no
  `albumartist`, lookup uses `artist=MAPHRA`. This is exactly the fallback the question
  asked for — it's already there, no change needed.
- **No album is handled gracefully** — `album.getTopTags` is optional enrichment, only
  called when an album exists; artist tags alone are used otherwise.
- **Full `stage_genres` run** (8 files, throwaway DB): **8 staged, 0 no_match**, junk
  `Music` replaced by `['metal', 'alternative metal', 'metalcore', 'rock']`, `artist`
  preserved.

So **nothing needs fixing for auto-tagging Maphra.** The earlier "lands in `no_match`"
claim was an untested guess. The real-world "doesn't show" impression most likely came
from **staging ≠ disk**: `stage_genres` writes the *plan* to the ledger; the file on
disk only changes after `commit_tags`. Confirming that commit→disk→revert round-trip is
exactly the smoke test in Gap 5.

**Remaining (genuine but low-priority) edge — the manual path:**
This only matters for files Last.fm genuinely *can't* resolve, which Maphra is **not**.
There are currently **zero** such files in the 487-file library (all have an artist).

- [ ] No-identity worklist: files with no `artist` AND no `albumartist` are silently
      skipped (`skipped_no_artist`) and bucket as plain `pending` with no worklist
      (`store.py:651`). Give them a first-class state so they're visible. *Defensive;
      no such files exist today.*
- [ ] Manual tagging path for a genuine `no_match` (artist not on Last.fm): the escape
      hatch already exists per-file via `stage_tags` + `commit_tags`; a bulk
      artist/folder-scoped manual stage would be a convenience. *Defer until a real
      `no_match` actually appears in the library.*
- Known limitation (document, don't fix): when the true artist is in the *title*
  (`"Bad Omens - Impose (MAPHRA Vocal Cover)"`), genre still comes from `MAPHRA`'s own
  tags — which is *correct* here (her covers are metalcore). No action.

---

## Gap 3 — CLI surface for the unattended bulk run — **DEFERRED to the very end**

**Decision (2026-06-15):** all tools are designed **MCP-first**; the bulk run is driven
through the MCP client (Claude). CLI integration is deliberately left until *after*
everything else is complete, when it's an easy call which tools to expose. Recorded here
only so the eventual pass has a starting list:

- [ ] `tagmend stage-genres [--artist … | --limit …]`
- [ ] `tagmend diff` / `tagmend commit [-m …]`
- [ ] `tagmend revert <file_id> <version>` / `tagmend revert-commit <id> [--dry-run]`
- [ ] `tagmend list [--genre-status no_match]` (the fix-by-hand worklist)

---

## Gap 4 — Live Last.fm readiness

The live API now **works** — the Maphra investigation (2026-06-15) made real
`artist.getTopTags` and `artist.getCorrection` calls successfully with the configured
key, and `stage_genres` resolved real genres. What's still missing is making that a
*repeatable readiness check* rather than a one-off:

- [ ] Add a Last.fm connectivity check to `doctor` (one cheap `artist.getTopTags` ping;
      verifies the key works + the network path before a long run). `doctor.py` checks
      settings, music folder, and ledger only — not Last.fm.

---

## Gap 5 — Pre-run safety: prove the round-trip on real files first

`PLAN.md` says "backups proven before any real run." The v0 baseline is the backup —
but it only snapshots the **managed** tags, and no real-API write/commit/revert cycle
has been run on actual audio yet.

- [ ] Full round-trip on a copy of one real album: scan → stage_genres → commit →
      verify on disk (mutagen) → `revert_commit` → verify restored. Across all four
      formats if convenient (mp3/flac/m4a/ogg).
- [ ] Recommend a filesystem-level copy of `music/` before the first full run (cheap
      insurance beyond the managed-tag baseline).

---

## Deferred (not blocking the genre run)

- **M5 — Polish:** genre vocabulary tuning, album-level override, README, packaging.
- **M6 — Organize (opt-in moves/renames):** DDL ships in v6; `moves.py` is a stub +
  paper sketch (`PLAN.md §18`). Needs M4's canonical names first. The dirty `Miami
  Nights` folders are the intended demo.

---

## Suggested order (matches the agreed plan)

1. ~~**Fix Maphra tagging.**~~ **Done — no fix needed** (Gap 2). Auto-tagging works
   end-to-end; the album-artist→artist fallback and no-album handling already exist.
2. **Gap 4 + Gap 5 — smoke tests against the real files.** Prove the live API + the
   full `stage → commit → (verify on disk) → revert` round-trip on a copy of real
   audio, including the `Maphra` mp3s (multi-value genre write, junk→clean). This is
   the next step and de-risks everything else.
3. **Gap 1 (M4) — artist-name normalization + review loop** (the `claude-code-feature-
   workflow` experiment is intended for this larger chunk).
4. **Low-priority / deferred:** Gap 2 no-identity worklist + bulk manual stage (no such
   files exist yet), then **M5 / M6**.

> **CLI (Gap 3) is intentionally deferred to the very end** — all tools are being
> designed MCP-first; CLI integration is a final, easy decision once everything else is
> complete. Gap 3 below is kept only as a record of what that pass would cover.
