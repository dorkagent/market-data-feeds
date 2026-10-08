#!/usr/bin/env python3
"""FINRA consolidated short interest (positions) for the Alpaca paper-trading simulator.

CONTEXT LAYER ONLY — informs, never triggers trades alone. Biweekly short
*positions* (not flow): current/previous short position, avg daily volume,
days-to-cover, change %. Additive to the daily short-volume feed (flow).

Endpoint (keyless, no signup):
  POST https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest
  Fields: settlementDate, symbolCode, issueName, marketClassCode,
          currentShortPositionQuantity, previousShortPositionQuantity,
          averageDailyVolumeQuantity, daysToCoverQuantity, changePercent

NMS COVERAGE CAVEAT: the dataset group is `otcMarket`; per-symbol NMS
coverage is verified empirically — any watchlist symbol returning no rows is
flagged "unverified" in the snapshot rather than silently trusted.

Idempotent: processes only a NEW settlement date; otherwise writes
"no_new_data" and exits 0.

Writes:
  hidden_files/finra-short-interest/finra-short-interest-latest.json
  hidden_files/finra-short-interest/finra-short-interest-history.jsonl
  hidden_files/finra-short-interest/state.json

Stdout: ALERT lines for days-to-cover spikes; OK otherwise.
Exit 0 on success; nonzero on hard failure (writes an error snapshot).
"""

import datetime as dt
import json
import os
import sys

try:
    import requests
except ImportError:
    print("ERROR: requests is not importable", file=sys.stderr)
    sys.exit(2)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "hidden_files", "finra-short-interest")
STATE = os.path.join(OUTDIR, "state.json")

API = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
FIELDS = ["settlementDate", "symbolCode", "issueName", "marketClassCode",
          "currentShortPositionQuantity", "previousShortPositionQuantity",
          "averageDailyVolumeQuantity", "daysToCoverQuantity", "changePercent"]
WATCHLIST = ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"]
DTC_ALERT = 5.0  # days-to-cover level worth flagging as context

NOTE = ("Context layer — informs, never triggers trades alone. "
        "Biweekly short positions; unverified symbols are flagged, never trusted.")


def query():
    end = dt.date.today()
    start = end - dt.timedelta(days=90)
    payload = {"fields": FIELDS,
               "dateRangeFilters": [{"fieldName": "settlementDate",
                                     "startDate": start.isoformat(),
                                     "endDate": end.isoformat()}],
               "domainFilters": [{"fieldName": "symbolCode", "values": WATCHLIST}],
               "limit": 500}
    r = requests.post(API, json=payload, headers={"Accept": "application/json"},
                      timeout=60)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected response shape: {str(rows)[:200]}")
    return rows


def load_state():
    if os.path.exists(STATE):
        try:
            with open(STATE) as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {}


def save_state(s):
    with open(STATE, "w") as f:
        json.dump(s, f, indent=2)


def write(snapshot):
    with open(os.path.join(OUTDIR, "finra-short-interest-latest.json"), "w") as f:
        json.dump(snapshot, f, indent=2)
    with open(os.path.join(OUTDIR, "finra-short-interest-history.jsonl"), "a") as f:
        f.write(json.dumps(snapshot) + "\n")


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    snapshot = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": NOTE,
        "source": "FINRA consolidatedShortInterest (api.finra.org keyless)",
        "status": "ok",
        "tickers": {},
    }
    try:
        rows = query()
        by_sym = {}
        for r in rows:
            by_sym.setdefault(r.get("symbolCode"), []).append(r)
        latest_date = max((r.get("settlementDate", "") for r in rows), default=None)
        snapshot["latest_settlement_date"] = latest_date
        state = load_state()
        if state.get("last_settlement_date") == latest_date and state.get("tickers"):
            snapshot["status"] = "no_new_data"
            snapshot["tickers"] = state["tickers"]
            write(snapshot)
            print(f"OK finra-short-interest: no new settlement date ({latest_date} already processed)")
            return 0
        for sym in WATCHLIST:
            sym_rows = [r for r in by_sym.get(sym, [])
                        if r.get("settlementDate") == latest_date]
            if not sym_rows:
                snapshot["tickers"][sym] = {
                    "status": "unverified",
                    "note": "no rows for latest settlement date — NMS coverage "
                            "not confirmed; do not trust",
                }
                continue
            r0 = sym_rows[0]
            snapshot["tickers"][sym] = {
                "status": "verified",
                "settlement_date": r0.get("settlementDate"),
                "issue_name": r0.get("issueName"),
                "market_class": r0.get("marketClassCode"),
                "short_position": r0.get("currentShortPositionQuantity"),
                "prev_short_position": r0.get("previousShortPositionQuantity"),
                "avg_daily_volume": r0.get("averageDailyVolumeQuantity"),
                "days_to_cover": r0.get("daysToCoverQuantity"),
                "change_pct": r0.get("changePercent"),
            }
        save_state({"last_settlement_date": latest_date,
                    "tickers": snapshot["tickers"]})
    except Exception as e:
        snapshot["status"] = "error"
        snapshot["error"] = str(e)
        write(snapshot)
        print(f"ERROR: finra-short-interest feed failed: {e}", file=sys.stderr)
        return 1

    write(snapshot)
    for sym, e in snapshot["tickers"].items():
        dtc = e.get("days_to_cover")
        if isinstance(dtc, (int, float)) and dtc >= DTC_ALERT:
            print(f"ALERT finra-short-interest {sym}: days-to-cover {dtc} "
                  f"on {e['settlement_date']} (elevated)")
    cov = {s: e.get("status") for s, e in snapshot["tickers"].items()}
    print(f"OK finra-short-interest: settlement {snapshot['latest_settlement_date']}; "
          f"coverage={cov}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
