#!/usr/bin/env python3
"""FINRA Reg SHO daily short-volume feed for the Alpaca paper-trading simulator.

CONTEXT LAYER ONLY — informs, never triggers trades alone. FINRA publishes
this data T+1 (yesterday's tape), so it is laggy-but-official: useful for
spotting crowded-short setups and squeeze risk on existing shorts, not for
timing entries.

Endpoint: keyless FINRA Query API (free for non-commercial use, no key needed).
  POST https://api.finra.org/data/group/otcMarket/name/regShoDaily
  This is FINRA's official "short sale volume" data product (Reg SHO daily):
  per-day, per-symbol, per-market-center short vs total par quantities.
  Note: the *other* FINRA short product, consolidatedShortInterest, is biweekly
  OTC short *positions* — not what this feed uses. See README.md for the
  endpoint findings (api.finra.org worked keyless from the VM 2026-10-01;
  gateway.finra.org key flow was never needed).

Wiring: one POST with domainFilters on the 5 symbols + dateRangeFilters on
tradeReportDate (trailing ~45 calendar days). Rows come back per market center;
we aggregate per symbol/date and compute:

  short_pct = sum(shortParQuantity) / sum(totalParQuantity)   (exempt shorts excluded)
  z         = (latest short_pct - mean of trailing 20 trading days) / std
  dod_pp    = latest short_pct - prior trading day short_pct   (percentage points)

Flags (context inputs for the monitors, not trade signals):
  squeeze_risk    : short% z-score > 2.0   -> crowded-short / squeeze-risk setup
  short_pct_spike : day-over-day short% rise >= 10 percentage points

The DoD spike flag is cross-referenced against open paper positions
(~/workspace/skills/alpaca-paper-trading/bin/alpaca positions); a spike on a
name we hold gets the "holding" field set true. If the positions read fails,
holding is "unknown" and the spike is still reported transparently.

Writes:
  hidden_files/finra-short/finra-short-latest.json   (read by the monitors)
  hidden_files/finra-short/finra-short-history.jsonl (append-only run log)

Stdout: ALERT lines for flagged tickers only (the cron surfaces these).
Exit 0 on success; nonzero on hard failure (writes an error snapshot).
"""

import datetime as dt
import json
import os
import statistics
import subprocess
import sys
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(BASE, "hidden_files", "finra-short")
ALPACA_CLI = os.path.expanduser("~/workspace/skills/alpaca-paper-trading/bin/alpaca")

API = "https://api.finra.org/data/group/otcMarket/name/regShoDaily"
SYMBOL_FIELD = "securitiesInformationProcessorSymbolIdentifier"
UNIVERSE = ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"]

WINDOW_DAYS = 45          # calendar days back, to net >= 20 trading days
BASELINE_TRADING_DAYS = 20
Z_THRESHOLD = 2.0
SPIKE_PP_THRESHOLD = 0.10  # 10 percentage points day-over-day

NOTE = ("Context layer — informs, never triggers trades alone. "
        "FINRA data is T+1, laggy-but-official.")


def post_query(payload, tries=2):
    # stdlib-only (urllib): the GitHub runner image has no `requests` installed
    # and the workflow cannot pip-install it without a workflow-scope token.
    last = None
    body = json.dumps(payload).encode("utf-8")
    for _ in range(tries):
        try:
            req = urllib.request.Request(
                API, data=body, method="POST",
                headers={"Accept": "application/json",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                text = r.read().decode("utf-8")
                if r.status == 200:
                    return json.loads(text)
                last = f"HTTP {r.status}: {text[:300]}"
        except Exception as e:  # network flake / HTTPError; retry once
            last = f"{type(e).__name__}: {e}"
    raise RuntimeError(f"FINRA Query API failed after {tries} tries: {last}")


def fetch_window(end, start):
    payload = {
        "fields": ["tradeReportDate", SYMBOL_FIELD, "shortParQuantity",
                   "shortExemptParQuantity", "totalParQuantity"],
        "dateRangeFilters": [{"fieldName": "tradeReportDate",
                              "startDate": start.isoformat(),
                              "endDate": end.isoformat()}],
        "domainFilters": [{"fieldName": SYMBOL_FIELD, "values": UNIVERSE}],
        "limit": 5000,
    }
    rows = post_query(payload)
    if not isinstance(rows, list):
        raise RuntimeError(f"unexpected response shape: {str(rows)[:200]}")
    return rows


def aggregate(rows):
    """Per symbol -> per date: summed short/total across market centers."""
    per = {}
    for r in rows:
        try:
            sym = r[SYMBOL_FIELD]
            d = r["tradeReportDate"]
            short = int(r.get("shortParQuantity") or 0)
            exempt = int(r.get("shortExemptParQuantity") or 0)
            total = int(r.get("totalParQuantity") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        per.setdefault(sym, {}).setdefault(d, {"short": 0, "exempt": 0, "total": 0})
        acc = per[sym][d]
        acc["short"] += short
        acc["exempt"] += exempt
        acc["total"] += total
    return per


def short_pct(acc):
    if acc["total"] <= 0:
        return None
    return acc["short"] / acc["total"]


def open_positions():
    """Set of symbols currently held in the Alpaca paper account, or None."""
    try:
        out = subprocess.run([ALPACA_CLI, "positions"], capture_output=True,
                             text=True, timeout=20)
        if out.returncode != 0:
            return None
        syms = set()
        for tok in out.stdout.replace(",", " ").split():
            t = tok.strip().upper()
            if t in UNIVERSE:
                syms.add(t)
        return syms
    except Exception:
        return None


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    end = dt.date.today()
    start = end - dt.timedelta(days=WINDOW_DAYS)

    snapshot = {
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "note": NOTE,
        "source": "FINRA Reg SHO Daily (api.finra.org keyless Query API)",
        "status": "ok",
    }

    try:
        rows = fetch_window(end, start)
    except Exception as e:
        snapshot["status"] = "error"
        snapshot["error"] = str(e)
        snapshot["tickers"] = {}
        write(snapshot)
        print(f"ERROR: finra-short feed failed: {e}", file=sys.stderr)
        sys.exit(1)

    per = aggregate(rows)
    holdings = open_positions()
    latest_date = max((d for sym in per for d in per[sym]), default=None)
    snapshot["latest_finra_date"] = latest_date
    snapshot["tickers"] = {}

    for sym in UNIVERSE:
        dates = sorted(per.get(sym, {}))
        pcts = {d: short_pct(per[sym][d]) for d in dates}
        pcts = {d: v for d, v in pcts.items() if v is not None}
        dates = sorted(pcts)
        entry = {"dates_covered": len(dates), "flags": [], "holding": None}
        if not dates:
            entry["error"] = "no data in window"
            snapshot["tickers"][sym] = entry
            continue
        latest = dates[-1]
        base = dates[-21:-1] if len(dates) > 1 else []
        if len(base) < BASELINE_TRADING_DAYS:
            base = dates[:-1]
        base_vals = [pcts[d] for d in base]
        entry["short_pct"] = round(pcts[latest], 4)
        entry["latest_date"] = latest
        if len(base_vals) >= 2:
            mean = statistics.fmean(base_vals)
            sd = statistics.pstdev(base_vals)
            entry["short_pct_z"] = round((pcts[latest] - mean) / sd, 2) if sd > 0 else 0.0
            entry["baseline_mean"] = round(mean, 4)
            entry["baseline_days"] = len(base_vals)
        else:
            entry["short_pct_z"] = None
        # day-over-day change vs prior trading day in the window
        if len(dates) >= 2:
            entry["dod_pp"] = round(pcts[latest] - pcts[dates[-2]], 4)
            entry["prior_date"] = dates[-2]
        if holdings is not None:
            entry["holding"] = sym in holdings
        # flags
        if entry["short_pct_z"] is not None and entry["short_pct_z"] > Z_THRESHOLD:
            entry["flags"].append("squeeze_risk")
        if entry.get("dod_pp") is not None and entry["dod_pp"] >= SPIKE_PP_THRESHOLD:
            entry["flags"].append("short_pct_spike")
        snapshot["tickers"][sym] = entry

    write(snapshot)

    flagged = [(s, e) for s, e in snapshot["tickers"].items() if e.get("flags")]
    for sym, e in flagged:
        hold = f" (HELD)" if e.get("holding") else ""
        print(f"ALERT finra-short {sym}{hold}: flags={','.join(e['flags'])} "
              f"short%={e['short_pct']:.2%} z={e['short_pct_z']} "
              f"DoD={e.get('dod_pp', 0):+.2%} on {e['latest_date']}")
    if not flagged:
        print(f"OK finra-short: no flags; latest FINRA date {latest_date}")
    return 0


def write(snapshot):
    with open(os.path.join(OUTDIR, "finra-short-latest.json"), "w") as f:
        json.dump(snapshot, f, indent=2)
    with open(os.path.join(OUTDIR, "finra-short-history.jsonl"), "a") as f:
        f.write(json.dumps(snapshot) + "\n")


if __name__ == "__main__":
    sys.exit(main())
