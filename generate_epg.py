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

# Category IDs we care about. Each entry's `name` shows up in the EPG <category>
# tag. Optional per-category overrides:
#   duration_min: how long to assume events run (defaults to DEFAULT_DURATION_MIN)
#   force_default_duration: if True, ignore any explicit stop time from the
#       provider and always use duration_min. Useful when the provider's stop
#       times are unreliable (MLB stop times can be 7+ hours, way too long).
#
# To add a category to the live runs, also include its id on the workflow's
# --categories flag in .github/workflows/regenerate-epg.yml.
CATEGORIES = {
    "911":  {"name": "ESPN+",     "duration_min": 180},  # tennis/soccer ~3hr
    "1960": {"name": "ESPN+ VIP", "duration_min": 180},
    "606":  {"name": "MLB",       "duration_min": 180, "force_default_duration": True},
    "1185": {"name": "MLB Team",  "duration_min": 180, "force_default_duration": True},
    "597":  {"name": "NFL",       "duration_min": 210},  # football ~3.5hr
    "2354": {"name": "World Cup", "duration_min": 150},  # soccer ~2.5hr
    "1882": {"name": "FIFA+",     "duration_min": 150},  # soccer ~2.5hr
}

# Skip channels whose name looks like a category header/separator
HEADER_RE = re.compile(r"^\s*#{2,}")

# Drop events that ended more than this many hours ago (avoids stale
# entries from prior games showing up as "current" to TiviMate)
STALE_CUTOFF_HOURS = 24

# Default UA — mimics VLC, since raw curl/python UAs may get 884'd
DEFAULT_UA = "VLC/3.0.20 LibVLC/3.0.20"

# How long to assume an event runs when only a start time is given.
# Per-category overrides in CATEGORIES take precedence.
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
    raw_name: str               # original channel name from provider, unmodified
    category_id: str
    category_name: str          # human-friendly: "ESPN+"
    epg_channel_id: str         # provider-set EPG id, empty for event channels
    event: ParsedEvent | None   # None if we couldn't parse an event
    # IPTVEditor-derived fields (None if not found in IPTVEditor M3U):
    cuid: str | None = None     # IPTVEditor's internal channel ID (e.g., "41711")
    iptveditor_name: str | None = None  # current display name in IPTVEditor M3U
                                         # (reflects any renames the user has applied)


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


def _match_mlb_start_stop(name: str, now_utc: datetime, category_meta: dict | None = None):
    """MLB-style: '... start:YYYY-MM-DD HH:MM:SS stop:YYYY-MM-DD HH:MM:SS'.

    Timestamps are labeled as if UTC by the provider, but empirically the
    provider doesn't apply DST adjustments — they always treat Eastern as
    UTC-5 (EST) even during daylight saving. So during DST months the
    timestamp is 1 hour too late. We detect DST from the timestamp itself
    (by asking America/New_York if it's in DST on that date) and subtract
    1 hour when needed.

    Stop times from the provider are unreliable (often 7+ hours after start),
    so when category_meta requests force_default_duration, we ignore the
    explicit stop and use category duration_min instead.
    """
    m = re.search(
        r"\s*start:(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"
        r"\s+stop:(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
        name,
    )
    if not m:
        return None
    try:
        start_naive = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        stop_naive = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

    # Convert provider's "UTC" to actual UTC.
    # Their timestamp = local Eastern time + 5h (assuming EST year-round).
    # In summer (EDT), real UTC = their timestamp − 1h.
    # In winter (EST), real UTC = their timestamp (no adjustment).
    start_utc = start_naive.replace(tzinfo=UTC)
    if _is_eastern_dst(start_naive):
        start_utc -= timedelta(hours=1)

    # Stop time: either use category's duration_min override, or compute
    # from the provider's stop value (with the same DST correction).
    meta = category_meta or {}
    if meta.get("force_default_duration"):
        duration = timedelta(minutes=meta.get("duration_min", DEFAULT_DURATION_MIN))
        stop_utc = start_utc + duration
        had_explicit_stop = False
    else:
        stop_utc = stop_naive.replace(tzinfo=UTC)
        if _is_eastern_dst(stop_naive):
            stop_utc -= timedelta(hours=1)
        had_explicit_stop = True

    return ParsedEvent(
        title=_strip_match_and_clean(name, m),
        start_utc=start_utc,
        stop_utc=stop_utc,
        had_explicit_stop=had_explicit_stop,
    )


def _is_eastern_dst(naive_dt: datetime) -> bool:
    """True if the given naive datetime falls within daylight saving time
    in the America/New_York zone. Used for fixing provider timestamps that
    are EST-frozen (don't handle DST)."""
    # Attach ET and ask what offset is in effect at that local time.
    # During EDT, dst() returns 1 hour; during EST it returns 0.
    return naive_dt.replace(tzinfo=ET).dst() != timedelta(0)


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


def parse_event(name: str, now_utc: datetime, category_meta: dict | None = None) -> ParsedEvent | None:
    """Return a ParsedEvent if the channel name has a recognizable start
    time; None otherwise (treat as off-air).

    category_meta may carry per-category overrides (e.g., force_default_duration
    for MLB to ignore the provider's unreliable stop times). Only matchers
    that need it consume it; others ignore the kwarg silently.
    """
    if _is_empty_channel(name):
        return None
    for matcher in _MATCHERS:
        try:
            # Some matchers accept category_meta as a 3rd arg; others don't.
            # Detect via signature inspection avoided for simplicity — we
            # just pass it positionally to matchers that accept it.
            if matcher is _match_mlb_start_stop:
                event = matcher(name, now_utc, category_meta)
            else:
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


# ---------- IPTVEditor integration ----------
#
# IPTVEditor publishes the user's curated playlist as an M3U at a URL like
# https://opop.pro/<token>. Each #EXTINF line carries the IPTVEditor CUID
# (a stable per-channel identifier IPTVEditor assigns when it ingests the
# provider's playlist) and the channel's current display name (which
# reflects any renames the user has applied in IPTVEditor).
#
# We download this M3U and join its CUID + display-name onto our provider-
# sourced channels via the stream_id, which appears in both the URL (as the
# .ts filename) and the provider's API response. CUID becomes the XMLTV
# <channel id>; IPTVEditor's display name becomes the <display-name> element.
# When both are correct, IPTVEditor's auto-EPG-search can find the match.

# Example #EXTINF line we parse:
# #EXTINF:0 CUID="41711" tvg-name="ESPN+ 001" tvg-id="41711" tvg-logo="..."
#   group-title="ESPN+ PPV",ESPN+ 001
# http://server/live/USER/PASS/1601718.ts
M3U_CUID_RE = re.compile(r'CUID="([^"]+)"')
M3U_NAME_RE = re.compile(r',([^\n]+)$')  # text after the last comma on the EXTINF line
M3U_URL_STREAM_ID_RE = re.compile(r'/(\d+)\.\w+\s*$')


def fetch_iptveditor_m3u(url: str, user_agent: str) -> str:
    """Download the IPTVEditor-curated M3U as text."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(req, timeout=60) as resp:
        # IPTVEditor M3U files can be a few MB; read whole thing.
        return resp.read().decode("utf-8", errors="replace")


def parse_iptveditor_m3u(text: str) -> dict[int, dict[str, str]]:
    """
    Parse the IPTVEditor M3U and return a dict keyed by stream_id with
    {cuid, name} for each channel. stream_id is extracted from the URL line.

    Returns:
        {1601718: {"cuid": "41711", "name": "ESPN+ 001"}, ...}
    """
    result: dict[int, dict[str, str]] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("#EXTINF"):
            i += 1
            continue

        # Find this channel's URL on a following line (skip any #EXTVLCOPT
        # or other metadata directives that might sit between EXTINF and URL).
        url_line = None
        j = i + 1
        while j < len(lines):
            candidate = lines[j].strip()
            if not candidate:
                j += 1
                continue
            if candidate.startswith("#"):
                j += 1
                continue
            url_line = candidate
            break

        if url_line is None:
            i += 1
            continue

        cuid_m = M3U_CUID_RE.search(line)
        name_m = M3U_NAME_RE.search(line)
        stream_m = M3U_URL_STREAM_ID_RE.search(url_line)

        if cuid_m and stream_m:
            stream_id = int(stream_m.group(1))
            name = name_m.group(1).strip() if name_m else ""
            result[stream_id] = {
                "cuid": cuid_m.group(1),
                "name": name,
            }

        i = j + 1  # advance past the URL line

    return result


# ---------- Channel processing ----------

def process_streams(streams: list[dict], category_id: str, category_meta: dict,
                    now_utc: datetime,
                    iptveditor_by_stream_id: dict[int, dict[str, str]] | None = None
                    ) -> list[Channel]:
    """Turn raw API stream dicts into Channel objects with parsed events.

    If iptveditor_by_stream_id is provided, each Channel gets its IPTVEditor
    CUID and current display name attached (joined by stream_id). Channels
    missing from the IPTVEditor map (e.g., hidden in IPTVEditor) get None
    for those fields and will be emitted without a CUID-keyed channel id —
    matching won't work for them, which is correct: a hidden channel
    shouldn't get EPG data either.
    """
    channels = []
    stale_cutoff = now_utc - timedelta(hours=STALE_CUTOFF_HOURS)
    ie_map = iptveditor_by_stream_id or {}

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

        event = parse_event(name, now_utc, category_meta)

        # Apply per-category duration override after parsing. For categories
        # configured with force_default_duration, recompute stop_utc from
        # start_utc + duration_min — this handles non-MLB categories
        # uniformly (the MLB matcher itself also respects the override for
        # symmetry, but this catches any future categories that opt in).
        if event and category_meta.get("force_default_duration") and event.had_explicit_stop:
            duration = timedelta(minutes=category_meta.get("duration_min", DEFAULT_DURATION_MIN))
            event.stop_utc = event.start_utc + duration
            event.had_explicit_stop = False

        # Skip stale events
        if event and event.stop_utc < stale_cutoff:
            continue

        stream_id = int(s["stream_id"])
        ie_meta = ie_map.get(stream_id)

        channels.append(Channel(
            stream_id=stream_id,
            raw_name=name,
            category_id=category_id,
            category_name=category_meta["name"],
            epg_channel_id=epg_id,
            event=event,
            cuid=ie_meta["cuid"] if ie_meta else None,
            iptveditor_name=ie_meta["name"] if ie_meta else None,
        ))
    return channels


# ---------- XMLTV generation ----------

def xmltv_time(dt: datetime) -> str:
    """XMLTV format: 'YYYYMMDDhhmmss +0000'."""
    return dt.strftime("%Y%m%d%H%M%S %z")


def tvg_id_for(channel: Channel) -> str:
    """
    The XMLTV <channel id> for this channel. When the channel was joined
    to an IPTVEditor entry, we use its CUID — IPTVEditor's auto-match writes
    this exact value into the playlist's tvg-id when it finds a display-name
    match, so using the CUID is what closes the loop.

    Fallback: stream_id-based ID. Stable but won't auto-match in IPTVEditor.
    """
    if channel.cuid:
        return channel.cuid
    slug = channel.category_name.lower().replace(" ", "-").replace("+", "plus")
    return f"iptv-{slug}-{channel.stream_id}"


def display_name_for(channel: Channel) -> str:
    """
    The XMLTV <display-name>. IPTVEditor's auto-match uses substring/fuzzy
    matching against the current channel name in IPTVEditor — so we want
    this to be exactly the name IPTVEditor currently has (which reflects
    whatever bulk-rename the user has applied). If we don't have IPTVEditor
    data, fall back to a category-aware short form derived from the raw
    provider name.
    """
    if channel.iptveditor_name:
        return channel.iptveditor_name

    # Fallback: derive a short, distinctive label from the raw provider name.
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
    # Last resort: use the raw name up to the first pipe
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
    ap.add_argument("--iptveditor-m3u-url",
                    help="URL of the IPTVEditor-published M3U for this playlist. When supplied, the "
                         "EPG uses IPTVEditor's CUID as each <channel id> and IPTVEditor's current "
                         "channel name as each <display-name>, so IPTVEditor's auto-EPG-search can "
                         "find matches. Also reads IPTVEDITOR_M3U_URL from the env file.")
    ap.add_argument("--iptveditor-m3u-file",
                    help="Path to a saved IPTVEditor M3U file (for testing without re-fetching).")
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
        if not args.iptveditor_m3u_url:
            args.iptveditor_m3u_url = env.get("IPTVEDITOR_M3U_URL")

    now_utc = datetime.now(timezone.utc)
    cache_dir = Path(args.cache_dir)

    # Optionally pull IPTVEditor's M3U so we can join CUIDs onto channels.
    # Without this, the EPG falls back to stream_id-based ids (which won't
    # auto-match in IPTVEditor) but is otherwise functional.
    iptveditor_map: dict[int, dict[str, str]] = {}
    if args.iptveditor_m3u_file:
        try:
            m3u_text = Path(args.iptveditor_m3u_file).read_text(encoding="utf-8", errors="replace")
            iptveditor_map = parse_iptveditor_m3u(m3u_text)
            print(f"Loaded IPTVEditor M3U from {args.iptveditor_m3u_file} "
                  f"({len(iptveditor_map)} channels with CUIDs)", file=sys.stderr)
        except Exception as e:
            print(f"WARN: couldn't load IPTVEditor M3U from {args.iptveditor_m3u_file}: {e}",
                  file=sys.stderr)
    elif args.iptveditor_m3u_url:
        try:
            m3u_text = fetch_iptveditor_m3u(args.iptveditor_m3u_url, args.user_agent)
            iptveditor_map = parse_iptveditor_m3u(m3u_text)
            print(f"Fetched IPTVEditor M3U: {len(iptveditor_map)} channels with CUIDs",
                  file=sys.stderr)
            if args.save_cache:
                cache_dir.mkdir(parents=True, exist_ok=True)
                (cache_dir / "iptveditor.m3u").write_text(m3u_text, encoding="utf-8")
        except Exception as e:
            print(f"WARN: IPTVEditor M3U fetch failed ({e}); proceeding without CUIDs",
                  file=sys.stderr)

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

        channels = process_streams(streams, cat_id, meta, now_utc, iptveditor_map)
        joined = sum(1 for c in channels if c.cuid)
        with_events = sum(1 for c in channels if c.event)
        all_channels.extend(channels)
        print(f"  -> {len(channels)} usable channels "
              f"({with_events} with events, {joined} joined to IPTVEditor CUIDs)",
              file=sys.stderr)

    xml = build_xmltv(all_channels, now_utc)
    Path(args.output).write_text(xml, encoding="utf-8")
    print(f"Wrote {len(all_channels)} channels to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
