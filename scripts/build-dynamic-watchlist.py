#!/usr/bin/env python3
"""Build the day's dynamic gapper watchlist for the paper-trading scanner.

Feeder: stockanalysis.com premarket movers (same page family as the
missed-signal audit). Filters out penny pumps and illiquid names, writes
the survivors to hidden_files/dynamic-watchlist/YYYY-MM-DD.json, which
bin/signal-screen.py appends to its stock universe for the day.

Filters (all must pass):
  - price >= $10
  - market cap >= $1B
  - premarket volume >= 1M shares
  - not already in the scanner's core universe, not RMD (track-only, never traded)

Cap: top 5 by % gain. Entries on dynamic names follow the standard
catalyst + confirmed-momentum rules; max 5% equity per dynamic name;
variant kill criterion lives in strategy/evolution.md.
"""
import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)  # repo root when run from scripts/ on GitHub Actions
GOAL = os.path.expanduser("~/workspace/goals/alpaca-paper-trading-simulator")
# Prefer repo-relative layout when running under GitHub Actions.
if os.path.isdir(os.path.join(REPO, "hidden_files")):
    GOAL = REPO
OUT_DIR = os.path.join(GOAL, "hidden_files", "dynamic-watchlist")

CORE = {"SPY", "QQQ", "NVDA", "AAPL", "MSFT"}
NEVER = {"RMD"}
URL = "https://stockanalysis.com/markets/premarket/"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
MAX_NAMES = 5
PT = ZoneInfo("America/Los_Angeles")


def parse_mcap(s):
    m = re.match(r"\$?([\d.]+)\s*([KMBT])", s.strip().upper())
    if not m:
        return 0.0
    val, suf = float(m.group(1)), m.group(2)
    return val * {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[suf]


def fetch():
    import urllib.request
    req = urllib.request.Request(URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        html = r.read().decode("utf-8", "replace")
    tables = re.findall(r"<table.*?</table>", html, re.S)
    if not tables:
        raise RuntimeError("no tables on premarket movers page")
    rows = re.findall(r"<tr.*?</tr>", tables[0], re.S)
    out = []
    for row in rows[1:]:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)
        txt = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        if len(txt) < 7:
            continue
        try:
            out.append({
                "ticker": txt[1],
                "name": txt[2],
                "pct": float(txt[3].replace("%", "").replace("+", "")),
                "price": float(txt[4].replace("$", "").replace(",", "")),
                "volume": int(txt[5].replace(",", "")),
                "mcap": txt[6],
                "mcap_usd": parse_mcap(txt[6]),
            })
        except (ValueError, IndexError):
            continue
    return out


def main():
    now = datetime.now(PT)
    today = now.strftime("%Y-%m-%d")
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"{today}.json")

    movers = fetch()
    picked = []
    for m in sorted(movers, key=lambda x: -x["pct"]):
        t = m["ticker"]
        if t in CORE or t in NEVER:
            continue
        if m["price"] < 10 or m["mcap_usd"] < 1e9 or m["volume"] < 1_000_000:
            continue
        picked.append({k: m[k] for k in ("ticker", "name", "pct", "price", "volume", "mcap")})
        if len(picked) >= MAX_NAMES:
            break

    with open(out_path, "w") as f:
        json.dump({"date": today, "built_at": now.isoformat(),
                   "source": URL, "names": picked}, f, indent=2)
    print(f"DYNAMIC-WATCHLIST {today}: {len(picked)} names "
          f"({', '.join(p['ticker'] for p in picked) or 'none'}) -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
