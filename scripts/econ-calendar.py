#!/usr/bin/env python3
"""econ-calendar.py -- Economic calendar feed (keyless).

Pulls the weekly economic calendar from Faireconomy (free, no key) and
filters to USD + High impact events: FOMC decisions/minutes, CPI prints,
NFP / jobs reports. These are the events the paper-trading strategy's
30-minute no-trade rule applies to, so this feed exists to make sure the
dates are never missed or misremembered.

Source: https://nfs.faireconomy.media/ff_calendar_thisweek.json
  Fields per event: title, country, date (ISO with tz offset), impact,
  forecast, previous.

Outputs (under hidden_files/econ-calendar/):
- econ-calendar-latest.json   this week's USD High-impact events, datetimes
                              converted to America/Los_Angeles
- econ-calendar-history.jsonl one compact line per run (30-day rolling window
                              kept; older lines pruned)

Context layer only: "informs, never triggers trades alone."

Stdlib + requests only.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
UA = {"User-Agent": "econ-calendar-feed/1.0 (paper-trading research)"}
PT = ZoneInfo("America/Los_Angeles")

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "hidden_files", "econ-calendar")
LATEST = os.path.join(OUT_DIR, "econ-calendar-latest.json")
HISTORY = os.path.join(OUT_DIR, "econ-calendar-history.jsonl")

HISTORY_KEEP_DAYS = 30


def fetch(retries=3, timeout=25):
    last = None
    for _ in range(retries):
        try:
            r = requests.get(URL, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001 - network flake
            last = repr(e)
        time.sleep(2)
    raise RuntimeError(f"GET {URL} failed: {last}")


def to_pt(iso_str):
    """Parse the event's ISO datetime and render in America/Los_Angeles."""
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    pt = dt.astimezone(PT)
    return pt.isoformat(timespec="minutes"), pt.strftime("%a %Y-%m-%d %H:%M PT")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    errors = []

    try:
        raw = fetch()
    except Exception as e:  # noqa: BLE001
        errors.append(str(e))
        raw = []

    events = []
    for e in raw:
        if e.get("country") != "USD" or e.get("impact") != "High":
            continue
        try:
            iso_pt, pretty = to_pt(e["date"])
        except Exception:  # noqa: BLE001 - bad date, skip the row
            errors.append(f"bad date on event: {e.get('title')}")
            continue
        events.append({
            "title": e.get("title", ""),
            "datetime_pt": iso_pt,
            "pretty_pt": pretty,
            "impact": e.get("impact", ""),
            "forecast": e.get("forecast") or None,
            "previous": e.get("previous") or None,
        })
    events.sort(key=lambda x: x["datetime_pt"])

    snapshot = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "nfs.faireconomy.media/ff_calendar_thisweek.json (keyless)",
        "note": ("USD + High impact only. Serves the strategy's 30-minute "
                 "no-trade rule around prints: no tactical entries within "
                 "30 min of FOMC/CPI/jobs releases."),
        "errors": errors,
        "events": events,
    }
    with open(LATEST, "w") as f:
        json.dump(snapshot, f, indent=2)

    # Rolling 30-day history: append today's compact line, prune old ones.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_KEEP_DAYS)).date().isoformat()
    kept = []
    if os.path.exists(HISTORY):
        with open(HISTORY) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("date", "") >= cutoff:
                    kept.append(row)
    kept.append({
        "date": datetime.now(timezone.utc).date().isoformat(),
        "fetched_at_utc": snapshot["fetched_at_utc"],
        "usd_high_count": len(events),
        "titles": [e["title"] for e in events],
        "errors": errors,
    })
    with open(HISTORY, "w") as f:
        for row in kept:
            f.write(json.dumps(row) + "\n")

    print(f"econ-calendar: {len(events)} USD High-impact events this week")
    for e in events:
        fc = f" (fcst {e['forecast']}, prev {e['previous']})" if e["forecast"] or e["previous"] else ""
        print(f"  {e['pretty_pt']}: {e['title']}{fc}")
    if errors:
        print("ERRORS: " + " | ".join(errors), file=sys.stderr)
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
