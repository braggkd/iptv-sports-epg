#!/usr/bin/env python3
"""
Generate an XMLTV EPG file for NoBuffr (or any player that fetches its
playlist directly from the provider's Xtream API rather than through
IPTVEditor).

Differs from generate_epg.py in three ways:
  1. No IPTVEditor M3U fetch — this EPG stands alone.
  2. tvg-id is derived from the provider's stream_id (iptv-espnplus-1601718)
     rather than IPTVEditor's CUID. Matches what the provider's own M3U
     could carry as tvg-id — if it doesn't, most players will fall back
     to name matching against the raw provider channel name.
  3. <display-name> is the RAW provider channel name (unchanged), so
     name-based matching against the provider's M3U works out of the box.

Everything else — the parser, DST fix, per-category durations, coverage
window, off-air filler — is imported from generate_epg.py so we don't
maintain two copies of the tricky logic.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

# Import from the main script so we don't duplicate parser/CATEGORIES/etc.
# This means both EPGs benefit from any parser improvements.
import generate_epg as gg


def build_nobuffr_xmltv(channels: list[gg.Channel], now_utc: datetime) -> str:
    """Build XMLTV using stream_id-based tvg-ids and raw display-names.

    Copies most of gg.build_xmltv but overrides the tvg-id and display-name
    strategy for NoBuffr's use case (no IPTVEditor CUIDs available).
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE tv SYSTEM "xmltv.dtd">',
        f'<tv generator-info-name="nobuffr-epg-generator" '
        f'date="{now_utc.strftime("%Y%m%d%H%M%S %z")}">',
    ]

    # Channels first (XMLTV convention)
    for ch in channels:
        tvg_id = _nobuffr_tvg_id(ch)
        lines.append(f'  <channel id="{escape(tvg_id, {chr(34): "&quot;"})}">')
        # Raw provider name as display-name — that's what NoBuffr will see
        # in its own channel list.
        lines.append(f'    <display-name>{escape(ch.raw_name)}</display-name>')
        lines.append('  </channel>')

    # Coverage window: 24h back through 24h forward from now, same as main
    # script — recently-ended events stay visible in the guide.
    off_air_start = (now_utc - gg.timedelta(hours=24)).replace(
        minute=0, second=0, microsecond=0
    )
    off_air_stop = off_air_start + gg.timedelta(hours=48)

    for ch in channels:
        tvg_id = _nobuffr_tvg_id(ch)
        if ch.event:
            # Real event programme
            if off_air_start < ch.event.start_utc:
                lines.append(
                    f'  <programme start="{gg.xmltv_time(off_air_start)}" '
                    f'stop="{gg.xmltv_time(ch.event.start_utc)}" '
                    f'channel="{escape(tvg_id, {chr(34): "&quot;"})}">'
                )
                lines.append('    <title>Off Air</title>')
                lines.append(f'    <category>{escape(ch.category_name)}</category>')
                lines.append('  </programme>')

            lines.append(
                f'  <programme start="{gg.xmltv_time(ch.event.start_utc)}" '
                f'stop="{gg.xmltv_time(ch.event.stop_utc)}" '
                f'channel="{escape(tvg_id, {chr(34): "&quot;"})}">'
            )
            lines.append(f'    <title>{escape(ch.event.title)}</title>')
            lines.append(f'    <category>{escape(ch.category_name)}</category>')
            desc = ch.raw_name
            if not ch.event.had_explicit_stop:
                desc = f"Estimated end time. Source: {desc}"
            lines.append(f'    <desc>{escape(desc)}</desc>')
            lines.append('  </programme>')

            if ch.event.stop_utc < off_air_stop:
                lines.append(
                    f'  <programme start="{gg.xmltv_time(ch.event.stop_utc)}" '
                    f'stop="{gg.xmltv_time(off_air_stop)}" '
                    f'channel="{escape(tvg_id, {chr(34): "&quot;"})}">'
                )
                lines.append('    <title>Off Air</title>')
                lines.append(f'    <category>{escape(ch.category_name)}</category>')
                lines.append('  </programme>')
        else:
            # No parseable event — one big Off Air block covers the window
            lines.append(
                f'  <programme start="{gg.xmltv_time(off_air_start)}" '
                f'stop="{gg.xmltv_time(off_air_stop)}" '
                f'channel="{escape(tvg_id, {chr(34): "&quot;"})}">'
            )
            lines.append('    <title>Off Air</title>')
            lines.append(f'    <category>{escape(ch.category_name)}</category>')
            lines.append('  </programme>')

    lines.append('</tv>')
    return "\n".join(lines)


def _nobuffr_tvg_id(channel: gg.Channel) -> str:
    """iptv-{slug}-{stream_id}. Same format we used before we added CUIDs
    for IPTVEditor. Stable across runs (stream_id is stable on the provider
    side)."""
    slug = channel.category_name.lower().replace(" ", "-").replace("+", "plus")
    return f"iptv-{slug}-{channel.stream_id}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url")
    ap.add_argument("--username")
    ap.add_argument("--password")
    ap.add_argument("--user-agent", default=gg.DEFAULT_UA)
    ap.add_argument("--output", default="nobuffr_epg.xml")
    ap.add_argument("--categories", nargs="+",
                    help="Category IDs to include (default: 911 for ESPN+ only)")
    ap.add_argument("--env-file", default="provider.env")
    ap.add_argument("--from-files", action="store_true",
                    help="Load streams from cached JSON instead of fetching")
    ap.add_argument("--cache-dir", default=".",
                    help="Directory holding {category_id}.json for --from-files")
    args = ap.parse_args(argv)

    # Load creds from env file if not on CLI
    env_path = Path(args.env_file)
    if env_path.is_file():
        env = gg.load_env_file(env_path)
        if not args.base_url:
            args.base_url = env.get("BASE_URL")
        if not args.username:
            args.username = env.get("USERNAME")
        if not args.password:
            args.password = env.get("PASSWORD")

    # Default to ESPN+ only for NoBuffr (per user's initial ask)
    category_ids = args.categories or ["911"]

    now_utc = datetime.now(timezone.utc)
    all_channels: list[gg.Channel] = []
    cache_dir = Path(args.cache_dir)

    for cat_id in category_ids:
        meta = gg.CATEGORIES.get(cat_id)
        if not meta:
            print(f"WARN: category {cat_id} not in CATEGORIES config, "
                  f"using default settings", file=sys.stderr)
            meta = {"name": f"Category {cat_id}"}

        if args.from_files:
            candidates = [f"{cat_id}.json"]
            fpath = None
            for c in candidates:
                if (cache_dir / c).exists():
                    fpath = cache_dir / c
                    break
            if not fpath:
                print(f"WARN: no cache file for {cat_id}", file=sys.stderr)
                continue
            streams = gg.load_streams_from_file(fpath)
        else:
            if not (args.base_url and args.username and args.password):
                print("ERROR: --base-url / --username / --password required",
                      file=sys.stderr)
                return 2
            try:
                streams = gg.fetch_streams(
                    args.base_url, args.username, args.password,
                    cat_id, args.user_agent,
                )
            except Exception as e:
                print(f"ERROR fetching category {cat_id}: {e}", file=sys.stderr)
                continue

        # Process streams WITHOUT IPTVEditor mapping — that's the whole point.
        channels = gg.process_streams(streams, cat_id, meta, now_utc, None)
        with_events = sum(1 for c in channels if c.event)
        all_channels.extend(channels)
        print(f"Category {cat_id} ({meta['name']}): "
              f"{len(channels)} channels, {with_events} with events",
              file=sys.stderr)

    xml = build_nobuffr_xmltv(all_channels, now_utc)
    Path(args.output).write_text(xml, encoding="utf-8")
    print(f"Wrote {len(all_channels)} channels to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
