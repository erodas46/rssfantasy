# TeamChile NFL News Feed

Pulls fantasy-relevant NFL news from RSS (Rotowire + ESPN) three times a day,
dedupes it, tags each item (INJURY / TRANSACTION / ROLE-DEPTH CHART /
SUSPENSION), and keeps a single cumulative Markdown file you upload to the
TeamChile Claude Project weekly.

## Setup (10 minutes, no server needed)

1. **Create a new GitHub repo** (public or private — private is fine and free).
2. **Add these files** to the repo root, keeping the folder structure:
   - `fetch_nfl_news.py`
   - `requirements.txt`
   - `.github/workflows/nfl-news.yml`
3. Push to the `main` branch. GitHub Actions picks up the workflow
   automatically — no secrets or config needed, since it only writes back to
   its own repo.
4. **First run:** go to the *Actions* tab → *NFL News Feed* → *Run workflow*
   to trigger it manually instead of waiting for the next cron tick. Confirm
   `nfl-news-feed-2026.md` and `seen_items.json` appear in the repo after it
   finishes (~15 seconds).
5. After that it runs unattended 3x/day per the schedule in the workflow file.

## Weekly routine

Once a week, open `nfl-news-feed-2026.md` in the repo, copy or download it,
and upload it to the TeamChile Claude Project (replacing the prior week's
version is fine — the file is cumulative, so nothing is lost).

## Files

| File | What it is |
|---|---|
| `fetch_nfl_news.py` | The script. Fetches feeds, dedupes, categorizes, renders the MD. |
| `requirements.txt` | Just `feedparser`. |
| `.github/workflows/nfl-news.yml` | The cron trigger + commit-back step. |
| `seen_items.json` | **Generated.** Full history of every item ever captured — the source of truth. Don't delete it, or you'll get duplicate re-adds next run. |
| `nfl-news-feed-2026.md` | **Generated.** The human/Claude-readable rendered view — this is what you upload. |

## Tuning it

- **Change frequency/times:** edit the `cron:` lines in the workflow file.
  Cron is UTC; the defaults land near 7am / 1pm / 8pm ET.
- **Add another source:** append an entry to the `FEEDS` list at the top of
  `fetch_nfl_news.py`. Set `fantasy_focused: True` only for feeds that are
  already fantasy-specific (like Rotowire) — general news feeds should stay
  `False` so the keyword filter applies and you don't get flooded with
  non-fantasy stories.
- **Adjust categorization:** edit the keyword lists in `CATEGORY_KEYWORDS`.
  It's intentionally simple (substring matching) — good enough to sort the
  firehose, not a substitute for reading the actual item.
- **Staleness window:** `STALE_AFTER_DAYS` (default 14) controls when an item
  gets the ⚠️ STALE flag in the render.

## Known limitations

- **No player/position/team columns.** Reliable name-matching would need a
  roster database; the headline/summary text almost always has the name
  already, so v1 just keeps the raw text instead of guessing.
- **Rotowire's free RSS feed omits the paid "analysis" line** — you get the
  factual blurb only, which is what gets kept here.
- **Scheduled GitHub Actions can slip** a few minutes during high platform
  load — not a concern for a 3x/day cadence, just don't rely on it for
  second-precision timing.
- Categorization is keyword-based, not NLP — treat category tags as a sort
  aid, not ground truth. Spot-check anything that matters for a roster
  decision.
