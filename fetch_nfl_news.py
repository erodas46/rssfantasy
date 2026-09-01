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

# Named insiders to flag when cited as the source within an already-ingested
# item (e.g. "...Adam Schefter of ESPN reports"). Neither ESPN nor Twitter/X
# offer a public per-reporter RSS feed, so this is the practical substitute:
# their scoops already flow into Rotowire/ESPN blurbs as citations, this just
# surfaces which items are theirs. Match on surname (how they're usually
# cited on second mention). Add more the same way, e.g. "Rapoport", "Pelissero".
INSIDER_WATCHLIST = ["Schefter"]

SLEEPER_PLAYERS_CACHE = Path("sleeper_players_cache.json")
SLEEPER_PLAYERS_CACHE_MAX_AGE_HOURS = 6   # Sleeper's guidance is "at most once
                                           # a day" as a courtesy for a ~5MB
                                           # payload; 6h means every one of
                                           # the 3 scheduled runs gets fresh
                                           # injury_status data instead of
                                           # roughly 1-in-3, which matters
                                           # for same-day status changes
                                           # (practice reports, inactives).
                                           # Raise this if that call volume
                                           # ever becomes a problem.

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

_BROAD_PATTERNS = None  # lazily built from the full Sleeper player universe
_BROAD_INFO = None      # (populated on first match_player() call, cached after)


def get_broad_matchers():
    """Full-name-only matcher over Sleeper's complete player universe
    (offense + IDP/DEF + practice squad — ~11k names). This catches players
    like individual defenders that don't appear in players.json because this
    league doesn't roster them, so they're absent from the ADP file it was
    built from. No last-name fallback at this scale: too many surname
    collisions across 11k names to do safely."""
    global _BROAD_PATTERNS, _BROAD_INFO
    if _BROAD_PATTERNS is None:
        directory = get_sleeper_player_directory()
        info = {}
        for _pid, d in directory.items():
            info.setdefault(d["name"], {"pos": d["pos"], "team": d["team"]})
        names_sorted = sorted(info.keys(), key=len, reverse=True)
        _BROAD_PATTERNS = [(n, re.compile(r"\b" + re.escape(n) + r"\b")) for n in names_sorted]
        _BROAD_INFO = info
    return _BROAD_PATTERNS, _BROAD_INFO


def match_player(text: str):
    """Return (player_name, 'POS/TEAM') or ('', '') if nothing matched.

    Tier 1: full-name match against players.json (curated, league-relevant).
    Tier 2: full-name match against the full Sleeper player universe (catches
            individual defenders and others outside the ADP pool).
    Tier 3: unique-last-name fallback, players.json only — the broad
            universe is too large for a last-name fallback to stay safe.
    """
    for name, pattern in FULL_NAME_PATTERNS:
        if pattern.search(text):
            info = PLAYERS[name]
            return name, f"{info['pos']}/{info['team']}"

    broad_patterns, broad_info = get_broad_matchers()
    for name, pattern in broad_patterns:
        if pattern.search(text):
            info = broad_info[name]
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


def detect_insiders(text: str) -> list:
    return [n for n in INSIDER_WATCHLIST if re.search(r"\b" + re.escape(n) + r"\b", text, re.IGNORECASE)]


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
    """One-time migration: items captured before player matching (or insider
    detection) existed won't have those keys yet. Re-derive them from the
    stored title+summary so old items don't stay permanently blank."""
    updated = 0
    for record in seen.values():
        touched = False
        text = f"{record.get('title','')} {record.get('summary','')}"
        if "player" not in record:
            player, pos_team = match_player(text)
            record["player"] = player
            record["pos_team"] = pos_team
            touched = True
        if "insiders" not in record:
            record["insiders"] = detect_insiders(text)
            touched = True
        if touched:
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
            insiders = detect_insiders(f"{title} {summary}")

            seen[item_id] = {
                "date": parse_date(entry),
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "source": feed_cfg["name"],
                "category": category,
                "player": player,
                "pos_team": pos_team,
                "insiders": insiders,
                "title": title,
                "summary": summary,
                "link": entry.get("link", ""),
            }
            added += 1
    return added


# ---------------------------------------------------------------------------
# Sleeper trending (waiver-wire adds/drops) — a different signal than the
# news above: actual manager behavior across Sleeper, not reporting. Sleeper
# has no text news API, but it does expose this for free with no auth.
# ---------------------------------------------------------------------------

def _http_get_json(url: str, timeout: int = 20) -> dict:
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "teamchile-nfl-news-feed/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_sleeper_player_directory() -> dict:
    """player_id -> {name, pos, team, injury_status}, cached locally and
    refreshed at most once every SLEEPER_PLAYERS_CACHE_MAX_AGE_HOURS, per
    Sleeper's own guidance not to hit /players/nfl (a ~5MB payload) on every
    call. injury_status is the same official NFL injury report ESPN's page
    shows — pulling it from here avoids scraping ESPN at all."""
    if SLEEPER_PLAYERS_CACHE.exists():
        cached = json.loads(SLEEPER_PLAYERS_CACHE.read_text())
        fetched_at = datetime.fromisoformat(cached["fetched_at"])
        if datetime.now(timezone.utc) - fetched_at < timedelta(hours=SLEEPER_PLAYERS_CACHE_MAX_AGE_HOURS):
            return cached["players"]

    try:
        raw = _http_get_json("https://api.sleeper.app/v1/players/nfl")
    except Exception as exc:
        print(f"WARN: could not refresh Sleeper player directory ({exc}); ", end="")
        if SLEEPER_PLAYERS_CACHE.exists():
            print("using stale cache.")
            return json.loads(SLEEPER_PLAYERS_CACHE.read_text())["players"]
        print("no cache available, trending/injury sections will be skipped this run.")
        return {}

    directory = {}
    for pid, info in raw.items():
        name = info.get("full_name") or " ".join(
            filter(None, [info.get("first_name"), info.get("last_name")])
        )
        if not name:
            continue
        directory[pid] = {
            "name": name,
            "pos": info.get("position") or (info.get("fantasy_positions") or ["—"])[0],
            "team": info.get("team") or "FA",
            "injury_status": info.get("injury_status") or "",
            "injury_start_date": info.get("injury_start_date"),
        }
    SLEEPER_PLAYERS_CACHE.write_text(json.dumps(
        {"fetched_at": datetime.now(timezone.utc).isoformat(), "players": directory}
    ))
    return directory


def get_sleeper_trending(direction: str, lookback_hours: int = 24, limit: int = 15) -> list:
    url = (f"https://api.sleeper.app/v1/players/nfl/trending/{direction}"
           f"?lookback_hours={lookback_hours}&limit={limit}")
    try:
        return _http_get_json(url)
    except Exception as exc:
        print(f"WARN: could not fetch Sleeper trending/{direction} ({exc})")
        return []


def render_trending_section() -> list:
    directory = get_sleeper_player_directory()
    if not directory:
        return []

    lines = [
        "", "---", "", "## Sleeper waiver-wire trends (last 24h)", "",
        "_Real add/drop momentum from Sleeper's own trending API — actual "
        "manager behavior across the platform, not reporting. Complements "
        "the news tables above rather than duplicating them._", "",
    ]
    for direction, label in [("add", "Top adds"), ("drop", "Top drops")]:
        trending = get_sleeper_trending(direction)
        lines += [f"**{label}**", ""]
        if not trending:
            lines += ["_Unavailable this run._", ""]
            continue
        lines += ["| Player | Pos/Team | # leagues (24h) |", "|---|---|---|"]
        for row in trending:
            pid = row.get("player_id")
            count = row.get("count", "—")
            info = directory.get(pid, {"name": f"(unknown id {pid})", "pos": "—", "team": "—"})
            lines.append(f"| {info['name']} | {info['pos']}/{info['team']} | {count} |")
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Injury report — sourced from Sleeper's injury_status field (see
# get_sleeper_player_directory above), NOT scraped from ESPN's injuries
# page. Same official report, already-cached structured data, nothing to
# maintain. Tracks status changes run-over-run, which matters more for
# in-season lineup/waiver decisions than a static snapshot.
# ---------------------------------------------------------------------------

INJURY_SNAPSHOT_FILE = Path("injury_status_snapshot.json")


def get_injury_snapshot() -> dict:
    if INJURY_SNAPSHOT_FILE.exists():
        return json.loads(INJURY_SNAPSHOT_FILE.read_text())
    return {}


def save_injury_snapshot(snapshot: dict) -> None:
    INJURY_SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=2, sort_keys=True))


def format_injury_date(raw) -> str:
    if not raw:
        return ""
    try:
        num = float(raw)
        if num > 10**11:   # looks like epoch milliseconds
            return datetime.fromtimestamp(num / 1000, tz=timezone.utc).date().isoformat()
        if num > 10**9:    # looks like epoch seconds
            return datetime.fromtimestamp(num, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError):
        pass
    return str(raw)[:10]


def render_injury_section(seen: dict) -> list:
    directory = get_sleeper_player_directory()
    if not directory:
        return []

    # Neither Sleeper nor the free ESPN/Rotowire APIs give fantasy-impact
    # commentary. Substitute: the most recent news item we've already
    # captured for that player, so there's real context instead of a bare
    # status.
    latest_by_player = {}
    for record in seen.values():
        name = record.get("player")
        if not name:
            continue
        prev = latest_by_player.get(name)
        key = (record["date"], record["captured_at"])
        if prev is None or key > (prev["date"], prev["captured_at"]):
            latest_by_player[name] = record

    previous = get_injury_snapshot()
    current = {}
    changes = []

    for pid, info in directory.items():
        status = info.get("injury_status") or ""
        if not status:
            continue
        current[pid] = {
            "name": info["name"], "pos": info["pos"], "team": info["team"],
            "status": status, "since": format_injury_date(info.get("injury_start_date")),
        }
        prev_status = previous.get(pid, {}).get("status", "")
        if status != prev_status:
            changes.append({
                "name": info["name"], "pos": info["pos"], "team": info["team"],
                "from": prev_status or "(unlisted)", "to": status,
            })

    for pid, prev_info in previous.items():
        if pid not in current and prev_info.get("status"):
            changes.append({
                "name": prev_info["name"], "pos": prev_info["pos"], "team": prev_info["team"],
                "from": prev_info["status"], "to": "(cleared)",
            })

    save_injury_snapshot(current)

    lines = [
        "", "---", "", "## Injury report (official status, via Sleeper)", "",
        "_'Latest news' pulls the most recent matching item from the tables "
        "above (not a separate source) — Sleeper/ESPN's free APIs don't "
        "include fantasy-impact commentary._", "",
        "**Status changes since last run**", "",
    ]
    if changes:
        lines += ["| Player | Pos/Team | From | To |", "|---|---|---|---|"]
        lines += [f"| {c['name']} | {c['pos']}/{c['team']} | {c['from']} | {c['to']} |" for c in changes]
    else:
        lines.append("_None since last run._")

    lines += ["", "**Full current report**", ""]
    if current:
        rows = sorted(current.values(), key=lambda r: (r["team"], r["name"]))
        lines += ["| Player | Pos/Team | Status | Since | Latest news |", "|---|---|---|---|---|"]
        for r in rows:
            news = latest_by_player.get(r["name"])
            if news:
                news_cell = _cell(f"{news['summary']}" + (f" ([link]({news['link']}))" if news.get("link") else ""))
            else:
                news_cell = "—"
            lines.append(f"| {r['name']} | {r['pos']}/{r['team']} | {r['status']} | {r['since'] or '—'} | {news_cell} |")
    else:
        lines.append("_Nothing currently listed._")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Transactions — via ESPN's undocumented core API, not FantasyPros HTML.
#
# CAVEAT: this endpoint isn't publicly documented and its exact field names
# aren't independently verifiable ahead of a live run. This is written
# defensively (several plausible field names tried, never crashes the run),
# but treat the very first live run as the real test. If it comes back with
# zero parsed items despite a nonzero fetch count, the schema likely differs
# from what's assumed here — share a sample raw item and the parser can be
# tightened in a few lines.
# ---------------------------------------------------------------------------

ESPN_TRANSACTIONS_URL = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/transactions"


def fetch_espn_transactions(limit: int = 200) -> list:
    try:
        data = _http_get_json(f"{ESPN_TRANSACTIONS_URL}?limit={limit}")
    except Exception as exc:
        print(f"WARN: could not fetch ESPN transactions ({exc})")
        return []

    # Diagnostics first: log the real shape so a schema mismatch is fixable
    # from the next run's log alone, instead of another guess-and-check pass.
    if isinstance(data, list):
        print(f"DEBUG: ESPN transactions response is a top-level list, length {len(data)}")
        items = data
    elif isinstance(data, dict):
        print(f"DEBUG: ESPN transactions response keys: {sorted(data.keys())}")
        items = None
        for key in ("items", "transactions", "data", "results", "entries"):
            if key in data:
                items = data[key]
                print(f"DEBUG: using key '{key}', found {len(items) if hasattr(items, '__len__') else '?'} entries")
                break
    else:
        print(f"WARN: ESPN transactions returned unexpected type: {type(data)}")
        return []

    if not items:
        snippet = json.dumps(data)[:1500]
        print(f"WARN: ESPN transactions - no usable list found under any known key. "
              f"Raw response (truncated to 1500 chars): {snippet}")
        return []

    parsed = []
    unparsed_sample = None
    for item in items:
        if not isinstance(item, dict):
            continue
        text = (item.get("description") or item.get("text")
                or item.get("shortText") or item.get("displayText") or "")
        date = item.get("date") or item.get("transactionDate") or item.get("stamp") or ""
        team_field = item.get("team")
        team_name = ""
        if isinstance(team_field, dict):
            team_name = team_field.get("displayName") or team_field.get("abbreviation") or team_field.get("name") or ""
        text = text.strip()
        if not text:
            if unparsed_sample is None:
                unparsed_sample = json.dumps(item)[:800]
            continue
        parsed.append({"text": text, "date": str(date)[:10], "team": team_name})

    if not parsed and items:
        print(f"WARN: fetched {len(items)} ESPN transaction item(s) but parsed 0 — "
              "field names likely don't match. Sample raw item (truncated to 800 "
              f"chars): {unparsed_sample}")
    return parsed


def fetch_espn_transactions_into_seen(seen: dict) -> int:
    added = 0
    for tx in fetch_espn_transactions():
        item_id = "espn-tx-" + hashlib.sha1(tx["text"].encode("utf-8")).hexdigest()[:16]
        if item_id in seen:
            continue
        title = tx["text"]
        player, pos_team = match_player(title)
        seen[item_id] = {
            "date": tx["date"] or datetime.now(timezone.utc).date().isoformat(),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "source": "ESPN Transactions",
            "category": "TRANSACTION",
            "player": player,
            "pos_team": pos_team,
            "insiders": detect_insiders(title),
            "title": title,
            "summary": (f"[{tx['team']}] " if tx["team"] else "") + title,
            "link": "",
        }
        added += 1
    return added


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

TABLE_HEADER = "| Date | Player | Pos/Team | Category | Insider | Summary | Source |"
TABLE_DIVIDER = "|---|---|---|---|---|---|---|"


def _cell(text: str) -> str:
    """Escape pipes/newlines so a field can't break the table row."""
    return re.sub(r"\s+", " ", (text or "—").replace("|", "\\|")).strip() or "—"


def _row(r: dict, today) -> str:
    age_days = (today - datetime.fromisoformat(r["date"]).date()).days
    category = r["category"] + (" ⚠️STALE" if age_days > STALE_AFTER_DAYS else "")
    summary_with_link = f"{r['summary']} ([link]({r['link']}))" if r["link"] else r["summary"]
    insiders = r.get("insiders") or []
    insider_cell = ("🔥 " + ", ".join(insiders)) if insiders else "—"
    return "| {} | {} | {} | {} | {} | {} | {} |".format(
        r["date"],
        _cell(r.get("player", "")),
        _cell(r.get("pos_team", "")),
        _cell(category),
        _cell(insider_cell),
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
        "> Auto-generated. Sources: " + ", ".join(f["name"] for f in FEEDS) + " + Sleeper "
        "trending API. Items older than " + str(STALE_AFTER_DAYS) + " days are flagged "
        "⚠️STALE — treat as unconfirmed without a fresher source. Player/Pos/Team is "
        "matched by name against players.json (`—` = no confident match, not 'no player "
        "involved'). Insider flags when a name on the watchlist "
        f"({', '.join(INSIDER_WATCHLIST)}) is cited as the source within an item.",
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
    lines += render_trending_section()
    lines += render_injury_section(seen)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    seen = load_seen()
    backfilled = backfill_player_matches(seen)
    added = fetch_new_items(seen)
    added_tx = fetch_espn_transactions_into_seen(seen)
    OUTPUT_FILE.write_text(render(seen))
    save_seen(seen)
    msg = f"Added {added + added_tx} new item(s) ({added} RSS, {added_tx} ESPN transactions). Total items: {len(seen)}."
    if backfilled:
        msg += f" Backfilled player matches on {backfilled} older item(s)."
    print(msg)


if __name__ == "__main__":
    main()
