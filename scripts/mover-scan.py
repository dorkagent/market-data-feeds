#!/usr/bin/env python3
"""Morning mover scan — discovery feed for the tradebot.

Finds what's actually moving TODAY, beyond the fixed watchlist.
Feeds Setup A (news-continuation), Setup B (vwap-reversion long),
and Setup C (short-breakdown) with fresh candidates.

Universe: US-listed, no penny stocks (< $5/share).
Filters: top % gainers AND losers by price move. Premarket run, so no
dollar-volume filter — Nasdaq premarket volume is too thin for it and
it was zeroing out every run. Volume confirmation happens at the open.

Source: Nasdaq API (same as earnings-calendar.py) + Finnhub for market cap.
Runs: premarket, ~6:00 AM PT, before the premarket scan.
"""

import json, os, sys, time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "hidden_files", "mover-scan")
os.makedirs(OUT_DIR, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

def fetch(url, timeout=20):
    req = Request(url, headers=UA)
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def main():
    movers = []
    # Nasdaq screener API. NOTE (Oct 14): response nests rows under
    # data.table.rows (not data.rows), and no longer returns a volume
    # column — screen on % move + price only.
    try:
        data = fetch("https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=100&offset=0&order=desc&sort=volume&assetclass=stocks")
        rows = data.get("data", {}).get("table", {}).get("rows", [])
    except Exception as e:
        print(f"Nasdaq screener failed: {e}", file=sys.stderr)
        rows = []
    for r in rows:
        try:
            sym = r["symbol"].strip()
            pct = float(r.get("pctchange", "0").replace("%", ""))
            price = float(r.get("lastsale", "$0").replace("$", "").replace(",", ""))
            mcap = r.get("marketCap", "0")
            # Filters: no penny stocks, meaningful % move. No volume filter —
            # premarket volume is too thin and the API dropped the column.
            if price < 5:
                continue
            if abs(pct) < 3:  # at least 3% move
                continue
            movers.append({
                "symbol": sym,
                "pct_change": round(pct, 1),
                "price": round(price, 2),
                "market_cap": mcap,
                "direction": "up" if pct > 0 else "down",
            })
        except (ValueError, KeyError, AttributeError):
            continue

    # Dedupe, sort by absolute % change
    seen = {}
    for m in movers:
        if m["symbol"] not in seen or abs(m["pct_change"]) > abs(seen[m["symbol"]]["pct_change"]):
            seen[m["symbol"]] = m
    movers = sorted(seen.values(), key=lambda x: abs(x["pct_change"]), reverse=True)[:20]

    # Split for setup routing
    up = [m for m in movers if m["direction"] == "up"]
    down = [m for m in movers if m["direction"] == "down"]

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe": "US-listed, price >= $5, |move| >= 3% (premarket: no volume filter)",
        "top_movers": movers,
        "setup_routing": {
            "setup_a_candidates": [m["symbol"] for m in up[:10]],
            "setup_b_candidates": [m["symbol"] for m in up[:10]],
            "setup_c_candidates": [m["symbol"] for m in down[:10]],
        },
        "count": len(movers),
    }

    path = os.path.join(OUT_DIR, "movers-latest.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=1)

    # Compact stdout for the cron
    print(f"Mover scan: {len(movers)} names ({len(up)} up / {len(down)} down)")
    for m in movers[:10]:
        arrow = "▲" if m["direction"] == "up" else "▼"
        print(f"  {arrow} {m['symbol']:6s} {m['pct_change']:+6.1f}%  ${m['price']:.2f}")

if __name__ == "__main__":
    main()
