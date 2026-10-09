#!/usr/bin/env python3
"""earnings-calendar.py -- Earnings calendar feed (keyless).

Pulls the Nasdaq earnings calendar (free, no key; requires a browser
User-Agent header) for today + the next 5 trading days. Used by the
paper-trading premarket scan to know which names report before the open
or after the close, so positions are not carried blindly into prints.

Source: https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD
  Per-row fields: symbol, name, time (time-pre-market / time-after-hours /
  time-during-market), marketCap, fiscalQuarterEnding, epsForecast, noOfEsts.

Outputs (under hidden_files/earnings/):
- earnings-latest.json     all earnings in the 6-trading-day window:
                           symbol, company name, date, report time
                           (before/after market), consensus EPS, number of
                           estimates, market cap
- earnings-history.jsonl   one compact line per run (30-day rolling window)

Context layer only: "informs, never triggers trades alone."

Stdlib + requests only.
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

UA = {"User-Agent": "Mozilla/5.0 (paper-trading research)"}
BASE = "https://api.nasdaq.com/api/calendar/earnings"

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "hidden_files", "earnings")
LATEST = os.path.join(OUT_DIR, "earnings-latest.json")
HISTORY = os.path.join(OUT_DIR, "earnings-history.jsonl")

TRADING_DAYS = 6  # today + next 5 trading days
HISTORY_KEEP_DAYS = 30

TIME_LABEL = {
    "time-pre-market": "before",
    "time-after-hours": "after",
    "time-during-market": "during",
}


def trading_days(n):
    """Next n trading days starting today (skips Sat/Sun; not holidays)."""
    out, d = [], date.today()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def fetch_day(day, retries=3, timeout=25):
    url = f"{BASE}?date={day.isoformat()}"
    last = None
    for _ in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200:
                data = r.json().get("data") or {}
                return data.get("rows") or []
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001 - network flake
            last = repr(e)
        time.sleep(2)
    raise RuntimeError(f"GET {url} failed: {last}")


def clean_money(s):
    if not s:
        return None
    return s.replace("$", "").replace(",", "").strip() or None


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    errors = []
    days = trading_days(TRADING_DAYS)

    events = []
    for day in days:
        try:
            rows = fetch_day(day)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{day.isoformat()}: {e}")
            continue
        for r in rows:
            sym = (r.get("symbol") or "").strip()
            if not sym:
                continue
            eps = clean_money(r.get("epsForecast"))
            try:
                eps_f = float(eps) if eps not in (None, "--", "n/a") else None
            except ValueError:
                eps_f = None
            try:
                n_est = int(r.get("noOfEsts") or 0)
            except (ValueError, TypeError):
                n_est = 0
            events.append({
                "symbol": sym,
                "name": (r.get("name") or "").strip(),
                "date": day.isoformat(),
                "report_time": TIME_LABEL.get(r.get("time"), r.get("time") or "unknown"),
                "eps_consensus": eps_f,
                "num_estimates": n_est,
                "market_cap": clean_money(r.get("marketCap")),
                "fiscal_quarter_ending": r.get("fiscalQuarterEnding") or None,
            })
    events.sort(key=lambda e: (e["date"], e["symbol"]))

    snapshot = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": f"{days[0].isoformat()} to {days[-1].isoformat()} ({TRADING_DAYS} trading days)",
        "source": "api.nasdaq.com/api/calendar/earnings (keyless, UA header required)",
        "note": ("Context layer: know who reports before/after the market so "
                 "positions are not carried blindly into prints."),
        "errors": errors,
        "events": events,
    }
    with open(LATEST, "w") as f:
        json.dump(snapshot, f, indent=2)

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
        "window": snapshot["window"],
        "event_count": len(events),
        "symbols": sorted({e["symbol"] for e in events}),
        "errors": errors,
    })
    with open(HISTORY, "w") as f:
        for row in kept:
            f.write(json.dumps(row) + "\n")

    print(f"earnings-calendar: {len(events)} earnings in window ({snapshot['window']})")
    for e in events[:15]:
        eps = f"EPS est {e['eps_consensus']}" if e["eps_consensus"] is not None else "no EPS est"
        print(f"  {e['date']} {e['report_time']:>6}: {e['symbol']} ({eps})")
    if len(events) > 15:
        print(f"  ... and {len(events) - 15} more")
    if errors:
        print("ERRORS: " + " | ".join(errors), file=sys.stderr)
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
