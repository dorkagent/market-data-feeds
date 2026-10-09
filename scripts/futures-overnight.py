#!/usr/bin/env python3
"""Index-futures + vol + commodities overnight feed (keyless).

Quotes: Yahoo Finance unofficial chart API (no key) — index futures
(ES/NQ/YM/RTY), vol (VIX spot + 3-month term-structure leg), commodities
(gold, crude oil). NOTE: Yahoo does not expose a VX=F front-month VIX
futures symbol, so the term-structure leg uses ^VIX3M (CBOE 3-month vol
index) instead of front-month VIX futures; the contango/backwardation
read is the same regime hint.

CFTC Commitments of Traders (legacy futures-only, public Socrata, no key):
speculator (non-commercial) net positioning for S&P 500 / Nasdaq-100 /
VIX / gold / crude — regime only, weekly. The COT leg gets 2 attempts;
if it stays flaky it is SKIPPED cleanly (logged) and never blocks quotes.

Outputs:
  hidden_files/futures/futures-latest.json   (full snapshot)
  hidden_files/futures/futures-history.jsonl (one compact line per run)

Context layer only: "informs, never triggers trades alone."

Freshness rule: any quote-leg failure -> status "stale", the last good
futures-latest.json is left untouched, the error is logged, and the run
still appends one history line. NEVER write fresh timestamps on stale data.

Stdlib + requests only.
"""

import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests

SYMBOLS = {
    "ES=F": "S&P 500 e-mini",
    "NQ=F": "Nasdaq-100 e-mini",
    "YM=F": "Dow e-mini",
    "RTY=F": "Russell 2000 e-mini",
    "^VIX": "VIX spot",
    "^VIX3M": "VIX 3-month",
    "GC=F": "gold",
    "CL=F": "WTI crude",
}

# CFTC legacy futures-only Socrata dataset (verified live 2026-10-09).
CFTC_BASE = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
COT_MARKETS = {
    "sp500": "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
    "nasdaq100": "NASDAQ-100 Consolidated - CHICAGO MERCANTILE EXCHANGE",
    "vix": "VIX FUTURES - CBOE FUTURES EXCHANGE",
    "gold": "GOLD - COMMODITY EXCHANGE INC.",
    # NYMEX renamed the contract; the old "CRUDE OIL, LIGHT SWEET - ..."
    # name now returns only pre-2022 rows.
    "crude": "WTI FINANCIAL CRUDE OIL - NEW YORK MERCANTILE EXCHANGE",
}
# COT publishes Fridays for Tuesday positions; >21d stale means the market
# name likely changed again -> skip the leg rather than feed ancient data.
COT_MAX_AGE_DAYS = 21

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "hidden_files", "futures")
LATEST = os.path.join(OUT_DIR, "futures-latest.json")
HISTORY = os.path.join(OUT_DIR, "futures-history.jsonl")

UA = {"User-Agent": "futures-overnight-feed/1.0 (paper-trading research)"}


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


def yahoo_quote(symbol):
    """{price, prev_close, change_pct, quote_time_utc} from daily bars."""
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
    if len(closes) < 2:
        raise RuntimeError(f"{symbol}: fewer than 2 daily closes")
    price, prev = closes[-1], closes[-2]
    rmt = meta.get("regularMarketTime")
    quote_time = (datetime.fromtimestamp(rmt, tz=timezone.utc)
                  .isoformat(timespec="seconds") if rmt else None)
    return {
        "name": SYMBOLS[symbol],
        "price": round(price, 2),
        "change_pct": round((price - prev) / prev * 100, 2),
        "quote_time_utc": quote_time,
    }


def cot_leg():
    """Latest 2 weekly rows per market -> speculator net + week delta."""
    out = {}
    for key, market in COT_MARKETS.items():
        q = (f"?market_and_exchange_names={urllib.parse.quote(market)}"
             "&$limit=2&$order=report_date_as_yyyy_mm_dd%20DESC"
             "&$select=report_date_as_yyyy_mm_dd,noncomm_positions_long_all,"
             "noncomm_positions_short_all,open_interest_all")
        rows = get_json(CFTC_BASE + q, timeout=30, retries=2)
        if not rows:
            raise RuntimeError(f"COT: no rows for {market}")
        report_date = (rows[0].get("report_date_as_yyyy_mm_dd") or "")[:10]
        age_days = (datetime.now(timezone.utc).date()
                    - datetime.strptime(report_date, "%Y-%m-%d").date()).days
        if age_days > COT_MAX_AGE_DAYS:
            raise RuntimeError(
                f"COT: {market} latest report {report_date} is {age_days}d old "
                f"(market name may have changed)")
        def net(r):
            return (int(str(r["noncomm_positions_long_all"]).replace(",", ""))
                    - int(str(r["noncomm_positions_short_all"]).replace(",", "")))
        latest, prev = rows[0], rows[1] if len(rows) > 1 else None
        out[key] = {
            "market": market,
            "report_date": (latest.get("report_date_as_yyyy_mm_dd") or "")[:10],
            "spec_net": net(latest),
            "spec_net_prev_week": net(prev) if prev else None,
            "open_interest": int(str(latest.get("open_interest_all") or 0)
                                 .replace(",", "")),
        }
    return out


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    errors = []
    quotes = {}
    for sym in SYMBOLS:
        try:
            quotes[sym] = yahoo_quote(sym)
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))

    # COT is regime-only: 2 attempts inside get_json, skip cleanly on failure.
    cot = {}
    cot_skipped = None
    if not errors:
        try:
            cot = cot_leg()
        except Exception as e:  # noqa: BLE001
            cot_skipped = f"COT skipped: {e}"

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

    vix_spot = quotes["^VIX"]["price"]
    vix_3m = quotes["^VIX3M"]["price"]
    spread = round(vix_3m - vix_spot, 2)
    snapshot = {
        "fetched_at_utc": fetched_at,
        "status": "ok",
        "note": "Context layer - informs, never triggers trades alone. "
                "Futures trade nearly 24/5; quotes are the overnight read "
                "into the 06:00 PT premarket scan.",
        "symbols": quotes,
        "vix_term_structure": {
            "spot": vix_spot,
            "three_month": vix_3m,
            "spread": spread,
            "regime": "contango (normal)" if spread > 0
                      else "backwardation (stress)",
            "leg_note": "^VIX3M used - Yahoo has no VX=F front-month symbol",
        },
        "cot": {
            "regime_only": True,
            "skipped": cot_skipped,
            "markets": cot,
        },
    }
    with open(LATEST, "w") as f:
        json.dump(snapshot, f, indent=2)

    hist_line.update({
        "status": "ok",
        "quotes": {s: {"p": q["price"], "c": q["change_pct"]}
                   for s, q in quotes.items()},
        "vix_spread": spread,
        "cot_skipped": bool(cot_skipped),
    })
    with open(HISTORY, "a") as f:
        f.write(json.dumps(hist_line) + "\n")

    for s, q in quotes.items():
        print(f"{s:6s} {q['price']:>10}  {q['change_pct']:+.2f}%")
    print(f"VIX term structure: spot {vix_spot} / 3M {vix_3m} "
          f"(spread {spread:+.2f})")
    if cot:
        for k, m in cot.items():
            print(f"COT {k}: spec net {m['spec_net']:+,} "
                  f"(report {m['report_date']})")
    if cot_skipped:
        print(cot_skipped, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
