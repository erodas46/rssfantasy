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
PLAYERS_FILE = Path(__file__).parent / "players.json"
OUTPUT_FILE = Path("nfl-news-feed-2026.md")
STALE_AFTER_DAYS = 14      # items older than this get flagged in the render
NEW_SECTION_DAYS = 7       # window for the "new this week" section at the top

# Suffixes/particles used when deriving a "last name" fallback match.
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
PARTICLES = {"st", "van", "von", "de", "della", "du", "la"}

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
# Player matching
# ---------------------------------------------------------------------------
# Built from players.json (a name -> {pos, team} lookup generated from a
# project ADP file). Two-pass matching:
#   1. Full name, e.g. "Bijan Robinson" — reliable, used whenever present.
#   2. Unique last name, e.g. "Robinson" — used ONLY as a fallback, and ONLY
#      when exactly one player in the database shares that last name. This
#      catches blurbs that reference a player by last name only, without
#      risking a wrong attribution on common surnames (Jones, Williams...).

def load_players() -> dict:
    if PLAYERS_FILE.exists():
        return json.loads(PLAYERS_FILE.read_text())
    return {}


def extract_last_name(full_name: str) -> str:
    tokens = full_name.split()
    while tokens and tokens[-1].lower().strip(".") in SUFFIXES:
        tokens.pop()
    if len(tokens) >= 2 and tokens[-2].lower().strip(".") in PARTICLES:
        return f"{tokens[-2]} {tokens[-1]}"
    return tokens[-1] if tokens else full_name


def build_matchers(players: dict):
    full_names = sorted(players.keys(), key=len, reverse=True)
    full_patterns = [(n, re.compile(r"\b" + re.escape(n) + r"\b")) for n in full_names]

    # Last-name fallback is for individual players only. Team defense entries
    # (pos == "DEF") have "last names" like "Colts" or "Bills" that collide
    # with normal transaction headlines ("Colts sign veteran RB...") and would
    # misattribute the story to the team's DST. Full-name matching still
    # applies to DEF entries; only the risky fallback is skipped for them.
    last_name_map = {}
    for name, info in players.items():
        if info.get("pos") == "DEF":
            continue
        last = extract_last_name(name)
        last_name_map.setdefault(last, []).append(name)
    unique_last = {
        last: names[0] for last, names in last_name_map.items() if len(names) == 1
    }
    last_patterns = [
        (full, re.compile(r"\b" + re.escape(last) + r"\b"))
        for last, full in unique_last.items()
    ]
    return full_patterns, last_patterns


PLAYERS = load_players()
FULL_NAME_PATTERNS, LAST_NAME_PATTERNS = build_matchers(PLAYERS)


def match_player(text: str):
    """Return (player_name, 'POS/TEAM') or ('', '') if nothing matched."""
    for name, pattern in FULL_NAME_PATTERNS:
        if pattern.search(text):
            info = PLAYERS[name]
            return name, f"{info['pos']}/{info['team']}"
    for name, pattern in LAST_NAME_PATTERNS:
        if pattern.search(text):
            info = PLAYERS[name]
            return name, f"{info['pos']}/{info['team']}"
    return "", ""


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

def backfill_player_matches(seen: dict) -> int:
    """One-time migration: items captured before player matching existed
    won't have 'player'/'pos_team' keys yet. Re-run the matcher against their
    stored title+summary so old items don't stay permanently blank."""
    updated = 0
    for record in seen.values():
        if "player" not in record:
            player, pos_team = match_player(f"{record.get('title','')} {record.get('summary','')}")
            record["player"] = player
            record["pos_team"] = pos_team
            updated += 1
    return updated


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

            player, pos_team = match_player(f"{title} {summary}")

            seen[item_id] = {
                "date": parse_date(entry),
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "source": feed_cfg["name"],
                "category": category,
                "player": player,
                "pos_team": pos_team,
                "title": title,
                "summary": summary,
                "link": entry.get("link", ""),
            }
            added += 1
    return added


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

TABLE_HEADER = "| Date | Player | Pos/Team | Category | Summary | Source |"
TABLE_DIVIDER = "|---|---|---|---|---|---|"


def _cell(text: str) -> str:
    """Escape pipes/newlines so a field can't break the table row."""
    return re.sub(r"\s+", " ", (text or "—").replace("|", "\\|")).strip() or "—"


def _row(r: dict, today) -> str:
    age_days = (today - datetime.fromisoformat(r["date"]).date()).days
    category = r["category"] + (" ⚠️STALE" if age_days > STALE_AFTER_DAYS else "")
    summary_with_link = f"{r['summary']} ([link]({r['link']}))" if r["link"] else r["summary"]
    return "| {} | {} | {} | {} | {} | {} |".format(
        r["date"],
        _cell(r.get("player", "")),
        _cell(r.get("pos_team", "")),
        _cell(category),
        _cell(summary_with_link),
        _cell(r["source"]),
    )


def render(seen: dict) -> str:
    today = datetime.now(timezone.utc).date()
    items = sorted(seen.values(), key=lambda r: (r["date"], r["captured_at"]), reverse=True)
    matched = sum(1 for r in items if r.get("player"))

    lines = [
        "# TeamChile — NFL News Feed (2026 season)",
        "",
        f"_Last updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_ "
        f"· {len(items)} items total · {matched}/{len(items)} matched to a player",
        "",
        "> Auto-generated. Sources: " + ", ".join(f["name"] for f in FEEDS) + ". "
        f"Items older than {STALE_AFTER_DAYS} days are flagged ⚠️STALE — treat as "
        "unconfirmed without a fresher source. Player/Pos/Team is matched by name "
        "against players.json — a `—` means no confident match, not 'no player involved'.",
        "",
        "---",
        "",
        f"## New in the last {NEW_SECTION_DAYS} days",
        "",
    ]

    cutoff_new = today - timedelta(days=NEW_SECTION_DAYS)
    new_items = [r for r in items if datetime.fromisoformat(r["date"]).date() >= cutoff_new]
    if new_items:
        lines += [TABLE_HEADER, TABLE_DIVIDER] + [_row(r, today) for r in new_items]
    else:
        lines.append("_Nothing new this week._")

    lines += ["", "---", "", "## Full log (newest first)", "", TABLE_HEADER, TABLE_DIVIDER]
    lines += [_row(r, today) for r in items]
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    seen = load_seen()
    backfilled = backfill_player_matches(seen)
    added = fetch_new_items(seen)
    OUTPUT_FILE.write_text(render(seen))
    save_seen(seen)
    msg = f"Added {added} new item(s). Total items: {len(seen)}."
    if backfilled:
        msg += f" Backfilled player matches on {backfilled} older item(s)."
    print(msg)


if __name__ == "__main__":
    main()
