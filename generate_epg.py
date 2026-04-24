#!/usr/bin/env python3
"""
Generate an XMLTV EPG file from an Xtream Codes IPTV provider.

Reads channel names that embed event info (team matchups + timestamps)
and turns them into proper EPG entries. Designed for "single event per
day" sports channels like ESPN+ event feeds, MLB PPV, NFL PPV, etc.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

# ---------- Config ----------

# Category IDs we care about, with metadata about how to interpret them.
# tz: timezone that bare timestamps in channel names use.
# default_duration_min: used when only a start time is found (ESPN+ style).
# display_prefix: what the <display-name> in XMLTV looks like. The full
#   original channel name is kept in <programme>/title.
CATEGORIES = {
    "911":  {"name": "ESPN+",    "tz": "America/New_York", "default_duration_min": 180},
    "1960": {"name": "ESPN+ VIP","tz": "America/New_York", "default_duration_min": 180},
    "606":  {"name": "MLB",      "tz": "UTC",              "default_duration_min": 210},
    "1185": {"name": "MLB Team", "tz": "UTC",              "default_duration_min": 210},
    "597":  {"name": "NFL",      "tz": "America/New_York", "default_duration_min": 240},
}

# Skip channels whose name looks like a category header/separator
HEADER_RE = re.compile(r"^\s*#{2,}")

# Skip obvious placeholder timestamps (year far in the future)
PLACEHOLDER_YEAR_THRESHOLD = 2050

# Drop events that ended more than this many hours ago (avoids stale
# MLB entries from prior games showing up as "current" to TiviMate)
STALE_CUTOFF_HOURS = 24

# Default UA — mimics VLC, since raw curl/python UAs may get 884'd
DEFAULT_UA = "VLC/3.0.20 LibVLC/3.0.20"


# ---------- Data types ----------

@dataclass
class ParsedEvent:
    """What we pulled out of a channel name."""
    title: str                  # "Tigers x Reds" or "Macarthur FC vs. Wellington Phoenix"
    start_utc: datetime
    stop_utc: datetime
    had_explicit_stop: bool     # False if we estimated it from default_duration


@dataclass
class Channel:
    stream_id: int
    raw_name: str               # original channel name, unmodified
    category_id: str
    category_name: str          # human-friendly: "ESPN+"
    epg_channel_id: str         # provider-set EPG id, empty for event channels
    event: ParsedEvent | None   # None if we couldn't parse an event


# ---------- Parsers ----------

def parse_mlb_style(name: str, tz_name: str) -> ParsedEvent | None:
    """
    Matches: 'MLB 1 | Tigers x Reds start:2026-04-24 23:40:00 stop:2026-04-25 06:53:20'

    Both start and stop are explicit, so this is the easiest case.
    Timestamps in this format are treated as UTC (empirically true for
    this provider).
    """
    m = re.search(
        r"\|\s*(.+?)\s+start:(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+stop:(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
        name,
    )
    if not m:
        return None
    title = m.group(1).strip()
    start_local = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S")
    stop_local = datetime.strptime(m.group(3), "%Y-%m-%d %H:%M:%S")
    tz = ZoneInfo(tz_name)
    return ParsedEvent(
        title=title,
        start_utc=start_local.replace(tzinfo=tz).astimezone(timezone.utc),
        stop_utc=stop_local.replace(tzinfo=tz).astimezone(timezone.utc),
        had_explicit_stop=True,
    )


def parse_espn_plus_style(name: str, tz_name: str, default_duration_min: int) -> ParsedEvent | None:
    """
    Matches: 'US (ESPN+ 002) | Macarthur FC vs. Wellington Phoenix Apr 24 5:30AM ET (2026-04-24 05:30:00)'

    The parenthesized ISO timestamp at the end is our source of truth;
    the human 'Apr 24 5:30AM ET' is redundant but confirms the tz.
    Only start is given, so we estimate stop using default_duration_min.
    """
    m = re.search(r"\|\s*(.+?)\s*\((\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\)\s*$", name)
    if not m:
        return None
    title = m.group(1).strip()

    # Filter placeholder-year entries (provider uses 2098-12-31 for unassigned slots)
    year = int(m.group(2))
    if year >= PLACEHOLDER_YEAR_THRESHOLD:
        return None

    # The human-readable tz marker ('ET', 'PT', etc.) lives inside the title
    # group — we need to strip it before using title, and it also tells us
    # if the provider deviates from our configured default tz. For now we
    # trust the configured tz; if we see PT/CT in the wild we can parse.
    # Strip common patterns: 'Apr 24 5:30AM ET', 'Apr 24 5:30PM ET', etc.
    title = re.sub(
        r"\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{1,2}:\d{2}[AP]M\s+[A-Z]{2,3}\s*$",
        "",
        title,
    ).strip()

    start_local = datetime(
        year, int(m.group(3)), int(m.group(4)),
        int(m.group(5)), int(m.group(6)), int(m.group(7)),
    )
    tz = ZoneInfo(tz_name)
    start_utc = start_local.replace(tzinfo=tz).astimezone(timezone.utc)
    stop_utc = start_utc + timedelta(minutes=default_duration_min)
    return ParsedEvent(
        title=title,
        start_utc=start_utc,
        stop_utc=stop_utc,
        had_explicit_stop=False,
    )


PARSERS = [parse_mlb_style, parse_espn_plus_style]


def parse_event(name: str, category_meta: dict) -> ParsedEvent | None:
    """Try each parser. First one that matches wins."""
    for parser in PARSERS:
        try:
            if parser is parse_mlb_style:
                event = parser(name, category_meta["tz"])
            else:
                event = parser(name, category_meta["tz"], category_meta["default_duration_min"])
            if event is not None:
                return event
        except (ValueError, KeyError):
            # Bad date, missing field, etc. — try next parser
            continue
    return None


def is_header_channel(name: str) -> bool:
    """'####### ESPN+ PPV #######' etc. — not a real stream."""
    return bool(HEADER_RE.match(name))


# ---------- Fetching ----------

def load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file. Blank lines and # comments ignored."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def fetch_streams(base_url: str, username: str, password: str, category_id: str,
                  user_agent: str) -> list[dict]:
    """Hit the Xtream player_api and return the stream list for a category."""
    params = urllib.parse.urlencode({
        "username": username,
        "password": password,
        "action": "get_live_streams",
        "category_id": category_id,
    })
    url = f"{base_url.rstrip('/')}/player_api.php?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_streams_from_file(path: Path) -> list[dict]:
    """For offline testing against a saved JSON dump."""
    return json.loads(path.read_text(encoding="utf-8"))


# ---------- Channel processing ----------

def process_streams(streams: list[dict], category_id: str, category_meta: dict,
                    now_utc: datetime) -> list[Channel]:
    """Turn raw API stream dicts into Channel objects with parsed events."""
    channels = []
    stale_cutoff = now_utc - timedelta(hours=STALE_CUTOFF_HOURS)

    for s in streams:
        name = s.get("name", "").strip()
        if not name or is_header_channel(name):
            continue

        epg_id = (s.get("epg_channel_id") or "").strip()

        # 24/7 channels (MLB Network, NFL Network, NFL RedZone) have a real
        # epg_channel_id set by the provider. Skip — IPTVEditor will already
        # have an EPG source for these.
        if epg_id:
            continue

        event = parse_event(name, category_meta)

        # Skip stale events
        if event and event.stop_utc < stale_cutoff:
            continue

        channels.append(Channel(
            stream_id=int(s["stream_id"]),
            raw_name=name,
            category_id=category_id,
            category_name=category_meta["name"],
            epg_channel_id=epg_id,
            event=event,
        ))
    return channels


# ---------- XMLTV generation ----------

def xmltv_time(dt: datetime) -> str:
    """XMLTV format: 'YYYYMMDDhhmmss +0000'."""
    return dt.strftime("%Y%m%d%H%M%S %z")


def tvg_id_for(channel: Channel) -> str:
    """
    Stable ID we'll use both in XMLTV and for IPTVEditor matching.
    Using stream_id guarantees uniqueness and stability across runs.
    Prefix with category for readability.
    """
    slug = channel.category_name.lower().replace(" ", "-").replace("+", "plus")
    return f"iptv-{slug}-{channel.stream_id}"


def display_name_for(channel: Channel) -> str:
    """
    What shows up as the channel label. Keep it simple and stable across
    events so the channel survives day-to-day schedule changes.
    """
    # For ESPN+ style we can yank the channel number cleanly
    m = re.match(r"US \(ESPN\+\s*(\d+)\)", channel.raw_name)
    if m:
        return f"ESPN+ {m.group(1)}"
    m = re.match(r"MLB\s+(\d+)\s*[|:]", channel.raw_name)
    if m:
        return f"MLB {m.group(1)}"
    m = re.match(r"NFL\s+\|\s*(\S+)", channel.raw_name)
    if m:
        return f"NFL {m.group(1)}"
    # Fallback: use the raw name up to the first pipe
    return channel.raw_name.split("|", 1)[0].strip() or channel.raw_name


def build_xmltv(channels: list[Channel], now_utc: datetime) -> str:
    """Build the full XMLTV document as a string."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
        f'<tv generator-info-name="iptv-epg-generator" date="{now_utc.strftime("%Y%m%d%H%M%S %z")}">',
    ]

    # Channels first, then programmes — XMLTV convention
    for ch in channels:
        tvg_id = tvg_id_for(ch)
        display = display_name_for(ch)
        lines.append(f'  <channel id="{escape(tvg_id, {chr(34): "&quot;"})}">')
        lines.append(f'    <display-name>{escape(display)}</display-name>')
        # Second display-name = raw provider name. Lets IPTVEditor's fuzzy
        # matcher hit a 100% score on unrenamed playlist channels, while
        # renamed ones still match the short name above.
        if ch.raw_name and ch.raw_name != display:
            lines.append(f'    <display-name>{escape(ch.raw_name)}</display-name>')
        lines.append('  </channel>')

    # 24-hour "Off Air" block for channels with no event. Kept simple:
    # one big block, not a grid of empty 30-min slots.
    off_air_start = now_utc.replace(minute=0, second=0, microsecond=0)
    off_air_stop = off_air_start + timedelta(hours=24)

    for ch in channels:
        tvg_id = tvg_id_for(ch)
        if ch.event:
            lines.append(
                f'  <programme start="{xmltv_time(ch.event.start_utc)}" '
                f'stop="{xmltv_time(ch.event.stop_utc)}" '
                f'channel="{escape(tvg_id, {chr(34): "&quot;"})}">'
            )
            lines.append(f'    <title>{escape(ch.event.title)}</title>')
            lines.append(f'    <category>{escape(ch.category_name)}</category>')
            if not ch.event.had_explicit_stop:
                # Flag estimated end times in the description so we know
                # not to trust them too tightly
                lines.append(f'    <desc>{escape(f"Estimated end time. Source: {ch.raw_name}")}</desc>')
            else:
                lines.append(f'    <desc>{escape(ch.raw_name)}</desc>')
            lines.append('  </programme>')
        else:
            lines.append(
                f'  <programme start="{xmltv_time(off_air_start)}" '
                f'stop="{xmltv_time(off_air_stop)}" '
                f'channel="{escape(tvg_id, {chr(34): "&quot;"})}">'
            )
            lines.append('    <title>Off Air</title>')
            lines.append(f'    <category>{escape(ch.category_name)}</category>')
            lines.append('  </programme>')

    lines.append('</tv>')
    return "\n".join(lines)


# ---------- CLI ----------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", help="Provider base URL (e.g. http://example.com)")
    ap.add_argument("--username", help="Xtream username")
    ap.add_argument("--password", help="Xtream password")
    ap.add_argument("--user-agent", default=DEFAULT_UA, help=f"HTTP User-Agent (default: {DEFAULT_UA})")
    ap.add_argument("--output", default="epg.xml", help="Where to write the XMLTV file")
    ap.add_argument("--from-files", action="store_true",
                    help="Don't fetch; read JSON dumps from --cache-dir instead (for testing)")
    ap.add_argument("--cache-dir", default=".",
                    help="Directory holding {category_id}.json when --from-files, or to write fresh dumps")
    ap.add_argument("--save-cache", action="store_true",
                    help="When fetching, also save raw API responses to --cache-dir")
    ap.add_argument("--categories", nargs="+",
                    help="Only process these category IDs (default: all configured)")
    ap.add_argument("--env-file", default="provider.env",
                    help="Path to KEY=VALUE file with BASE_URL/USERNAME/PASSWORD (default: provider.env). "
                         "CLI args override values from this file.")
    args = ap.parse_args(argv)

    env_path = Path(args.env_file)
    if env_path.is_file():
        env = load_env_file(env_path)
        if not args.base_url:
            args.base_url = env.get("BASE_URL")
        if not args.username:
            args.username = env.get("USERNAME")
        if not args.password:
            args.password = env.get("PASSWORD")

    now_utc = datetime.now(timezone.utc)
    cache_dir = Path(args.cache_dir)

    if args.from_files:
        # Accept both {category_id}.json and the original names (espn_plus.json, etc.)
        filename_map = {
            "911": ["911.json", "espn_plus.json"],
            "1960": ["1960.json", "espn_plus_vip.json"],
            "606": ["606.json", "mlb.json"],
            "1185": ["1185.json", "mlb_team.json"],
            "597": ["597.json", "nfl.json"],
        }
    else:
        if not (args.base_url and args.username and args.password):
            print("ERROR: --base-url, --username, --password required (or use --from-files)",
                  file=sys.stderr)
            return 2

    category_ids = args.categories or list(CATEGORIES.keys())
    all_channels: list[Channel] = []

    for cat_id in category_ids:
        if cat_id not in CATEGORIES:
            print(f"WARN: category {cat_id} not configured, skipping", file=sys.stderr)
            continue
        meta = CATEGORIES[cat_id]

        if args.from_files:
            found = None
            for candidate in filename_map.get(cat_id, [f"{cat_id}.json"]):
                p = cache_dir / candidate
                if p.exists():
                    found = p
                    break
            if not found:
                print(f"WARN: no cached file for category {cat_id} ({meta['name']}), skipping",
                      file=sys.stderr)
                continue
            streams = load_streams_from_file(found)
            print(f"Loaded {len(streams)} streams from {found} ({meta['name']})", file=sys.stderr)
        else:
            try:
                streams = fetch_streams(args.base_url, args.username, args.password,
                                        cat_id, args.user_agent)
            except Exception as e:
                print(f"ERROR fetching category {cat_id} ({meta['name']}): {e}", file=sys.stderr)
                continue
            print(f"Fetched {len(streams)} streams for {meta['name']}", file=sys.stderr)
            if args.save_cache:
                cache_dir.mkdir(parents=True, exist_ok=True)
                (cache_dir / f"{cat_id}.json").write_text(
                    json.dumps(streams, indent=2), encoding="utf-8"
                )

        channels = process_streams(streams, cat_id, meta, now_utc)
        all_channels.extend(channels)
        print(f"  -> {len(channels)} usable channels ({sum(1 for c in channels if c.event)} with events)",
              file=sys.stderr)

    xml = build_xmltv(all_channels, now_utc)
    Path(args.output).write_text(xml, encoding="utf-8")
    print(f"Wrote {len(all_channels)} channels to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
