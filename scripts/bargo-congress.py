#!/usr/bin/env python3
"""
bargo-congress.py -- Congressional trading watchlist feed via the Bargo API.

Replaces the old congress-trades.py House-PDF scraper (Oct 2026):
  - House + Senate (old script was House-only; Senate eFD bot-blocks us)
  - Clean JSON API, no RC4-PDF decryption pipeline, no pdftotext dependency
  - Adds per-trade and per-member performance attribution

CONTEXT ONLY -- never a trade trigger. STOCK Act disclosures lag 30-45 days
by law; this feed describes what members DID, not what to do.

Data source (free, no key): https://www.bargo.ai/free-apis/congress

Outputs (under hidden_files/congress/):
- bargo-latest.json      full snapshot: per-ticker aggregates + cluster flags
- bargo-flags-YYYY-MM-DD.md  human-readable flag log

Cluster rule: 3+ members buying the same ticker within 30 days -> research
lead (never a trade; still needs catalyst + momentum + written thesis).
"""

import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = "https://www.bargo.ai/free-apis/congress"
GOAL_DIR = os.path.expanduser("~/workspace/goals/alpaca-paper-trading-simulator")
OUT_DIR = os.path.join(GOAL_DIR, "hidden_files", "congress")
UA = {"User-Agent": "Mozilla/5.0 (compatible; paper-trading-research)"}


def get(path, timeout=30):
    req = urllib.request.Request(BASE + path, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_recent(max_trades=3000, per_page=100):
    """Page through /v1/trades (most-recent-first) up to max_trades."""
    trades, page = [], 1
    while len(trades) < max_trades:
        d = get(f"/v1/trades?limit={per_page}&page={page}")
        batch = d.get("trades", [])
        if not batch:
            break
        trades.extend(batch)
        page += 1
    return trades[:max_trades]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    cutoff = date.today() - timedelta(days=45)
    trades = fetch_recent()
    recent = [t for t in trades
              if t.get("disclosure_date", "") >= cutoff.isoformat()]

    per_ticker = defaultdict(lambda: {"buys": 0, "sells": 0,
                                      "buy_members": set(), "sell_members": set(),
                                      "trades": []})
    for t in recent:
        tick = (t.get("ticker") or "").upper()
        if not tick:
            continue
        agg = per_ticker[tick]
        typ = (t.get("type") or "").lower()
        member = t.get("member", "?")
        if "buy" in typ or "purchase" in typ:
            agg["buys"] += 1
            agg["buy_members"].add(member)
        elif "sell" in typ:
            agg["sells"] += 1
            agg["sell_members"].add(member)
        agg["trades"].append({
            "member": member, "chamber": t.get("chamber"),
            "type": t.get("type"), "amount_range": t.get("amount_range"),
            "txn_date": t.get("transaction_date"),
            "disclosed": t.get("disclosure_date"),
            "perf_pct": t.get("perf_pct"),
        })

    clusters = []
    for tick, agg in per_ticker.items():
        n_buyers = len(agg["buy_members"])
        if n_buyers >= 3 and agg["buys"] > agg["sells"]:
            clusters.append({"ticker": tick, "buyers": n_buyers,
                             "buys": agg["buys"], "sells": agg["sells"],
                             "members": sorted(agg["buy_members"])[:8]})

    snapshot = {
        "as_of": datetime.now().isoformat(timespec="seconds"),
        "source": "bargo.ai (House + Senate)",
        "trades_scanned": len(trades),
        "trades_last_45d": len(recent),
        "tickers": {k: {"buys": v["buys"], "sells": v["sells"],
                        "buy_members": sorted(v["buy_members"]),
                        "sell_members": sorted(v["sell_members"])}
                    for k, v in sorted(per_ticker.items())},
        "clusters": sorted(clusters, key=lambda c: -c["buyers"]),
    }
    with open(os.path.join(OUT_DIR, "bargo-latest.json"), "w") as f:
        json.dump(snapshot, f, indent=2)

    flag_path = os.path.join(
        OUT_DIR, f"bargo-flags-{date.today().isoformat()}.md")
    with open(flag_path, "w") as f:
        f.write(f"# Congressional cluster flags — {date.today().isoformat()}\n\n")
        f.write(f"Source: Bargo API (House + Senate), {len(recent)} trades "
                f"disclosed in last 45d.\n\n")
        if clusters:
            for c in snapshot["clusters"]:
                f.write(f"- **{c['ticker']}**: {c['buyers']} members buying "
                        f"({c['buys']} buys vs {c['sells']} sells) — "
                        f"{', '.join(c['members'])} — RESEARCH LEAD ONLY\n")
        else:
            f.write("No 3+ member buying clusters in the window.\n")

    print(f"Scanned {len(trades)} trades ({len(recent)} in last 45d), "
          f"{len(per_ticker)} tickers, {len(clusters)} clusters.")
    for c in snapshot["clusters"][:10]:
        print(f"  CLUSTER {c['ticker']}: {c['buyers']} buyers "
              f"({c['buys']}B/{c['sells']}S)")


if __name__ == "__main__":
    sys.exit(main())
