#!/usr/bin/env python3
"""
companyfacts.py -- Pre-parsed fundamentals from SEC XBRL companyfacts.

Pulls structured financial facts for AAPL, MSFT, NVDA from SEC EDGAR's
keyless companyfacts API. This is the fundamentals layer our filings-based
EDGAR coverage lacks: EDGAR tracks *filings*; this gives *numbers*.

CONTEXT ONLY -- informs, never triggers trades alone. Fundamentals move
slowly (quarterly); this is reference data for thesis-building, not timing.

Endpoint: https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
Header required: User-Agent: DorkPaperTrading/1.0 (dork@agentmail.to)
(keyless; SEC blocks missing/default user agents).

Outputs (under hidden_files/companyfacts/):
  companyfacts-latest.json  per-ticker fundamentals snapshot
  README.md                 this directory's documentation

Exit codes: 0 = ok, 1 = failure. On failure latest.json is still written
with status="error" so the cron run can report the problem plainly.
"""

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

CIKS = {
    "AAPL": "0000320193",
    "MSFT": "0000789019",
    "NVDA": "0001045810",
}
BASE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
HEADERS = {"User-Agent": "DorkPaperTrading/1.0 (dork@agentmail.to)"}
REQUEST_TIMEOUT = 30
PT = ZoneInfo("America/Los_Angeles")

# fact name -> (label, prefer_10k). Revenue tries multiple names because
# filers differ: AAPL/MSFT use RevenueFromContractWithCustomerExcludingAssessedTax,
# NVDA uses Revenues.
REVENUE_FACTS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
]
FACTS = {
    "NetIncomeLoss": ("net_income_ttm", True),
    "EarningsPerShareDiluted": ("eps_diluted_ttm", True),
    "Assets": ("total_assets", False),
    "LongTermDebt": ("long_term_debt", False),
    "LongTermDebtCurrent": ("long_term_debt_current", False),
    "NetCashProvidedByUsedInOperatingActivities": ("operating_cash_flow_ttm", True),
}

HERE = Path(__file__).resolve()
GOAL_DIR = HERE.parent.parent
OUT_DIR = GOAL_DIR / "hidden_files" / "companyfacts"
LATEST = OUT_DIR / "companyfacts-latest.json"

CONTEXT_NOTE = "Context layer -- informs, never triggers trades alone."


def pt_today():
    return datetime.now(PT).date().isoformat()


def fetch_facts(sym, cik):
    url = BASE.format(cik=cik)
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        raise RuntimeError(f"transport error fetching {sym}: {e}")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} fetching {sym} companyfacts")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"non-JSON response for {sym} companyfacts")


def latest_value(entries, prefer_10k):
    """Pick the most recent entry; prefer 10-K annuals for TTM figures."""
    if not entries:
        return None
    cands = entries
    if prefer_10k:
        annuals = [e for e in entries if e.get("form") == "10-K"]
        if annuals:
            cands = annuals
    # most recent end date; dedupe identical end+val pairs
    cands.sort(key=lambda e: (e.get("end", ""), e.get("filed", "")))
    best = cands[-1]
    return {"value": best.get("val"), "end": best.get("end"),
            "filed": best.get("filed"), "form": best.get("form")}


def extract(sym, data):
    facts = (data.get("facts") or {}).get("us-gaap", {})
    out = {}
    # revenue: try each fact name, take the first with a recent 10-K
    for fact_name in REVENUE_FACTS:
        units = facts.get(fact_name, {}).get("units", {})
        entries = units.get("USD") or []
        val = latest_value(entries, prefer_10k=True)
        if val and val.get("end", "") >= "2024-01-01":
            out["revenue_ttm"] = dict(val, fact=fact_name)
            break
    for fact_name, (label, prefer_10k) in FACTS.items():
        units = facts.get(fact_name, {}).get("units", {})
        # USD first, then USD/shares for per-share facts
        entries = units.get("USD") or units.get("USD/shares") or []
        val = latest_value(entries, prefer_10k)
        if val:
            out[label] = val
    # total debt = long-term + current portion
    ltd = out.get("long_term_debt", {}).get("value")
    ltdc = out.get("long_term_debt_current", {}).get("value")
    if ltd is not None or ltdc is not None:
        out["total_debt"] = {
            "value": (ltd or 0) + (ltdc or 0),
            "end": (out.get("long_term_debt") or {}).get("end"),
            "note": "long_term_debt + long_term_debt_current",
        }
    return out


def fail(msg):
    snapshot = {
        "generated_at_utc": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
        "run_date_pt": pt_today(),
        "status": "error",
        "error": msg,
        "note": CONTEXT_NOTE,
        "tickers": {},
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(f"companyfacts: ERROR: {msg}", file=sys.stderr)
    return 1


def main():
    tickers = {}
    try:
        for sym, cik in CIKS.items():
            data = fetch_facts(sym, cik)
            tickers[sym] = extract(sym, data)
    except RuntimeError as e:
        return fail(str(e))
    snapshot = {
        "generated_at_utc": datetime.now(tz=ZoneInfo("UTC")).isoformat(),
        "run_date_pt": pt_today(),
        "status": "ok",
        "note": CONTEXT_NOTE,
        "tickers": tickers,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(f"companyfacts: ok for {len(tickers)} names")
    for sym, t in tickers.items():
        rev = (t.get("revenue_ttm") or {}).get("value")
        if rev:
            print(f"  {sym}: revenue TTM ${rev/1e9:.1f}B "
                  f"({(t.get('revenue_ttm') or {}).get('end')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
