#!/usr/bin/env python3
"""Sector breadth board (keyless).

11 Select Sector SPDRs via the Yahoo Finance unofficial chart API (no key):
1-day % change and 5-day momentum per sector, ranked best to worst, plus a
breadth summary. Answers "is the rally broad or narrow?"

Output:
  hidden_files/sector-board/sector-board-latest.json   (full snapshot)
  hidden_files/sector-board/sector-board-history.jsonl (one compact line per run)

Context layer only: "informs, never triggers trades alone."

Freshness rule: any quote failure -> status "stale", the last good
-latest.json is left untouched, the error is logged. NEVER write fresh
timestamps on stale data.

Stdlib + requests only.
"""

import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests

SECTORS = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLV": "Health Care",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLU": "Utilities",
    "XLP": "Consumer Staples",
    "XLY": "Consumer Discretionary",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
}

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "hidden_files", "sector-board")
LATEST = os.path.join(OUT_DIR, "sector-board-latest.json")
HISTORY = os.path.join(OUT_DIR, "sector-board-history.jsonl")

UA = {"User-Agent": "sector-board-feed/1.0 (paper-trading research)"}


def get_json(url, timeout=25, retries=3):
    last = None
    for _ in range(retries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001 - network flake
            last = repr(e)
        time.sleep(2)
    raise RuntimeError(f"GET {url} failed: {last}")


def sector_quote(symbol):
    """{price, chg_1d_pct, mom_5d_pct, quote_time_utc} from daily bars."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + urllib.parse.quote(symbol, safe="") + "?interval=1d&range=1mo")
    d = get_json(url)
    res = (d.get("chart") or {}).get("result")
    if not res:
        err = (d.get("chart") or {}).get("error") or {}
        raise RuntimeError(f"{symbol}: {err.get('description', 'no result')}")
    meta = res[0].get("meta") or {}
    closes = [c for c in (res[0].get("indicators", {}).get("quote", [{}])[0]
                         .get("close") or []) if c]
    if len(closes) < 6:
        raise RuntimeError(f"{symbol}: fewer than 6 daily closes")
    last, prev, five_ago = closes[-1], closes[-2], closes[-6]
    rmt = meta.get("regularMarketTime")
    quote_time = (datetime.fromtimestamp(rmt, tz=timezone.utc)
                  .isoformat(timespec="seconds") if rmt else None)
    return {
        "name": SECTORS[symbol],
        "price": round(last, 2),
        "chg_1d_pct": round((last - prev) / prev * 100, 2),
        "mom_5d_pct": round((last - five_ago) / five_ago * 100, 2),
        "quote_time_utc": quote_time,
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    errors = []
    rows = {}
    for sym in SECTORS:
        try:
            rows[sym] = sector_quote(sym)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    hist_line = {"t": fetched_at}

    if errors:
        # STALE: never touch the last good file, never mint fresh timestamps.
        hist_line.update({"status": "stale", "errors": errors})
        with open(HISTORY, "a") as f:
            f.write(json.dumps(hist_line) + "\n")
        print("STALE - quote leg failed, last good file untouched", file=sys.stderr)
        for e in errors:
            print("ERROR: " + e, file=sys.stderr)
        return 1

    ranking = sorted(rows.items(), key=lambda kv: kv[1]["chg_1d_pct"],
                     reverse=True)
    adv = sum(1 for r in rows.values() if r["chg_1d_pct"] > 0)
    dec = sum(1 for r in rows.values() if r["chg_1d_pct"] < 0)
    avg_chg = round(sum(r["chg_1d_pct"] for r in rows.values()) / len(rows), 2)
    snapshot = {
        "fetched_at_utc": fetched_at,
        "status": "ok",
        "note": "Context layer - informs, never triggers trades alone. "
                "Answers 'is the rally broad or narrow?' for the premarket scan.",
        "breadth": {
            "advancers": adv,
            "decliners": dec,
            "avg_chg_1d_pct": avg_chg,
            "read": ("broad rally" if adv >= 9 else
                     "broad selloff" if dec >= 9 else
                     "mixed / narrow"),
        },
        "ranking": [
            {"symbol": sym, **data} for sym, data in ranking
        ],
    }
    with open(LATEST, "w") as f:
        json.dump(snapshot, f, indent=2)

    hist_line.update({
        "status": "ok",
        "breadth": snapshot["breadth"],
        "top3": [s for s, _ in ranking[:3]],
        "bottom3": [s for s, _ in ranking[-3:]],
    })
    with open(HISTORY, "a") as f:
        f.write(json.dumps(hist_line) + "\n")

    print(f"breadth: {adv} up / {dec} down, avg {avg_chg:+.2f}% "
          f"({snapshot['breadth']['read']})")
    for sym, data in ranking:
        print(f"{sym:5s} {data['name'][:22]:22s} {data['chg_1d_pct']:+6.2f}%  "
              f"5d {data['mom_5d_pct']:+6.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
