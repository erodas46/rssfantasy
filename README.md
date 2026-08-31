# TeamChile NFL News Feed

Pulls fantasy-relevant NFL news from RSS (Rotowire + ESPN), matches each item
to a player, flags items where a named insider (e.g. Adam Schefter) is cited
as the source, and adds three structured sections — Sleeper waiver-wire
trends, an official injury report, and NFL transactions — all rendered into
one cumulative Markdown file you upload to the TeamChile Claude Project
weekly:

`Date | Player | Pos/Team | Category | Insider | Summary | Source`

plus **Sleeper waiver-wire trends** (top adds/drops, last 24h), an
**injury report** with status-change detection, and transactions folded
into the main table (source: `ESPN Transactions`).

## Why there's no direct "Sleeper" or "Schefter" feed

Worth knowing before you go looking for one:

- **Sleeper doesn't publish its own news.** The news shown in the Sleeper
  app is licensed from RotoBaller, and RotoBaller only sells RSS/API access
  to business partners — no free public feed to point at. What Sleeper
  *does* expose for free is real add/drop activity across the platform
  (`/players/nfl/trending/{add,drop}`) and each player's official
  `injury_status`, both used below.
- **Adam Schefter has no personal RSS feed.** ESPN doesn't publish
  per-author feeds, and Twitter/X killed free RSS access years ago. His
  scoops still reach you indirectly, though — Rotowire/ESPN blurbs cite him
  by name when he breaks something ("...Adam Schefter of ESPN reports").
  The **Insider** column flags exactly those items. Add more names to
  `INSIDER_WATCHLIST` in the script (e.g. `"Rapoport"`, `"Pelissero"`) the
  same way.

## Why injuries and transactions aren't scraped from ESPN/FantasyPros pages

Both constraints you gave — low maintenance, and reliable in-season use —
pushed away from scraping HTML:

- **Injury report** uses Sleeper's `injury_status` field on each player,
  which is already fetched and cached in this pipeline (same call used for
  the broad player matcher and trending section). It's the same official
  NFL injury report ESPN's page shows, just already-structured JSON instead
  of a page layout that can change without notice. The report also tracks
  **status changes run-over-run** — a fresh Questionable→Out shift is the
  actionable signal during the season, more so than a static snapshot.
- **Transactions** uses ESPN's own undocumented core API
  (`sports.core.api.espn.com/.../nfl/transactions`) rather than scraping
  FantasyPros' transactions page. It's not officially documented, but it's
  still a JSON contract rather than page HTML, so it's less likely to break
  silently on a redesign than a scraper would. **Caveat:** its exact field
  names couldn't be independently verified before shipping this — see
  "Known limitations" below for what to check on the first live run.

## Setup (10 minutes, no server needed)

1. **Create a new GitHub repo** (public or private — private is fine and free).
2. **Add these files** to the repo root, keeping the folder structure:
   - `fetch_nfl_news.py`
   - `players.json`
   - `requirements.txt`
   - `.github/workflows/nfl-news.yml`
3. Push to the `main` branch. GitHub Actions picks up the workflow
   automatically — no secrets or config needed, since it only writes back to
   its own repo (and only reads public APIs).
4. **First run:** go to the *Actions* tab → *NFL News Feed* → *Run workflow*
   to trigger it manually instead of waiting for the next cron tick. Confirm
   `nfl-news-feed-2026.md`, `seen_items.json`, `sleeper_players_cache.json`,
   and `injury_status_snapshot.json` appear in the repo afterward (~20
   seconds — the Sleeper player directory fetch is the slow part on a cold
   cache).
5. **Check the transactions section specifically** after that first run —
   open the log and look for lines starting `WARN: ESPN transactions` or
   `WARN: fetched N ESPN transaction item(s) but parsed 0`. If you see
   either, the endpoint's real field names differ from what the script
   assumes; paste a sample raw item back and the parser is a quick fix.
6. After that it runs unattended 3x/day per the schedule in the workflow file.

**Updating from an earlier version:** just replace `fetch_nfl_news.py` and
add `players.json` if you don't have it yet. Leave `seen_items.json` alone —
the script detects items captured before player-matching/insider-detection
existed and backfills them automatically on the next run.

## Weekly routine

Once a week, open `nfl-news-feed-2026.md` in the repo, copy or download it,
and upload it to the TeamChile Claude Project (replacing the prior week's
version is fine — the file is cumulative, so nothing is lost).

## Files

| File | What it is |
|---|---|
| `fetch_nfl_news.py` | The script. Fetches RSS + ESPN transactions + Sleeper trending/injuries, dedupes, categorizes, matches players, flags insiders, renders everything. |
| `players.json` | Name → position/team lookup (3,279 players), built from `sleeper_adp_20260803.csv`. Primary source for the Player/Pos-Team columns — curated to your league's fantasy-relevant player pool. |
| `requirements.txt` | Just `feedparser`. Everything else uses the standard library. |
| `.github/workflows/nfl-news.yml` | The cron trigger + commit-back step. |
| `seen_items.json` | **Generated.** Full history of every news/transaction item ever captured — the source of truth. Don't delete it, or you'll get duplicate re-adds next run. |
| `sleeper_players_cache.json` | **Generated.** Cached copy of Sleeper's full player directory (offense + IDP/DEF + injury_status), refreshed at most every 6h. Feeds the broad player matcher, trending section, and injury report. Safe to delete — it'll just refetch. |
| `injury_status_snapshot.json` | **Generated.** Last-seen injury status per player, used to detect changes run-over-run. Don't delete it, or you'll lose the diff baseline (everything will show as "newly listed" on the next run instead of showing real changes). |
| `nfl-news-feed-2026.md` | **Generated.** The human/Claude-readable rendered view — this is what you upload. |

## How player matching works

Three tiers, tried in order, first hit wins:

1. **Full name in `players.json`** — your league's actual player pool. Most
   reliable, and Pos/Team reflects a real ADP source.
2. **Full name in Sleeper's complete player directory** (~11k names,
   offense + IDP/DEF). Catches players outside your ADP file — individual
   defenders in trade/injury news being the most common case, since this
   league doesn't roster them and they're absent from the offense-focused
   ADP list `players.json` is built from.
3. **Unique last name in `players.json` only** — used only when exactly one
   player in that file shares the surname. Deliberately *not* extended to
   the ~11k-name Sleeper directory: at that size, common surnames collide
   often enough that a last-name fallback would misattribute more often
   than it'd help (confirmed in testing — "Colts sign veteran RB..." briefly
   misfired onto the Colts' own team defense before DEF entries were
   excluded from tier 3).

A `—` in Player/Pos-Team means no confident match at any tier — check the
summary text itself, don't assume "no player involved."

## Tuning it

- **Change frequency/times:** edit the `cron:` lines in the workflow file.
  Cron is UTC; the defaults land near 7am / 1pm / 8pm ET.
- **Add another RSS source:** append an entry to the `FEEDS` list.
  `fantasy_focused: True` only for feeds that are already fantasy-specific
  (like Rotowire) — general news feeds should stay `False` so the keyword
  filter applies and you don't get flooded with non-fantasy stories.
- **Track more insiders:** add surnames to `INSIDER_WATCHLIST`.
- **Adjust categorization:** edit the keyword lists in `CATEGORY_KEYWORDS`.
  Substring matching — good enough to sort the firehose, not a substitute
  for reading the actual item.
- **Staleness window:** `STALE_AFTER_DAYS` (default 14) controls the
  ⚠️STALE flag. **Trending window:** `lookback_hours`/`limit` in
  `get_sleeper_trending()` calls (default 24h, top 15).
- **Injury/directory freshness:** `SLEEPER_PLAYERS_CACHE_MAX_AGE_HOURS`
  (default 6h, so all 3 scheduled runs get fresh data) — raise it if you'd
  rather reduce call volume than get same-day status changes.
- **Refresh `players.json`** periodically from a newer ADP export using the
  same columns (`PLAYER,POS,TEAM`) if match quality drops later in the
  season — it's a snapshot from Aug 3, 2026 and won't know about players
  signed/promoted after that.

## Known limitations

- **ESPN transactions endpoint is unverified.** It's undocumented, and its
  exact JSON field names weren't confirmed against a live response before
  this shipped (network-restricted dev environment). The parser tries
  several plausible field names (`description`/`text`/`shortText` for the
  body, a few date variants, an embedded team dict) and logs a clear `WARN:`
  instead of crashing if none match — but "logs a warning" isn't the same
  as "definitely works." Check the Actions log after the first live run.
- **Injury/trending/matching freshness is capped by the 6h directory
  cache**, which is itself downstream of however often Sleeper updates
  `injury_status` (their docs say within 24h during the season). Don't treat
  the injury report as second-by-second — for that, the source of record is
  still the NFL's own inactive list close to kickoff.
- Categorization and insider detection are keyword/substring matching, not
  NLP — treat them as a sort aid, not ground truth. Spot-check anything
  that matters for a roster decision.
- **Rotowire's free RSS feed omits the paid "analysis" line** — you get the
  factual blurb only, which is what gets kept here.
- **Any of the three external calls (RSS, Sleeper, ESPN transactions) can
  fail independently** without breaking the others — each is wrapped so a
  bad run logs a warning and skips just that section, rather than failing
  the whole job.
- **Scheduled GitHub Actions can slip** a few minutes during high platform
  load — not a concern for a 3x/day cadence, just don't rely on it for
  second-precision timing.
