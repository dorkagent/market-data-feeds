#!/usr/bin/env python3
"""
options-flow.py -- Self-computed unusual options activity from Nasdaq chains.

Pulls the full option chain for SPY, QQQ, NVDA, AAPL, MSFT via Nasdaq's
keyless quote API and flags contracts where today's volume exceeds 2x open
interest (unusual activity). This is our own first-party options-flow
signal -- no vendor, no signup, no key.

CONTEXT ONLY -- informs, never triggers trades alone. Unusual volume can
mean hedging, closing, or speculation; direction requires the thesis.

Endpoint: https://api.nasdaq.com/api/quote/{SYM}/option-chain?assetclass=stocks
Header required: User-Agent: Mozilla/5.0 (keyless, no signup).

Outputs (under hidden_files/options-flow/):
  options-flow-latest.json  full snapshot: per-ticker summaries + flagged contracts
  options-flow-history.jsonl  one line per run: date, per-ticker flag counts
  README.md                 this directory's documentation

Exit codes: 0 = ok, 1 = failure. On failure latest.json is still written
with status="error" so the cron run can report the problem plainly.
"""

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

UNIVERSE = ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"]
# SPY/QQQ are ETFs; Nasdaq requires assetclass=etf for them.
ASSET_CLASS = {"SPY": "etf", "QQQ": "etf", "NVDA": "stocks", "AAPL": "stocks", "MSFT": "stocks"}
BASE = "https://api.nasdaq.com/api/quote/{sym}/option-chain?assetclass={ac}"
HEADERS = {"User-Agent": "Mozilla/5.0"}
REQUEST_TIMEOUT = 30
DELAY_BETWEEN_SYMBOLS = 2.0  # polite; 5 names, well under any limit
PT = ZoneInfo("America/Los_Angeles")

VOLUME_OI_RATIO = 2.0  # flag contracts where volume > 2x open interest
MIN_VOLUME = 200       # ignore small prints; unusual activity needs size
MIN_OI = 5             # ignore OI=1 noise (far-OTM strikes with no real base)

HERE = Path(__file__).resolve()
GOAL_DIR = HERE.parent.parent
OUT_DIR = GOAL_DIR / "hidden_files" / "options-flow"
LATEST = OUT_DIR / "options-flow-latest.json"
HISTORY = OUT_DIR / "options-flow-history.jsonl"

CONTEXT_NOTE = "Context layer -- informs, never triggers trades alone."


def pt_today():
    return datetime.now(PT).date().isoformat()


def parse_num(raw):
    """Nasdaq formats: '--' for none, commas in thousands. Returns int or 0."""
    if raw is None:
        return 0
    s = str(raw).strip().replace(",", "")
    if s in ("", "--", "N/A"):
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def fetch_chain(sym):
    url = BASE.format(sym=sym, ac=ASSET_CLASS[sym])
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise RuntimeError(f"transport error fetching {sym}: {e}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} fetching {sym} chain")
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"non-JSON response for {sym} chain")
    rows = (data.get("data") or {}).get("table", {}).get("rows", [])
    if not rows:
        raise RuntimeError(f"empty chain for {sym}")
    return rows


def analyze(sym, rows):
    """Walk contracts, flag unusual volume. Returns (summary, flagged)."""
    flagged = []
    total_contracts = 0
    total_call_vol = 0
    total_put_vol = 0
    for row in rows:
        if not row.get("strike"):
            continue  # expiry-group header row
        total_contracts += 1
        strike = row.get("strike")
        expiry = row.get("expiryDate", "")
        for side, prefix in (("call", "c_"), ("put", "p_")):
            vol = parse_num(row.get(prefix + "Volume"))
            oi = parse_num(row.get(prefix + "Openinterest"))
            if side == "call":
                total_call_vol += vol
            else:
                total_put_vol += vol
            if vol >= MIN_VOLUME and oi >= MIN_OI and vol > VOLUME_OI_RATIO * oi:
                flagged.append({
                    "symbol": sym,
                    "side": side,
                    "strike": strike,
                    "expiry": expiry,
                    "volume": vol,
                    "open_interest": oi,
                    "vol_oi_ratio": round(vol / oi, 2),
                    "last": row.get(prefix + "Last", "--"),
                    "bid": row.get(prefix + "Bid", "--"),
                    "ask": row.get(prefix + "Ask", "--"),
                })
    # sort by ratio, hottest first
    flagged.sort(key=lambda f: f["vol_oi_ratio"], reverse=True)
    summary = {
        "contracts_scanned": total_contracts,
        "total_call_volume": total_call_vol,
        "total_put_volume": total_put_vol,
        "put_call_volume_ratio": round(total_put_vol / total_call_vol, 3) if total_call_vol else None,
        "flagged_count": len(flagged),
    }
    return summary, flagged


def write_outputs(snapshot):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(snapshot, indent=2) + "\n")
    hist_line = {
        "run_date_pt": snapshot.get("run_date_pt"),
        "status": snapshot.get("status"),
        "tickers": {
            sym: {
                "flagged": t["summary"]["flagged_count"],
                "put_call_ratio": t["summary"].get("put_call_volume_ratio"),
            }
            for sym, t in snapshot.get("tickers", {}).items()
        },
        "error": snapshot.get("error"),
    }
    with HISTORY.open("a") as f:
        f.write(json.dumps(hist_line) + "\n")


def fail(msg):
    snapshot = {
        "generated_at_utc": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
        "run_date_pt": pt_today(),
        "status": "error",
        "error": msg,
        "note": CONTEXT_NOTE,
        "universe": UNIVERSE,
        "tickers": {},
    }
    write_outputs(snapshot)
    print(f"options-flow: ERROR: {msg}", file=sys.stderr)
    return 1


def main():
    tickers = {}
    try:
        for i, sym in enumerate(UNIVERSE):
            if i:
                time.sleep(DELAY_BETWEEN_SYMBOLS)
            rows = fetch_chain(sym)
            summary, flagged = analyze(sym, rows)
            tickers[sym] = {"summary": summary, "flagged_contracts": flagged[:25]}
    except RuntimeError as e:
        return fail(str(e))
    snapshot = {
        "generated_at_utc": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
        "run_date_pt": pt_today(),
        "status": "ok",
        "note": CONTEXT_NOTE,
        "universe": UNIVERSE,
        "rule": f"flag when volume > {VOLUME_OI_RATIO}x open interest and volume >= {MIN_VOLUME}",
        "tickers": tickers,
    }
    write_outputs(snapshot)
    total_flags = sum(t["summary"]["flagged_count"] for t in tickers.values())
    print(f"options-flow: ok, {total_flags} flagged contracts across {len(tickers)} names")
    for sym, t in tickers.items():
        if t["flagged_contracts"]:
            top = t["flagged_contracts"][0]
            print(f"  {sym}: hottest {top['side']} {top['strike']} {top['expiry']} "
                  f"vol {top['volume']} vs OI {top['open_interest']} ({top['vol_oi_ratio']}x)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
