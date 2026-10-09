#!/usr/bin/env python3
"""
marketbeat-volume.py -- Unusual stock-volume leaderboard from MarketBeat.

Scrapes https://www.marketbeat.com/market-data/unusual-volume-stocks/
(keyless, server-rendered table with data-clean attributes) and writes
the leaderboard as JSON.

Pairs with the unusual-options flow: a name flagging on BOTH unusual
stock volume and unusual call volume is a stronger signal than either
alone.

Caveat: single-operator site, page structure may change. Best-effort
scrape -- if the table structure changes, this script fails loud
(empty output + error) rather than writing stale data.

Outputs (under hidden_files/marketbeat-volume/):
- marketbeat-volume-latest.json
- marketbeat-volume-history.jsonl
- README.md
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

URL = "https://www.marketbeat.com/market-data/unusual-volume-stocks/"
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)  # repo root when run from scripts/ on GitHub Actions
OUT_DIR = os.path.join(
    REPO if os.path.isdir(os.path.join(REPO, "hidden_files")) else os.path.expanduser("~/workspace/goals/alpaca-paper-trading-simulator"),
    "hidden_files", "marketbeat-volume"
)
HEADERS = {"User-Agent": "Mozilla/5.0"}
LA = ZoneInfo("America/Los_Angeles")


def fetch():
    req = urllib.request.Request(URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(html):
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
    out = []
    for r in rows:
        cells = re.findall(r'data-clean="([^"]+)"', r)
        if len(cells) < 2:
            continue
        sym_co = cells[0].split("|")
        price_chg = cells[1].split("|")
        if len(sym_co) < 2 or sym_co[0].lower() == "symbol":
            continue
        entry = {"symbol": sym_co[0].strip(), "company": sym_co[1].strip()}
        if len(price_chg) >= 1:
            entry["price"] = price_chg[0].strip()
        if len(price_chg) >= 2:
            entry["change_pct"] = price_chg[1].strip()
        # extra columns if present (volume, avg volume, ratio)
        for c in cells[2:]:
            parts = c.split("|")
            if len(parts) == 2:
                key = parts[0].strip().lower().replace(" ", "_")
                entry[key] = parts[1].strip()
        out.append(entry)
    return out


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    now_la = now_utc.astimezone(LA)
    try:
        html = fetch()
    except Exception as e:
        print(f"FETCH FAILED: {type(e).__name__}: {str(e)[:120]}")
        return 1
    leaders = parse(html)
    if not leaders:
        print("PARSE FAILED: no table rows extracted (page structure changed?)")
        return 1

    out = {
        "pulled_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pulled_at_pt": now_la.strftime("%Y-%m-%d %H:%M %Z"),
        "source": "marketbeat.com/market-data/unusual-volume-stocks (keyless scrape)",
        "count": len(leaders),
        "leaders": leaders,
    }
    with open(os.path.join(OUT_DIR, "marketbeat-volume-latest.json"), "w") as f:
        json.dump(out, f, indent=2)
    with open(os.path.join(OUT_DIR, "marketbeat-volume-history.jsonl"), "a") as f:
        f.write(json.dumps({
            "date": now_la.strftime("%Y-%m-%d"),
            "count": len(leaders),
            "symbols": [l["symbol"] for l in leaders],
        }) + "\n")
    print(f"wrote marketbeat-volume-latest.json ({len(leaders)} names)")
    for l in leaders[:10]:
        print(f"  {l['symbol']} {l.get('price','')} {l.get('change_pct','')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
