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

# Category IDs we care about, with their human-readable name. The parser
# itself handles timezone and duration logic uniformly across all categories
# (see PARSER DOCS at top of this file), so adding a new category is just:
#
#     "<id>": {"name": "<label>"},
#
# To add a category to the live runs, also include its id on the workflow's
# --categories flag in .github/workflows/regenerate-epg.yml.
CATEGORIES = {
    "911":  {"name": "ESPN+"},
    "1960": {"name": "ESPN+ VIP"},
    "606":  {"name": "MLB"},
    "1185": {"name": "MLB Team"},
    "597":  {"name": "NFL"},
}

# Skip channels whose name looks like a category header/separator
HEADER_RE = re.compile(r"^\s*#{2,}")

# Drop events that ended more than this many hours ago (avoids stale
# entries from prior games showing up as "current" to TiviMate)
STALE_CUTOFF_HOURS = 24

# Default UA — mimics VLC, since raw curl/python UAs may get 884'd
DEFAULT_UA = "VLC/3.0.20 LibVLC/3.0.20"

# How long to assume an event runs when only a start time is given.
DEFAULT_DURATION_MIN = 180

# Year >= this means "the provider used a sentinel placeholder."
PLACEHOLDER_YEAR_THRESHOLD = 2050

# Bare-time-only formats (no date): if the inferred start is more than
# this many hours in the past, bump to tomorrow.
BARE_TIME_PAST_BUMP_HOURS = 6

# Formats with month/day but no year: if "this year" lands the event
# more than this many days in the past, try next year.
NO_YEAR_PAST_BUMP_DAYS = 30

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

MONTH_ABBREV = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Empty-channel signals
EMPTY_SIGNALS_RE = re.compile(r"no event streaming", re.IGNORECASE)

# Quick check: does this name have ANY plausible timestamp pattern?
# Used to disambiguate "name ends with : or | = empty" from real events.
HAS_TS_RE = re.compile(
    r"start:\d{4}-\d{2}-\d{2}"
    r"|\(\d{4}-\d{2}-\d{2}"
    r"|@\s+[A-Za-z]{3,}\s+\d{1,2}\s+\d{1,2}:\d{2}"
    r"|\b\d{1,2}:\d{2}\s*(?:[ap]m|[AP]M)\b"
    r"|\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{1,2}\s+[A-Za-z]{3,}\s+\d{1,2}:\d{2}",
    re.IGNORECASE,
)


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
#
# A single parse_event() function tries each matcher in priority order
# (most specific first). All start times are normalized to UTC. The matched
# timestamp text is stripped from the channel name to produce a cleaner
# title; the raw name is preserved separately for use in <desc>.
#
# Timezone policy: this provider broadcasts to US viewers and almost
# everything uses Eastern. The MLB 'start:/stop:' format empirically uses
# UTC. A few formats include an explicit TZ marker (e.g. 'EDT') which we
# use directly. Everything else falls back to Eastern.


def _to_utc(dt_naive: datetime, tz) -> datetime:
    return dt_naive.replace(tzinfo=tz).astimezone(UTC)


def _strip_match_and_clean(name: str, match: re.Match) -> str:
    """Remove the matched timestamp text and clean up trailing channel noise."""
    title = (name[:match.start()] + name[match.end():]).strip()

    # Strip trailing channel-marker noise that's ugly in the grid:
    #   ":WNBA  02"   ":Golf  06"   "| US: NFHS PPV 9"   etc.
    title = re.sub(r"\s*[:|]\s*[A-Za-z][\w +/.-]{0,40}\s+\d+\s*$", "", title)
    # Strip "| <stuff> | <stuff>" pipe-delimited tails (NFHS-style:
    # "| 8K EXCLUSIVE | US: NFHS PPV 9")
    title = re.sub(r"\s*\|\s*[^|]+\s*\|\s*[^|]+\s*$", "", title)
    # NFHS leaves an orphan "(US)" after stripping the timestamp:
    # "NEXT | TITLE | (US)" — drop trailing parenthesized country/lang code.
    title = re.sub(r"\s*\|\s*\([A-Za-z ]{1,8}\)\s*$", "", title)
    # Collapse internal multi-spaces and stray leading/trailing punctuation
    title = re.sub(r"\s+", " ", title).strip()
    title = title.strip("|-: ")
    return title or name.strip()


def _is_empty_channel(name: str) -> bool:
    """True if the name represents a channel with no scheduled event."""
    stripped = name.strip()
    if not stripped:
        return True
    if EMPTY_SIGNALS_RE.search(stripped):
        return True
    # 'World Cup 08 -', 'MLB 17 |', 'WNBA 04 :', ':Golf  08' — trailing
    # punctuation with nothing after, AND no timestamp anywhere in the name.
    if re.search(r"[-:|]\s*$", stripped) and not HAS_TS_RE.search(stripped):
        return True
    return False


def _match_mlb_start_stop(name: str, now_utc: datetime):
    """MLB-style: '... start:YYYY-MM-DD HH:MM:SS stop:YYYY-MM-DD HH:MM:SS'.
    Both timestamps are treated as UTC."""
    m = re.search(
        r"\s*start:(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"
        r"\s+stop:(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
        name,
    )
    if not m:
        return None
    try:
        start = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        stop = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return ParsedEvent(
        title=_strip_match_and_clean(name, m),
        start_utc=_to_utc(start, UTC),
        stop_utc=_to_utc(stop, UTC),
        had_explicit_stop=True,
    )


def _match_paren_iso(name: str, now_utc: datetime):
    """ESPN+ style: '... (YYYY-MM-DD HH:MM:SS)'. Treated as ET."""
    m = re.search(r"\((\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\)", name)
    if not m:
        return None
    year = int(m.group(1))
    if year >= PLACEHOLDER_YEAR_THRESHOLD:
        return None
    try:
        start_naive = datetime(year, int(m.group(2)), int(m.group(3)),
                               int(m.group(4)), int(m.group(5)), int(m.group(6)))
    except ValueError:
        return None
    start_utc = _to_utc(start_naive, ET)
    return ParsedEvent(
        title=_strip_match_and_clean(name, m),
        start_utc=start_utc,
        stop_utc=start_utc + timedelta(minutes=DEFAULT_DURATION_MIN),
        had_explicit_stop=False,
    )


def _match_weekday_date_tz(name: str, now_utc: datetime):
    """'Mon 29 Jun 17:30 EDT' style (NFHS PPV). Has explicit TZ marker."""
    m = re.search(
        r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2})\s+([A-Za-z]{3,})\s+"
        r"(\d{1,2}):(\d{2})\s+([A-Z]{2,4})",
        name,
    )
    if not m:
        return None
    day = int(m.group(1))
    month = MONTH_ABBREV.get(m.group(2).lower()[:3])
    if month is None:
        return None
    hour, minute = int(m.group(3)), int(m.group(4))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    tz_map = {
        "EDT": ET, "EST": ET, "ET": ET,
        "CDT": ZoneInfo("America/Chicago"), "CST": ZoneInfo("America/Chicago"), "CT": ZoneInfo("America/Chicago"),
        "MDT": ZoneInfo("America/Denver"), "MST": ZoneInfo("America/Denver"), "MT": ZoneInfo("America/Denver"),
        "PDT": ZoneInfo("America/Los_Angeles"), "PST": ZoneInfo("America/Los_Angeles"), "PT": ZoneInfo("America/Los_Angeles"),
        "UTC": UTC, "GMT": UTC,
    }
    tz = tz_map.get(m.group(5).upper(), ET)

    year = now_utc.astimezone(ET).year
    try:
        candidate = datetime(year, month, day, hour, minute, 0)
    except ValueError:
        return None
    candidate_utc = _to_utc(candidate, tz)
    if (now_utc - candidate_utc) > timedelta(days=NO_YEAR_PAST_BUMP_DAYS):
        try:
            candidate = datetime(year + 1, month, day, hour, minute, 0)
            candidate_utc = _to_utc(candidate, tz)
        except ValueError:
            return None
    return ParsedEvent(
        title=_strip_match_and_clean(name, m),
        start_utc=candidate_utc,
        stop_utc=candidate_utc + timedelta(minutes=DEFAULT_DURATION_MIN),
        had_explicit_stop=False,
    )


def _match_at_datetime(name: str, now_utc: datetime):
    """'@ Mon DD HH:MM' (WNBA, 24hr) or '@ Mon DD H:MM AM/PM' (Golf, 12hr).
    No year given — infer from current ET date, bump to next year if
    inferred date is too far in the past."""
    m = re.search(
        r"@\s+([A-Za-z]{3,})\s+(\d{1,2})\s+(\d{1,2}):(\d{2})\s*([AaPp][Mm])?",
        name,
    )
    if not m:
        return None
    month = MONTH_ABBREV.get(m.group(1).lower()[:3])
    if month is None:
        return None
    day = int(m.group(2))
    hour = int(m.group(3))
    minute = int(m.group(4))
    ampm = m.group(5)
    if ampm:
        ap = ampm.lower()
        if ap == "pm" and hour < 12:
            hour += 12
        elif ap == "am" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    year = now_utc.astimezone(ET).year
    try:
        candidate = datetime(year, month, day, hour, minute, 0)
    except ValueError:
        return None
    candidate_utc = _to_utc(candidate, ET)
    if (now_utc - candidate_utc) > timedelta(days=NO_YEAR_PAST_BUMP_DAYS):
        try:
            candidate = datetime(year + 1, month, day, hour, minute, 0)
            candidate_utc = _to_utc(candidate, ET)
        except ValueError:
            return None
    return ParsedEvent(
        title=_strip_match_and_clean(name, m),
        start_utc=candidate_utc,
        stop_utc=candidate_utc + timedelta(minutes=DEFAULT_DURATION_MIN),
        had_explicit_stop=False,
    )


def _match_bare_time(name: str, now_utc: datetime):
    """Bare 'H:MMam/pm' at end of name (World Cup). No date — assume
    today ET; bump to tomorrow if inferred time is too far in the past."""
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AaPp][Mm])\s*$", name)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2))
    ap = m.group(3).lower()
    if ap == "pm" and hour < 12:
        hour += 12
    elif ap == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    now_et = now_utc.astimezone(ET)
    candidate = datetime(now_et.year, now_et.month, now_et.day, hour, minute, 0)
    candidate_utc = _to_utc(candidate, ET)
    if (now_utc - candidate_utc) > timedelta(hours=BARE_TIME_PAST_BUMP_HOURS):
        candidate_utc = candidate_utc + timedelta(days=1)
    return ParsedEvent(
        title=_strip_match_and_clean(name, m),
        start_utc=candidate_utc,
        stop_utc=candidate_utc + timedelta(minutes=DEFAULT_DURATION_MIN),
        had_explicit_stop=False,
    )


# Order matters: most specific format first, most ambiguous last.
_MATCHERS = [
    _match_mlb_start_stop,
    _match_paren_iso,
    _match_weekday_date_tz,
    _match_at_datetime,
    _match_bare_time,
]


def parse_event(name: str, now_utc: datetime) -> ParsedEvent | None:
    """Return a ParsedEvent if the channel name has a recognizable start
    time; None otherwise (treat as off-air)."""
    if _is_empty_channel(name):
        return None
    for matcher in _MATCHERS:
        try:
            event = matcher(name, now_utc)
        except Exception:
            continue
        if event is not None:
            return event
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

        event = parse_event(name, now_utc)

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
        f'<tv generator-info-name="iptv-epg-generator" date="{now_utc.strftime("%Y%m%d%H%M%S %z")}">',
    ]

    # Channels first, then programmes — XMLTV convention
    for ch in channels:
        tvg_id = tvg_id_for(ch)
        display = display_name_for(ch)
        lines.append(f'  <channel id="{escape(tvg_id, {chr(34): "&quot;"})}">')
        # IPTVEditor only fuzzy-matches against the first <display-name>;
        # raw provider name goes first so unrenamed playlist channels hit
        # an exact match. Short name kept as a secondary for any consumer
        # that reads multiple display-names.
        if ch.raw_name and ch.raw_name != display:
            lines.append(f'    <display-name>{escape(ch.raw_name)}</display-name>')
        lines.append(f'    <display-name>{escape(display)}</display-name>')
        lines.append('  </channel>')

    # Coverage window: 24 hours backward through 24 hours forward from now.
    # The backward portion matters because most events listed in the channel
    # name have already started (and possibly already ended) by the time the
    # workflow runs. Without backward coverage, the channel shows "Off Air"
    # even though TiviMate users may still want to see what just played.
    # Off-air filler is emitted *only outside* any real event window, never
    # across the entire 48h block.
    off_air_start = (now_utc - timedelta(hours=24)).replace(minute=0, second=0, microsecond=0)
    off_air_stop = off_air_start + timedelta(hours=48)

    def emit_off_air(start: datetime, stop: datetime, tvg_id_esc: str, cat: str) -> None:
        lines.append(
            f'  <programme start="{xmltv_time(start)}" '
            f'stop="{xmltv_time(stop)}" '
            f'channel="{tvg_id_esc}">'
        )
        lines.append('    <title>Off Air</title>')
        lines.append(f'    <category>{escape(cat)}</category>')
        lines.append('  </programme>')

    for ch in channels:
        tvg_id = tvg_id_for(ch)
        tvg_id_esc = escape(tvg_id, {chr(34): "&quot;"})

        # Does the event overlap our coverage window? If not, treat as empty.
        event_in_window = (
            ch.event is not None
            and ch.event.stop_utc > off_air_start
            and ch.event.start_utc < off_air_stop
        )

        if not event_in_window:
            emit_off_air(off_air_start, off_air_stop, tvg_id_esc, ch.category_name)
            continue

        ev_start = ch.event.start_utc
        ev_stop = ch.event.stop_utc

        # Pre-event filler
        if ev_start > off_air_start:
            emit_off_air(off_air_start, ev_start, tvg_id_esc, ch.category_name)

        # The event itself
        lines.append(
            f'  <programme start="{xmltv_time(ev_start)}" '
            f'stop="{xmltv_time(ev_stop)}" '
            f'channel="{tvg_id_esc}">'
        )
        lines.append(f'    <title>{escape(ch.event.title)}</title>')
        lines.append(f'    <category>{escape(ch.category_name)}</category>')
        # Always show the raw channel name in the desc popup so the operator
        # can see exactly what the parser saw, even when title was cleaned.
        # Prepend an "Estimated end time" note when stop was a guess.
        desc = ch.raw_name
        if not ch.event.had_explicit_stop:
            desc = f"Estimated end time. Source: {desc}"
        lines.append(f'    <desc>{escape(desc)}</desc>')
        lines.append('  </programme>')

        # Post-event filler
        if ev_stop < off_air_stop:
            emit_off_air(ev_stop, off_air_stop, tvg_id_esc, ch.category_name)

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
