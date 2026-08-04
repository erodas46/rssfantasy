#!/usr/bin/env python3
"""
TeamChile NFL News Feed
------------------------
Pulls fantasy-relevant NFL news from RSS feeds, dedupes against everything
seen before, and renders a cumulative Markdown file for weekly upload to
the TeamChile Claude Project.

Source of truth : seen_items.json  (every item ever captured, never pruned)
Rendered view    : nfl-news-feed-2026.md  (fully regenerated every run)

Run manually:
    pip install feedparser
    python3 fetch_nfl_news.py

Runs automatically via .github/workflows/nfl-news.yml on a cron schedule.
"""

import json
import hashlib
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FEEDS = [
    {"name": "Rotowire", "url": "https://www.rotowire.com/rss/news.php?sport=NFL", "fantasy_focused": True},
    {"name": "ESPN",     "url": "https://www.espn.com/espn/rss/nfl/news",          "fantasy_focused": False},
    # Add more feeds here. Set fantasy_focused=False for general news feeds
    # so they get keyword-filtered before being kept; feeds that are already
    # fantasy-specific (like Rotowire) pass through untouched.
]

SEEN_FILE = Path("seen_items.json")
OUTPUT_FILE = Path("nfl-news-feed-2026.md")
STALE_AFTER_DAYS = 14      # items older than this get flagged in the render
NEW_SECTION_DAYS = 7       # window for the "new this week" section at the top

CATEGORY_KEYWORDS = {
    "INJURY": [
        "injury", "injured", "questionable", "doubtful", "out for", "ruled out",
        "injured reserve", " ir ", "concussion", "hamstring", "acl", "mcl",
        "surgery", "fracture", "sprain", "soreness", "activated", "return from",
        "limited", "did not practice", "dnp", "carted off", "left the game",
    ],
    "TRANSACTION": [
        "signed", "signs", "released", "waived", "cut by", "claimed off waivers",
        "trade", "traded", "acquired", "re-signed", "agrees to", "extension",
        "free agent", "promoted from", "elevated", "placed on", "activated from",
    ],
    "ROLE/DEPTH CHART": [
        "starter", "starting", "depth chart", "snap count", "snaps", "workload",
        "benched", "demoted", "first-team reps", "no. 1 receiver", "lead back",
        "committee", "target share",
    ],
    "SUSPENSION": [
        "suspended", "suspension", "banned",
    ],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def categorize(text: str) -> str:
    lowered = text.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            return category
    return "NEWS"


def make_id(entry) -> str:
    key = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def parse_date(entry) -> str:
    """Best-effort ISO date (YYYY-MM-DD) for an entry."""
    for field in ("published_parsed", "updated_parsed"):
        val = entry.get(field)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc).date().isoformat()
    return datetime.now(timezone.utc).date().isoformat()


def clean_summary(raw: str, limit: int = 280) -> str:
    text = re.sub("<[^<]+?>", "", raw or "")  # strip HTML tags
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace(
        "ANALYSIS Subscribe now to instantly reveal our take on this news.", ""
    ).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "..."
    return text


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Fetch + merge
# ---------------------------------------------------------------------------

def fetch_new_items(seen: dict) -> int:
    added = 0
    for feed_cfg in FEEDS:
        parsed = feedparser.parse(feed_cfg["url"])
        for entry in parsed.entries:
            item_id = make_id(entry)
            if item_id in seen:
                continue

            title = entry.get("title", "").strip()
            summary = clean_summary(entry.get("summary", entry.get("description", "")))
            category = categorize(f"{title} {summary}")

            if not feed_cfg["fantasy_focused"] and category == "NEWS":
                # General feed + no fantasy-relevant keyword hit -> skip.
                continue

            seen[item_id] = {
                "date": parse_date(entry),
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "source": feed_cfg["name"],
                "category": category,
                "title": title,
                "summary": summary,
                "link": entry.get("link", ""),
            }
            added += 1
    return added


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def _row(r: dict, today) -> str:
    age_days = (today - datetime.fromisoformat(r["date"]).date()).days
    stale = "⚠️ STALE " if age_days > STALE_AFTER_DAYS else ""
    return (
        f"- **{r['date']}** [{r['category']}] {stale}({r['source']}) "
        f"{r['title']} — {r['summary']} [[link]]({r['link']})"
    )


def render(seen: dict) -> str:
    today = datetime.now(timezone.utc).date()
    items = sorted(seen.values(), key=lambda r: (r["date"], r["captured_at"]), reverse=True)

    lines = [
        "# TeamChile — NFL News Feed (2026 season)",
        "",
        f"_Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_ "
        f"· {len(items)} items total",
        "",
        "> Auto-generated. Sources: " + ", ".join(f["name"] for f in FEEDS) + ". "
        f"Items older than {STALE_AFTER_DAYS} days are flagged ⚠️ STALE — "
        "treat as unconfirmed without a fresher source.",
        "",
        "---",
        "",
        f"## New in the last {NEW_SECTION_DAYS} days",
        "",
    ]

    cutoff_new = today - timedelta(days=NEW_SECTION_DAYS)
    new_items = [r for r in items if datetime.fromisoformat(r["date"]).date() >= cutoff_new]
    lines += [_row(r, today) for r in new_items] if new_items else ["_Nothing new this week._"]

    lines += ["", "---", "", "## Full log (newest first)", ""]
    lines += [_row(r, today) for r in items]
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    seen = load_seen()
    added = fetch_new_items(seen)
    OUTPUT_FILE.write_text(render(seen))
    save_seen(seen)
    print(f"Added {added} new item(s). Total items: {len(seen)}.")


if __name__ == "__main__":
    main()
