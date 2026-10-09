#!/usr/bin/env python3
"""
btc-cycle.py -- Bitcoin on-chain cycle gauges for the PaperTrade crypto sleeve.

Pulls MVRV, NUPL, and SOPR from bitcoin-data.com (keyless, no signup) and
writes the latest values plus a plain-English overheating read.

The three gauges:
- MVRV (Market Value / Realized Value): > 3.5 = overheated top zone,
  < 1.0 = undervalued bottom zone. Realized price = MVRV denominator.
- NUPL (Net Unrealized Profit/Loss): > 0.75 = euphoria (top),
  < 0 = capitulation (bottom).
- SOPR (Spent Output Profit Ratio): > 1 = holders selling at a profit,
  < 1 = selling at a loss. Sustained < 1 = bearish capitulation.

Context layer for the crypto sleeve -- informs position sizing and
risk posture, never a trade trigger alone.

Outputs (under hidden_files/btc-cycle/):
- btc-cycle-latest.json   latest values + read
- btc-cycle-history.jsonl append-only daily rows
- README.md               this file's content in doc form
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE = "https://bitcoin-data.com"
ENDPOINTS = {"mvrv": "/v1/mvrv", "nupl": "/v1/nupl", "sopr": "/v1/sopr"}
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)  # repo root when run from scripts/ on GitHub Actions
OUT_DIR = os.path.join(
    REPO if os.path.isdir(os.path.join(REPO, "hidden_files")) else os.path.expanduser("~/workspace/goals/alpaca-paper-trading-simulator"),
    "hidden_files", "btc-cycle"
)
UA = {"User-Agent": "DorkPaperTrading/1.0 (dork@agentmail.to)"}
LA = ZoneInfo("America/Los_Angeles")


def fetch(path):
    req = urllib.request.Request(BASE + path, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def read_gauge(name, value):
    if name == "mvrv":
        if value is None:
            return "unknown"
        if value > 3.5:
            return "OVERHEATED (top zone)"
        if value > 2.0:
            return "elevated"
        if value < 1.0:
            return "undervalued (bottom zone)"
        return "neutral"
    if name == "nupl":
        if value is None:
            return "unknown"
        if value > 0.75:
            return "EUPHORIA (top zone)"
        if value > 0.5:
            return "greed"
        if value < 0:
            return "capitulation (bottom zone)"
        return "neutral"
    if name == "sopr":
        if value is None:
            return "unknown"
        if value > 1.02:
            return "profit-taking"
        if value < 0.99:
            return "selling at a loss (capitulation)"
        return "neutral"
    return "unknown"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    now_utc = datetime.now(timezone.utc)
    now_la = now_utc.astimezone(LA)
    gauges = {}
    errors = []
    for name, path in ENDPOINTS.items():
        try:
            series = fetch(path)
            latest = series[-1]
            value = latest.get(name)
            gauges[name] = {
                "value": value,
                "date": latest.get("d"),
                "read": read_gauge(name, value),
            }
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:100]}")
            gauges[name] = {"value": None, "date": None, "read": "unknown"}

    overheated = sum(
        1
        for n, g in gauges.items()
        if "OVERHEATED" in g["read"] or "EUPHORIA" in g["read"]
    )
    if overheated >= 2:
        overall = "OVERHEATED: 2+ gauges in top zone -- trim-long bias, tighten stops"
    elif overheated == 1:
        overall = "WARM: 1 gauge in top zone -- watch, no aggression adds"
    elif any("capitulation" in g["read"] or "undervalued" in g["read"] for g in gauges.values()):
        overall = "WASHED OUT: bottom-zone read -- dip-buy setups back on the table"
    else:
        overall = "NEUTRAL: no cycle extreme"

    out = {
        "pulled_at_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pulled_at_pt": now_la.strftime("%Y-%m-%d %H:%M %Z"),
        "source": "bitcoin-data.com (keyless)",
        "gauges": gauges,
        "overall_read": overall,
        "errors": errors,
    }
    with open(os.path.join(OUT_DIR, "btc-cycle-latest.json"), "w") as f:
        json.dump(out, f, indent=2)

    hist = {
        "date": now_la.strftime("%Y-%m-%d"),
        "mvrv": gauges["mvrv"]["value"],
        "nupl": gauges["nupl"]["value"],
        "sopr": gauges["sopr"]["value"],
        "overall": overall,
    }
    with open(os.path.join(OUT_DIR, "btc-cycle-history.jsonl"), "a") as f:
        f.write(json.dumps(hist) + "\n")

    print("wrote btc-cycle-latest.json")
    for n, g in gauges.items():
        print(f"  {n}: {g['value']} ({g['date']}) -> {g['read']}")
    print("overall:", overall)
    if errors:
        print("ERRORS:", errors)
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
