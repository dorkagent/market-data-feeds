#!/usr/bin/env python3
"""CBOE daily put/call ratios for the Alpaca paper-trading simulator.

CONTEXT LAYER ONLY — informs, never triggers trades alone. Classic
contrarian sentiment gauge: elevated put/call = fear (contrarian bullish
context), depressed = complacency (contrarian caution).

Source (free, no key, no signup):
  https://www.cboe.com/markets/us/options/market-statistics/daily
  Ratios are embedded in the page's Next.js data blob (optionsData.ratios).
  Browser UA required. Page reflects the latest trading day (post-close).

Tracked: TOTAL, INDEX, EXCHANGE TRADED PRODUCTS (ETF), EQUITY, SPX + SPXW, VIX.
History builds a z-score baseline after 20 readings (history.jsonl).

Writes:
  hidden_files/cboe-putcall/cboe-putcall-latest.json
  hidden_files/cboe-putcall/cboe-putcall-history.jsonl

Stdout: ALERT lines on extreme readings; OK otherwise.
Exit 0 on success; nonzero on hard failure (writes an error snapshot).
"""

import datetime as dt
import json
import os
import re
import statistics
import sys

try:
    import requests
except ImportError:
    print("ERROR: requests is not importable", file=sys.stderr)
    sys.exit(2)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "hidden_files", "cboe-putcall")

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
URL = "https://www.cboe.com/markets/us/options/market-statistics/daily"
TRACKED = ["TOTAL PUT/CALL RATIO", "INDEX PUT/CALL RATIO",
           "EXCHANGE TRADED PRODUCTS PUT/CALL RATIO", "EQUITY PUT/CALL RATIO",
           "SPX + SPXW PUT/CALL RATIO", "CBOE VOLATILITY INDEX (VIX) PUT/CALL RATIO"]
SHORT = {"TOTAL PUT/CALL RATIO": "total", "INDEX PUT/CALL RATIO": "index",
         "EXCHANGE TRADED PRODUCTS PUT/CALL RATIO": "etf",
         "EQUITY PUT/CALL RATIO": "equity",
         "SPX + SPXW PUT/CALL RATIO": "spx",
         "CBOE VOLATILITY INDEX (VIX) PUT/CALL RATIO": "vix"}

NOTE = ("Context layer — informs, never triggers trades alone. "
        "Contrarian sentiment gauge; extreme readings are context, not signals.")


def fetch_ratios():
    r = requests.get(URL, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    t = r.text.replace('\\"', '"')
    m = re.findall(r'"name":"([^"]*PUT/CALL RATIO[^"]*)","value":"([\d.]+)"', t)
    out = {}
    for name, val in m:
        if name in TRACKED:
            try:
                out[SHORT[name]] = float(val)
            except ValueError:
                pass
    if not out:
        raise RuntimeError("no put/call ratios found in page (layout changed?)")
    return out


def history_values():
    path = os.path.join(OUTDIR, "cboe-putcall-history.jsonl")
    vals = {}
    if not os.path.exists(path):
        return vals
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
                if d.get("status") == "ok":
                    for k, v in d.get("ratios", {}).items():
                        vals.setdefault(k, []).append(v)
            except (json.JSONDecodeError, KeyError):
                continue
    return vals


def write(snapshot):
    with open(os.path.join(OUTDIR, "cboe-putcall-latest.json"), "w") as f:
        json.dump(snapshot, f, indent=2)
    with open(os.path.join(OUTDIR, "cboe-putcall-history.jsonl"), "a") as f:
        f.write(json.dumps(snapshot) + "\n")


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    snapshot = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": NOTE,
        "source": "CBOE daily market statistics (cboe.com, keyless scrape)",
        "status": "ok",
        "ratios": {},
    }
    try:
        snapshot["ratios"] = fetch_ratios()
    except Exception as e:
        snapshot["status"] = "error"
        snapshot["error"] = str(e)
        write(snapshot)
        print(f"ERROR: cboe-putcall feed failed: {e}", file=sys.stderr)
        return 1

    hist = history_values()
    snapshot["z_scores"] = {}
    for k, v in snapshot["ratios"].items():
        base = hist.get(k, [])
        if len(base) >= 20:
            sd = statistics.pstdev(base)
            snapshot["z_scores"][k] = round((v - statistics.fmean(base)) / sd, 2) if sd else 0.0

    write(snapshot)
    r = snapshot["ratios"]
    flags = []
    if r.get("total", 0) >= 1.0:
        flags.append(f"total p/c {r['total']:.2f} >= 1.0 (fear — contrarian bullish context)")
    if r.get("total", 1) <= 0.6:
        flags.append(f"total p/c {r['total']:.2f} <= 0.6 (complacency — contrarian caution)")
    if r.get("equity", 1) <= 0.45:
        flags.append(f"equity p/c {r['equity']:.2f} <= 0.45 (extreme equity complacency)")
    for z, k in [(v, k) for k, v in snapshot["z_scores"].items() if abs(v) > 2]:
        flags.append(f"{k} p/c z={z:+.1f} vs own history")
    for fl in flags:
        print(f"ALERT cboe-putcall: {fl}")
    if not flags:
        print(f"OK cboe-putcall: total={r.get('total')} index={r.get('index')} "
              f"etf={r.get('etf')} equity={r.get('equity')} spx={r.get('spx')} vix={r.get('vix')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
