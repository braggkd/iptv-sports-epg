# iptv-sports-epg

Generates an XMLTV EPG file for IPTV "event per day" sports channels from an Xtream Codes provider that encodes schedule info inside channel names.

Typical use case: your provider gives you ESPN+/MLB/NFL PPV channels named like

```
MLB 1 | Tigers x Reds start:2026-04-24 23:40:00 stop:2026-04-25 06:53:20
US (ESPN+ 001) | Whai vs. Otago Nuggets (Round 3) (2026-04-24 03:30:06)
```

but doesn't expose a real EPG for them. This script parses the name, builds a proper XMLTV file, and emits it so tools like iptveditor.com / TiviMate can consume it.

## Output

`epg.xml` — a plain XMLTV file. Regenerated on each run.

## Quickstart

Requires Python 3.9+ (uses `zoneinfo`, stdlib only, no pip install needed).

1. Create `provider.env` in this directory:

   ```
   BASE_URL=http://your-provider.example.com
   USERNAME=your_xtream_username
   PASSWORD=your_xtream_password
   ```

   Lock it down: `chmod 600 provider.env`.

2. Run:

   ```bash
   python3 generate_epg.py --output epg.xml --save-cache --cache-dir cache
   ```

   CLI args `--base-url`, `--username`, `--password` override values in `provider.env` if supplied.

3. Verify the output is well-formed:

   ```bash
   python3 -c "import xml.etree.ElementTree as ET; ET.parse('epg.xml'); print('ok')"
   ```

## Categories

Configured in the `CATEGORIES` dict at the top of the script. Each entry has:

- `name` — human label used in XMLTV `<category>` and `tvg-id` generation
- `tz` — timezone to interpret bare timestamps in (ESPN+ uses ET, MLB uses UTC in observed data)
- `default_duration_min` — used when a parser finds only a start time

Adjust the category IDs to match your provider.

## Parsers

`PARSERS` is an ordered list; first match wins. Currently:

1. `parse_mlb_style` — matches `| TEAM_A x TEAM_B start:ISO stop:ISO` (both timestamps explicit, UTC)
2. `parse_espn_plus_style` — matches `| EVENT_TITLE (ISO)` (single timestamp in the category's tz, duration estimated)

Add a new parser function and append it to `PARSERS` when new naming formats appear.

## tvg-id scheme

`iptv-{category-slug}-{stream_id}` (e.g. `iptv-espnplus-1601717`). Uses the provider's numeric stream_id so it's stable across runs even as event schedules shift.

## Off-air handling

Channels without a parseable event get a 24-hour "Off Air" programme so the guide always has something to display for them.

## Stale event cutoff

Events whose stop time is more than 24 hours in the past are dropped.

## Scheduling

On macOS, schedule via `launchd` — a `~/Library/LaunchAgents/*.plist` with a `StartInterval` (e.g. 14400 for every 4 hours) or `StartCalendarInterval`. Absolute paths, user-level, redirect stdout/stderr to a log.

## Options

```
--env-file PATH        KEY=VALUE file (default: provider.env)
--base-url URL         Provider base URL (overrides env file)
--username USER        Xtream username (overrides env file)
--password PASS        Xtream password (overrides env file)
--user-agent STRING    HTTP User-Agent (default: VLC/3.0.20 LibVLC/3.0.20)
--output PATH          Where to write XMLTV (default: epg.xml)
--categories IDS...    Only process these category IDs
--from-files           Skip the network; read from --cache-dir
--save-cache           Also write raw API JSON to --cache-dir
--cache-dir DIR        Where to read/write raw JSON dumps (default: .)
```

## License

MIT
